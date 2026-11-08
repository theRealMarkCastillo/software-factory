"""Drive the graph: admission, resume, cancellation, inspect and export.

No daemon, no timer, no second scheduler. Every entry point is a bounded call
made by the owning Hermes attempt (or by an operator command acting on a paused
run), and every call validates the current attempt before any effect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import OrchestrationError
from .graph import Deps, build_graph, thread_id
from .state import (
    GRAPH_VERSION,
    STATE_SCHEMA_VERSION,
    Attempt,
    FactoryRunState,
    RunAssignment,
    initial_state,
)
from .store import AdmissionPolicy, CoordinatorLock, RunStore


@dataclass
class DriveResult:
    thread_id: str
    status: str
    stage: str
    state: dict
    awaiting: dict | None = None

    @property
    def finished(self) -> bool:
        return self.awaiting is None

    def as_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "stage": self.stage,
            "awaiting": self.awaiting,
            "graph_version": self.state.get("graph_version"),
            "state_schema_version": self.state.get("state_schema_version"),
        }


def _checkpoint_saver(store: RunStore):
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver.from_conn_string(str(store.checkpoint_path))


def _graph_state(graph, config: dict) -> dict:
    snapshot = graph.get_state(config)
    return dict(snapshot.values or {})


def _interrupt_payload(snapshot) -> dict | None:
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return value
    return None


def current_state(deps: Deps, thread: str) -> dict:
    """Read the checkpointed state; read-only, requires no lock."""

    with _checkpoint_saver(deps.store) as saver:
        graph = build_graph(deps, saver)
        return _graph_state(graph, {"configurable": {"thread_id": thread}})


def drive(
    deps: Deps,
    assignment: RunAssignment,
    attempt: Attempt,
    *,
    resume_payload: dict | None = None,
) -> DriveResult:
    """Run or resume one bounded card until it awaits, holds or finishes."""

    store = deps.store
    thread = thread_id(assignment)
    existing = store.run(thread)
    if existing is None:
        AdmissionPolicy.load(deps.state_dir).check(assignment.card_id)

    view = deps.board.view(attempt)
    if not view.current(attempt):
        raise OrchestrationError(
            "stale_attempt",
            f"card {attempt.task_id} is not owned by run {attempt.run_id}",
        )

    lock = CoordinatorLock(deps.state_dir, thread)
    owner = lock.acquire()
    try:
        store.record_admission(thread, assignment, attempt)
        state = current_state(deps, thread)
        if state:
            _require_matching_versions(state)
            _require_matching_spec(state)

        with _checkpoint_saver(store) as saver:
            graph = build_graph(deps, saver)
            config = {
                "configurable": {"thread_id": thread},
                "recursion_limit": 60,
            }
            snapshot = graph.get_state(config)
            if not (snapshot.values or {}):
                graph.invoke(
                    initial_state(assignment, attempt, owner_token=owner.token),
                    config,
                )
            else:
                if (
                    resume_payload is None
                    and snapshot.next
                    and store.cancel_requested(thread)
                ):
                    resume_payload = {"kind": "cancelled"}
                if resume_payload is not None and snapshot.next:
                    from langgraph.types import Command

                    graph.invoke(Command(resume=resume_payload), config)
                elif snapshot.next and _interrupt_payload(snapshot) is not None:
                    # A persisted wait: return it instead of sleeping or advancing.
                    pass
                elif snapshot.next:
                    # A node died mid-execution without an interrupt. Continue it;
                    # its operation journal reconciles any effect it had started.
                    graph.invoke(None, config)
            snapshot = graph.get_state(config)
            values = _graph_state(graph, config)
            awaiting = (
                _interrupt_payload(snapshot)
                if snapshot.next and _interrupt_payload(snapshot) is not None
                else None
            )
    finally:
        lock.release()

    status = values.get("status") or "running"
    if awaiting is not None:
        status = (
            "awaiting_review"
            if awaiting.get("kind") == "await_review"
            else "awaiting_worker"
        )
    store.set_status(thread, status)
    return DriveResult(
        thread_id=thread,
        status=status,
        stage=values.get("stage", ""),
        state=values,
        awaiting=awaiting,
    )


def _require_matching_versions(state: dict) -> None:
    if state.get("graph_version") != GRAPH_VERSION:
        raise OrchestrationError(
            "version_mismatch",
            f"checkpoint graph {state.get('graph_version')!r} != {GRAPH_VERSION!r}",
        )
    if state.get("state_schema_version") != STATE_SCHEMA_VERSION:
        raise OrchestrationError(
            "version_mismatch",
            "checkpoint state schema "
            f"{state.get('state_schema_version')!r} != {STATE_SCHEMA_VERSION!r}",
        )


def _require_matching_spec(state: dict) -> None:
    """A changed spec invalidates a resume instead of being reinterpreted."""

    import hashlib
    from pathlib import Path

    path = Path(state.get("spec_reference") or "")
    if not path.is_file():
        raise OrchestrationError(
            "version_mismatch", f"spec reference is unreadable: {path}"
        )
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != state.get("spec_digest"):
        raise OrchestrationError(
            "version_mismatch", "spec content changed since admission"
        )


def resume(
    deps: Deps,
    thread: str,
    attempt: Attempt,
    *,
    payload: dict,
    resolved_hold: str,
) -> DriveResult:
    """Resume a paused graph, naming the hold/approval being resolved."""

    if not resolved_hold or not resolved_hold.strip():
        raise OrchestrationError(
            "invalid_assignment", "resume must name the hold it resolves"
        )
    store = deps.store
    existing = store.run(thread)
    if existing is None:
        raise OrchestrationError("invalid_assignment", f"no admitted run {thread}")
    if store.cancel_requested(thread):
        raise OrchestrationError(
            "cancelled", "cancellation is requested; use the cancel command"
        )
    state = current_state(deps, thread)
    if not state:
        raise OrchestrationError("invalid_assignment", "no checkpoint to resume")
    _require_matching_versions(state)
    _require_matching_spec(state)
    if int(state.get("hermes_attempt_id", 0)) != attempt.run_id:
        # A legitimate re-dispatch is a new attempt; it is recorded, never a
        # silent continuation of a superseded one (validated by the board view).
        store.event(
            thread,
            "attempt_advanced",
            {"from": state.get("hermes_attempt_id"), "to": attempt.run_id},
        )
    store.event(thread, "resume_requested", {"resolved_hold": resolved_hold})
    result = drive(deps, assignment_from_state(state), attempt, resume_payload=payload)
    return replace(
        result, state={**result.state, "hermes_attempt_id": str(attempt.run_id)}
    )


def assignment_from_state(state: dict) -> RunAssignment:
    """Rebuild the reviewed assignment from the checkpoint, never from prose."""

    return RunAssignment(
        board_id=state["board_id"],
        card_id=state["card_id"],
        repository=state["repository"],
        workspace_path=state["workspace_path"],
        immutable_base_sha=state["immutable_base_sha"],
        spec_reference=state["spec_reference"],
        spec_digest=state["spec_digest"],
        template_id=state["template_id"],
        worker_profile=state["worker_profile"],
        reviewer_profile=state["reviewer_profile"],
        acceptance_argv=tuple(state.get("acceptance_argv") or ("true",)),
        acceptance_artifacts=state.get("acceptance_artifacts")
        or state["workspace_path"],
        engine_release_reference=state["engine_release_reference"],
        engine_fingerprint=state["engine_fingerprint"],
        host_policy_digest=state["host_policy_digest"],
        max_repairs=int(state["max_repairs"]),
        cost_budget=state.get("cost_budget", "unknown"),
    )


def cancel(
    deps: Deps,
    thread: str,
    *,
    by: str,
    reason: str = "",
) -> dict:
    """Close admission for new effects and reconcile owned operations.

    Safe to repeat: the request row is idempotent. If another process currently
    owns evaluation, the request is observed at the next stage boundary and the
    command reports ``cancellation_pending`` instead of claiming completion.
    """

    store = deps.store
    created = store.request_cancel(thread, by=by, reason=reason)
    existing = store.run(thread)
    if existing is None:
        raise OrchestrationError("invalid_assignment", f"no admitted run {thread}")

    lock = CoordinatorLock(deps.state_dir, thread)
    try:
        lock.acquire()
    except OrchestrationError:
        store.event(thread, "cancel_pending_active_owner", {"by": by})
        return {"status": "cancellation_pending", "new_request": created}

    try:
        state = current_state(deps, thread)
        if not state:
            store.set_status(thread, "cancelled")
            return {"status": "cancelled", "new_request": created}
        with _checkpoint_saver(store) as saver:
            graph = build_graph(deps, saver)
            config = {
                "configurable": {"thread_id": thread},
                "recursion_limit": 60,
            }
            snapshot = graph.get_state(config)
            if snapshot.next:
                from langgraph.types import Command

                graph.invoke(Command(resume={"kind": "cancelled"}), config)
                values = _graph_state(graph, config)
            else:
                # No node is suspended, so there is no effect an interrupt could
                # replay; close the run and keep the checkpoint readable.
                values = state
                values["status"] = "cancelled"
                values["last_reason_code"] = "cancelled"
                store.event(thread, "cancelled_idle", {})
    finally:
        lock.release()

    status = values.get("status", "unknown")
    store.set_status(thread, status)
    return {
        "status": status,
        "reason": values.get("last_reason_code", ""),
        "detail": values.get("last_reason_detail", ""),
        "new_request": created,
    }


def inspect_run(deps: Deps, thread: str) -> dict:
    state = current_state(deps, thread)
    if not state:
        raise OrchestrationError("invalid_assignment", f"no checkpoint for {thread}")
    view = deps.store.inspect_view(thread, state)
    view["next_action"] = next_action(state, view)
    return view


def next_action(state: dict, view: dict) -> dict:
    status = state.get("status", "")
    if status == "ready_for_authorized_completion":
        return {
            "action": "return_to_lifecycle_owner",
            "detail": "completion is a proposal; promotion/push stay unauthorized here",
        }
    if status == "cancelled":
        return {"action": "none", "detail": "run cancelled"}
    if status == "held":
        return {
            "action": "operator_decision",
            "reason": state.get("last_reason_code", ""),
            "detail": state.get("last_reason_detail", ""),
        }
    pending = [
        record
        for record in view.get("operations", [])
        if record["state"] in {"submitted", "unknown"}
    ]
    if pending:
        return {
            "action": "resolve_pending_effect",
            "operation_key": pending[-1]["operation_key"],
            "state": pending[-1]["state"],
        }
    return {"action": "resume", "detail": "no pending effect recorded"}


def export_run(deps: Deps, thread: str) -> dict:
    state = current_state(deps, thread)
    return deps.store.export(thread, state)


def write_export(path: str | Path, payload: dict) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    )
    return destination


def state_as_json(state: FactoryRunState | dict) -> str:
    return json.dumps(state, indent=2, sort_keys=True, default=str)


__all__ = [
    "Deps",
    "DriveResult",
    "cancel",
    "current_state",
    "drive",
    "export_run",
    "inspect_run",
    "next_action",
    "resume",
    "write_export",
]
