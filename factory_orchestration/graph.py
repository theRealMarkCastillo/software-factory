"""Explicit versioned StateGraph for one bounded card (``factory-card/v1``).

Topology::

    admit -> validate_spec -> prepare_workspace -> request_implementation
          -> await_worker -> bind_candidate -> run_acceptance -> verify_evidence
          -> request_independent_review -> await_review -> return_result -> END

    any failure -> classify -> request_repair | hold_budget | hold_infrastructure
                            | hold_reconciliation | hold_operator
    cancellation at any stage -> cancel_owned_operations -> reconcile -> END

A wait is persisted state: ``await_worker``/``await_review`` raise a LangGraph
interrupt instead of sleeping in a subprocess, and resume only inside a
currently authorized Hermes attempt. Every mutating step goes through the
operation journal (intent before effect, receipt after), so a replayed node
reconciles the durable target instead of resubmitting.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import OrchestrationError
from .operations import digest_parts, resolve, stable_key
from .state import (
    Attempt,
    FactoryRunState,
    ReviewResult,
    RunAssignment,
    WorkerReceipt,
    WorkerRequest,
    approval_matches,
)
from .store import RunStore

HOLD_ROUTES = (
    "hold_budget",
    "hold_infrastructure",
    "hold_reconciliation",
    "hold_operator",
)


@dataclass
class Deps:
    store: RunStore
    board: Any
    worker: Any
    gate: Any
    reviewer: Any
    state_dir: Path
    clock: Callable[[], float] = time.time
    worker_timeout_s: int = 3600
    gate_timeout_s: int = 3600
    review_timeout_s: int = 3600
    artifact_dir: Path | None = None
    release_identity: tuple[str, str, str] = ("", "", "")
    events: list[dict] = field(default_factory=list)


def thread_id(assignment: RunAssignment) -> str:
    """One graph run per card; a moved base on the same card fails closed."""

    return f"{assignment.board_id}:{assignment.card_id}"


def _attempt(state: dict) -> Attempt:
    return Attempt(
        task_id=state["card_id"],
        run_id=int(state["hermes_attempt_id"]),
        profile=state.get("hermes_assignee") or state["worker_profile"],
        claim_lock=state.get("attempt_claim_lock") or None,
    )


def _route_for(reason: str) -> str:
    return {
        "cancelled": "cancel",
        "budget_exhausted": "hold_budget",
        "effect_unknown": "hold_reconciliation",
        "cancellation_pending": "hold_reconciliation",
        "worker_unavailable": "hold_infrastructure",
        "gate_unavailable": "hold_infrastructure",
        "stale_attempt": "hold_operator",
        "owner_grant_absent": "hold_operator",
        "review_scope_violation": "hold_operator",
        "review_unavailable": "hold_infrastructure",
        "invalid_assignment": "hold_operator",
        "version_mismatch": "hold_operator",
        "workspace_conflict": "hold_operator",
        "capability_unenforced": "hold_operator",
        "admission_disabled": "hold_operator",
        "review_inconclusive": "hold_operator",
        "protocol_error": "hold_operator",
        "worker_failed": "repair_or_hold",
        "evidence_rejected": "repair_or_hold",
        "review_changes": "repair_or_hold",
    }.get(reason, "hold_operator")


STRUCTURAL_EVIDENCE_REASONS = frozenset(
    {
        "mode_not_acceptance",
        "gates_disabled",
        "source_dirty",
        "source_changed_during_run",
        "teardown_failed",
        "safety_incident",
        "collateral_change",
        "engine_mismatch",
        "base_mismatch",
        "unknown_object",
        "not_a_commit",
        "artifacts_dir_invalid",
        "manifest_missing",
        "manifest_symlink",
        "malformed_manifest",
        "manifest_digest_missing",
        "manifest_digest_mismatch",
        "legacy_schema_unsupported",
        "unsupported_schema_name",
        "unsupported_schema_version",
        "invalid_argument",
    }
)


def _structural_evidence_reason(reason: str) -> bool:
    """A malformed/tampered artifact set is not repaired by another attempt."""

    return (
        reason in STRUCTURAL_EVIDENCE_REASONS
        or reason.startswith("artifact_")
        or reason.startswith("duplicate_json_keys")
        or reason.startswith("manifest_")
    )


def _git(repository: str, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            repository,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "--no-optional-locks",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OrchestrationError(
            "evidence_rejected",
            f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}",
        )
    return result.stdout.strip()


def _manifest_digest(artifacts: str) -> str:
    path = Path(artifacts) / "evidence-manifest.json"
    if not path.is_file():
        return ""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _advance(state: dict, stage: str, **updates: Any) -> dict:
    payload = {
        "stage": stage,
        "next_stage": stage,
        "stage_sequence": int(state.get("stage_sequence", 0)) + 1,
        "route": "continue",
        "last_reason_code": "",
        "last_reason_detail": "",
    }
    payload.update(updates)
    return payload


def _merge_operations(state: dict, key: str, record: dict) -> dict:
    operations = dict(state.get("operations") or {})
    operations[key] = record
    return operations


def _receipt_from_payload(payload: Any, attempt: Attempt) -> WorkerReceipt:
    if not isinstance(payload, dict):
        raise OrchestrationError("protocol_error", "resume payload must be an object")
    if payload.get("kind") == "cancelled":
        raise OrchestrationError("cancelled", "cancellation requested while waiting")
    receipt = payload.get("receipt")
    if not isinstance(receipt, dict):
        raise OrchestrationError("protocol_error", "resume payload has no receipt")
    return WorkerReceipt(
        operation_key=receipt["operation_key"],
        attempt=attempt,
        session_id=receipt.get("session_id", ""),
        outcome=receipt.get("outcome", ""),
        candidate_sha=receipt.get("candidate_sha"),
        artifact_references=tuple(receipt.get("artifact_references") or ()),
        reported_tests=tuple(receipt.get("reported_tests") or ()),
        detail=receipt.get("detail", ""),
    )


def _review_from_payload(payload: Any, attempt: Attempt) -> ReviewResult:
    if not isinstance(payload, dict):
        raise OrchestrationError("protocol_error", "resume payload must be an object")
    if payload.get("kind") == "cancelled":
        raise OrchestrationError("cancelled", "cancellation requested while waiting")
    review = payload.get("review")
    if not isinstance(review, dict):
        raise OrchestrationError("protocol_error", "resume payload has no review")
    required = (
        "operation_key",
        "reviewer",
        "outcome",
        "candidate_sha",
        "base_sha",
        "spec_digest",
        "evidence_digest",
        "graph_version",
    )
    missing = [name for name in required if not review.get(name)]
    if missing:
        # A compact receipt ({outcome, comment}) cannot be bound to a candidate.
        # Hold; never crash the node and never adopt an unbound verdict.
        raise OrchestrationError(
            "protocol_error",
            "review receipt lacks binding fields: " + ", ".join(sorted(missing)),
        )
    return ReviewResult(
        operation_key=review["operation_key"],
        attempt=attempt,
        reviewer=review["reviewer"],
        outcome=review["outcome"],
        candidate_sha=review["candidate_sha"],
        base_sha=review["base_sha"],
        spec_digest=review["spec_digest"],
        evidence_digest=review["evidence_digest"],
        graph_version=review["graph_version"],
        comment=review.get("comment", ""),
    )


def build_graph(deps: Deps, checkpointer=None):
    """Build and compile the versioned graph.

    LangGraph is imported lazily so ``inspect``/``export`` and the standalone
    verification engine never require the optional orchestration extra.
    """

    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extra
        raise OrchestrationError(
            "invalid_assignment",
            "LangGraph is not installed; install the 'orchestration' extra",
        ) from exc

    def guard(state: dict) -> Attempt:
        attempt = _attempt(state)
        view = deps.board.view(attempt)
        if not view.current(attempt):
            raise OrchestrationError(
                "stale_attempt",
                f"card {attempt.task_id} is not owned by run {attempt.run_id}",
            )
        return attempt

    def node(
        name: str,
        fn: Callable[[dict], dict],
        *,
        ownership: bool = True,
        cancellable: bool = True,
    ):
        def run(state: dict) -> dict:
            thread = state["run_id"]
            if cancellable and deps.store.cancel_requested(thread):
                deps.store.event(thread, "cancellation_observed", {"at": name})
                return {
                    "route": "cancel",
                    "cancellation_requested": True,
                    "stage": name,
                }
            try:
                if ownership:
                    guard(state)
                updates = fn(state) or {}
            except OrchestrationError as exc:
                deps.store.event(
                    thread,
                    "stage_error",
                    {"stage": name, "reason": exc.reason, "detail": exc.detail},
                )
                return {
                    "route": exc.route or _route_for(exc.reason),
                    "last_reason_code": exc.reason,
                    "last_reason_detail": exc.detail,
                    "stage": name,
                }
            updates.setdefault("route", "continue")
            updates["stage"] = name
            updates["stage_sequence"] = int(state.get("stage_sequence", 0)) + 1
            deps.store.event(
                thread,
                "stage",
                {"stage": name, "route": updates["route"]},
            )
            return updates

        return run

    # -- stages -------------------------------------------------------------
    def admit(state: dict) -> dict:
        attempt = guard(state)
        assignment = _assignment_from_state(state, deps)
        deps.store.record_admission(state["run_id"], assignment, attempt)
        return _advance(state, "validate_spec", status="running")

    def validate_spec(state: dict) -> dict:
        spec_path = Path(state["spec_reference"])
        if not spec_path.is_file():
            raise OrchestrationError(
                "invalid_assignment", f"spec reference is unreadable: {spec_path}"
            )
        actual = "sha256:" + hashlib.sha256(spec_path.read_bytes()).hexdigest()
        if actual != state["spec_digest"]:
            raise OrchestrationError(
                "invalid_assignment",
                "spec digest does not match the persisted assignment",
            )
        if state["graph_version"] != "factory-card/v1":
            raise OrchestrationError(
                "version_mismatch", f"unsupported graph {state['graph_version']}"
            )
        return _advance(state, "prepare_workspace")

    def prepare_workspace(state: dict) -> dict:
        workspace = Path(state["workspace_path"])
        repository = Path(state["repository"])
        if not workspace.is_dir() or not repository.is_dir():
            raise OrchestrationError(
                "invalid_assignment", "workspace or repository is missing"
            )
        _git(
            state["repository"],
            "cat-file",
            "-e",
            f"{state['immutable_base_sha']}^{{commit}}",
        )
        return _advance(state, "request_implementation")

    def request_implementation(state: dict) -> dict:
        thread = state["run_id"]
        repair_index = deps.store.repair_count(thread)
        input_digest = digest_parts(
            "implementation/v1",
            state["immutable_base_sha"],
            state["spec_digest"],
            state["workspace_path"],
            repair_index,
        )
        key = stable_key(
            run_id=thread,
            stage="request_implementation",
            repair_index=repair_index,
            input_digest=input_digest,
        )
        record = deps.store.begin_operation(
            key,
            thread,
            stage="request_implementation",
            kind="worker",
            repair_index=repair_index,
            input_digest=input_digest,
        )
        probe = (
            deps.worker.probe(key)
            if record["state"] in {"submitted", "unknown"}
            else None
        )
        resolution = resolve(record, probe)
        operations = _merge_operations(
            state,
            key,
            {
                "operation_key": key,
                "kind": "worker",
                "input_digest": input_digest,
                "repair_index": repair_index,
                "state": record["state"],
                "request_id": record.get("request_id") or "",
            },
        )
        if resolution.action == "hold":
            raise OrchestrationError("effect_unknown", resolution.detail)
        if resolution.action in {"adopt", "replay"}:
            receipt = resolution.receipt_object
            if receipt is None:
                # A request exists but has no receipt yet: attach to the existing
                # work and wait. Never submit a second request for the same key.
                operations[key].update({"state": "submitted"})
                return _advance(state, "await_worker", operations=operations)
            deps.store.mark_operation(
                key, "confirmed", receipt=_receipt_payload(receipt)
            )
            return _advance(
                state,
                "await_worker",
                operations=operations,
                worker_session_id=receipt.session_id,
                worker_request_id=record.get("request_id") or "",
            )

        request = WorkerRequest(
            operation_key=key,
            attempt=_attempt(state),
            profile=state["worker_profile"],
            spec_digest=state["spec_digest"],
            workspace=state["workspace_path"],
            base_sha=state["immutable_base_sha"],
            allowed_scope=(state["workspace_path"],),
            repair_index=repair_index,
            timeout_s=deps.worker_timeout_s,
            artifact_dir=str(deps.artifact_dir or deps.state_dir),
            prompt=_implementation_prompt(state, repair_index),
        )
        # Persist the intent to submit BEFORE the effect: a crash from here on
        # leaves an operation the next run must reconcile, never blindly repeat.
        deps.store.mark_operation(key, "submitted")
        result = deps.worker.submit(request)
        deps.store.mark_operation(key, "submitted", request_id=result.handle.request_id)
        operations[key].update(
            {"state": "submitted", "request_id": result.handle.request_id}
        )
        return _advance(
            state,
            "await_worker",
            operations=operations,
            worker_request_id=result.handle.request_id,
        )

    def await_worker(state: dict) -> dict:
        thread = state["run_id"]
        operations = dict(state.get("operations") or {})
        key = _pending_key(operations, "worker")
        if key is None:
            raise OrchestrationError(
                "protocol_error", "no pending worker operation to await"
            )
        attempt = _attempt(state)
        request_id = operations[key].get("request_id", "")
        external = _handle(deps.worker, key, request_id)
        receipt = deps.worker.poll(external)
        if receipt is None:
            payload = interrupt(
                {
                    "kind": "await_worker",
                    "operation_key": key,
                    "attempt": attempt.as_dict(),
                }
            )
            receipt = _receipt_from_payload(payload, attempt)
        if receipt.operation_key != key:
            raise OrchestrationError(
                "protocol_error", "receipt operation key does not match the request"
            )
        if receipt.attempt.run_id != attempt.run_id:
            deps.store.event(
                thread,
                "late_receipt",
                {"expected_run": attempt.run_id, "receipt_run": receipt.attempt.run_id},
            )
            raise OrchestrationError(
                "stale_attempt", "receipt from a superseded attempt"
            )

        deps.store.mark_operation(key, "confirmed", receipt=_receipt_payload(receipt))
        operations[key].update({"state": "confirmed"})
        updates = {
            "operations": operations,
            "worker_session_id": receipt.session_id,
        }
        if receipt.outcome == "completed":
            return _advance(
                state,
                "bind_candidate",
                candidate_sha=receipt.candidate_sha,
                **updates,
            )
        if receipt.outcome == "cancelled":
            raise OrchestrationError("cancelled", "worker reported cancellation")
        if receipt.outcome == "unavailable":
            raise OrchestrationError("worker_unavailable", receipt.detail)
        if receipt.outcome == "ambiguous":
            raise OrchestrationError("effect_unknown", receipt.detail)
        raise OrchestrationError("worker_failed", receipt.detail or "worker failed")

    def bind_candidate(state: dict) -> dict:
        candidate = state.get("candidate_sha") or ""
        if len(candidate) != 40:
            raise OrchestrationError(
                "protocol_error", "a completed worker must bind a full candidate SHA"
            )
        _git(state["repository"], "cat-file", "-e", f"{candidate}^{{commit}}")
        _git(
            state["repository"],
            "merge-base",
            "--is-ancestor",
            state["immutable_base_sha"],
            candidate,
        )
        status = _git(state["workspace_path"], "status", "--porcelain")
        if status:
            raise OrchestrationError(
                "worker_failed", "candidate workspace is not clean"
            )
        # A new candidate invalidates candidate-specific evidence and approval.
        return _advance(
            state,
            "run_acceptance",
            evidence_reference="",
            evidence_digest="",
            approval_reference="",
            reviewer_result={},
        )

    def run_acceptance(state: dict) -> dict:
        thread = state["run_id"]
        repair_index = deps.store.repair_count(thread)
        input_digest = digest_parts(
            "acceptance/v1",
            state["candidate_sha"],
            state["immutable_base_sha"],
            state["workspace_path"],
            repair_index,
        )
        key = stable_key(
            run_id=thread,
            stage="run_acceptance",
            repair_index=repair_index,
            input_digest=input_digest,
        )
        record = deps.store.begin_operation(
            key,
            thread,
            stage="run_acceptance",
            kind="gate",
            repair_index=repair_index,
            input_digest=input_digest,
        )
        probe = (
            deps.gate.probe(key)
            if record["state"] in {"submitted", "unknown"}
            else None
        )
        resolution = resolve(record, probe)
        operations = _merge_operations(
            state,
            key,
            {
                "operation_key": key,
                "kind": "gate",
                "input_digest": input_digest,
                "repair_index": repair_index,
                "state": record["state"],
            },
        )
        if resolution.action == "hold":
            raise OrchestrationError("effect_unknown", resolution.detail)
        if resolution.action in {"adopt", "replay"}:
            receipt = resolution.receipt or {}
            if receipt.get("outcome") != "pass":
                raise OrchestrationError("effect_unknown", resolution.detail)
            operations[key]["state"] = "confirmed"
            deps.store.mark_operation(key, "confirmed", receipt=receipt)
            return _advance(
                state,
                "verify_evidence",
                operations=operations,
                evidence_reference=receipt.get("evidence_reference", ""),
                evidence_digest=_manifest_digest(receipt.get("evidence_reference", "")),
            )
        deps.store.mark_operation(key, "submitted")
        outcome = deps.gate.run_acceptance(
            operation_key=key,
            attempt=_attempt(state),
            workspace=state["workspace_path"],
            base_sha=state["immutable_base_sha"],
            candidate_sha=state["candidate_sha"],
            argv=tuple(_assignment_from_state(state, deps).acceptance_argv),
            artifacts=str(deps.artifact_dir or deps.state_dir),
            timeout_s=deps.gate_timeout_s,
        )
        deps.store.mark_operation(
            key,
            "confirmed" if outcome.outcome == "pass" else "failed",
            receipt={
                "stage": "run_acceptance",
                "outcome": outcome.outcome,
                "reason": outcome.reason,
                "evidence_reference": outcome.evidence_reference,
            },
        )
        operations[key].update(
            {"state": "confirmed" if outcome.outcome == "pass" else "failed"}
        )
        if outcome.outcome == "pass":
            return _advance(
                state,
                "verify_evidence",
                operations=operations,
                evidence_reference=outcome.evidence_reference,
                evidence_digest=outcome.evidence_digest,
            )
        if outcome.outcome == "unavailable":
            raise OrchestrationError(
                "gate_unavailable", outcome.detail or outcome.reason
            )
        if outcome.outcome == "inconclusive":
            raise OrchestrationError(
                "protocol_error", outcome.detail or "gate was inconclusive"
            )
        raise OrchestrationError("evidence_rejected", outcome.reason or "gate failed")

    def verify_evidence(state: dict) -> dict:
        thread = state["run_id"]
        outcome = deps.gate.verify(
            artifacts=state["evidence_reference"],
            repository=state["repository"],
            candidate_sha=state["candidate_sha"],
            base_sha=state["immutable_base_sha"],
        )
        if not outcome.certified:
            structural = _structural_evidence_reason(outcome.reason)
            raise OrchestrationError(
                "evidence_rejected",
                f"{outcome.reason}: {outcome.detail}",
                route="hold_operator" if structural else None,
            )
        if (
            state.get("evidence_digest")
            and outcome.evidence_digest
            and outcome.evidence_digest != state["evidence_digest"]
        ):
            raise OrchestrationError(
                "evidence_rejected",
                "evidence changed between acceptance and verification",
            )
        deps.store.event(thread, "evidence_verified", {"reason": outcome.reason})
        return _advance(
            state,
            "request_independent_review",
            evidence_digest=outcome.evidence_digest or state["evidence_digest"],
        )

    def request_independent_review(state: dict) -> dict:
        thread = state["run_id"]
        if state["reviewer_profile"] == state["worker_profile"]:
            raise OrchestrationError(
                "invalid_assignment", "implementer cannot review its own candidate"
            )
        if not state.get("evidence_digest"):
            raise OrchestrationError(
                "protocol_error", "review requires verified evidence"
            )
        repair_index = deps.store.repair_count(thread)
        input_digest = digest_parts(
            "review/v1",
            state["candidate_sha"],
            state["immutable_base_sha"],
            state["spec_digest"],
            state["evidence_digest"],
            repair_index,
        )
        key = stable_key(
            run_id=thread,
            stage="request_independent_review",
            repair_index=repair_index,
            input_digest=input_digest,
        )
        record = deps.store.begin_operation(
            key,
            thread,
            stage="request_independent_review",
            kind="review",
            repair_index=repair_index,
            input_digest=input_digest,
        )
        probe = (
            deps.reviewer.probe(key)
            if record["state"] in {"submitted", "unknown"}
            else None
        )
        resolution = resolve(record, probe)
        operations = _merge_operations(
            state,
            key,
            {
                "operation_key": key,
                "kind": "review",
                "input_digest": input_digest,
                "repair_index": repair_index,
                "state": record["state"],
            },
        )
        if resolution.action == "hold":
            raise OrchestrationError("effect_unknown", resolution.detail)
        if resolution.action in {"adopt", "replay"}:
            stored = resolution.receipt
            if stored is None:
                operations[key].update({"state": "submitted"})
                return _advance(state, "await_review", operations=operations)
            review = _review_from_payload({"review": stored}, _attempt(state))
            if review.operation_key != key:
                raise OrchestrationError(
                    "protocol_error", "stored review key does not match the operation"
                )
            return _apply_review(state, key, review, {"operations": operations})
        deps.store.mark_operation(key, "submitted")
        handle, inline = deps.reviewer.submit(
            operation_key=key,
            attempt=_attempt(state),
            reviewer=state["reviewer_profile"],
            workspace=state["workspace_path"],
            candidate_sha=state["candidate_sha"],
            base_sha=state["immutable_base_sha"],
            spec_digest=state["spec_digest"],
            evidence_digest=state["evidence_digest"],
            artifacts=state["evidence_reference"],
            timeout_s=deps.review_timeout_s,
        )
        deps.store.mark_operation(key, "submitted", request_id=handle.request_id)
        operations[key].update({"state": "submitted", "request_id": handle.request_id})
        updates = {"operations": operations, "reviewer_request_id": handle.request_id}
        if inline is not None:
            return _apply_review(state, key, inline, updates)
        return _advance(state, "await_review", **updates)

    def await_review(state: dict) -> dict:
        operations = dict(state.get("operations") or {})
        key = _pending_key(operations, "review")
        if key is None:
            raise OrchestrationError("protocol_error", "no pending review to await")
        attempt = _attempt(state)
        handle = _handle(deps.reviewer, key, operations[key].get("request_id", ""))
        review = deps.reviewer.poll(handle)
        if review is None:
            payload = interrupt(
                {
                    "kind": "await_review",
                    "operation_key": key,
                    "attempt": attempt.as_dict(),
                }
            )
            review = _review_from_payload(payload, attempt)
        if review.operation_key != key:
            raise OrchestrationError(
                "protocol_error", "review result key does not match the request"
            )
        if review.attempt.run_id != attempt.run_id:
            deps.store.event(
                state["run_id"],
                "late_review",
                {"expected_run": attempt.run_id, "review_run": review.attempt.run_id},
            )
            raise OrchestrationError(
                "stale_attempt", "review from a superseded attempt"
            )
        return _apply_review(state, key, review, {"operations": operations})

    def _apply_review(
        state: dict, key: str, review: ReviewResult, updates: dict
    ) -> dict:
        thread = state["run_id"]
        expected = {
            "candidate_sha": state["candidate_sha"],
            "base_sha": state["immutable_base_sha"],
            "spec_digest": state["spec_digest"],
            "evidence_digest": state["evidence_digest"],
            "graph_version": state["graph_version"],
        }
        actual = review.binding()
        for name, value in expected.items():
            if actual[name] != value:
                deps.store.event(
                    thread,
                    "review_binding_mismatch",
                    {"field": name, "expected": value, "actual": actual[name]},
                )
                raise OrchestrationError(
                    "review_inconclusive",
                    f"review is bound to a different {name}",
                )
        if review.reviewer == state["worker_profile"]:
            raise OrchestrationError(
                "invalid_assignment", "implementer cannot approve its own candidate"
            )
        operations = dict(updates.get("operations") or {})
        operations[key].update({"state": "confirmed"})
        deps.store.mark_operation(
            key,
            "confirmed",
            receipt={
                "outcome": review.outcome,
                "reviewer": review.reviewer,
                "candidate_sha": review.candidate_sha,
            },
        )
        if review.outcome == "approve":
            approval_key = deps.store.record_approval(
                thread,
                reviewer=review.reviewer,
                outcome="approve",
                candidate_sha=review.candidate_sha,
                base_sha=review.base_sha,
                spec_digest=review.spec_digest,
                evidence_digest=review.evidence_digest,
                graph_version=review.graph_version,
                comment=review.comment,
            )
            return _advance(
                state,
                "return_result",
                approval_reference=approval_key,
                reviewer_result=review.binding(),
                operations=operations,
            )
        if review.outcome == "changes_required":
            raise OrchestrationError(
                "review_changes", review.comment or "changes required"
            )
        raise OrchestrationError(
            "review_inconclusive",
            review.comment or f"reviewer outcome {review.outcome}",
        )

    def return_result(state: dict) -> dict:
        thread = state["run_id"]
        approval = deps.store.approval(state.get("approval_reference") or "")
        if not approval_matches(
            approval,
            candidate_sha=state["candidate_sha"],
            base_sha=state["immutable_base_sha"],
            spec_digest=state["spec_digest"],
            evidence_digest=state["evidence_digest"],
            implementer=state["worker_profile"],
        ):
            raise OrchestrationError(
                "review_inconclusive",
                "no matching independent approval for this candidate",
            )
        if not state.get("evidence_reference"):
            raise OrchestrationError("evidence_rejected", "no evidence was verified")
        deps.store.event(
            thread,
            "ready_for_authorized_completion",
            {"approval_key": approval["approval_key"]},
        )
        return {
            "stage": "return_result",
            "next_stage": END,
            "stage_sequence": int(state.get("stage_sequence", 0)) + 1,
            "status": "ready_for_authorized_completion",
            "route": "continue",
            "last_reason_code": "",
            "last_reason_detail": "",
        }

    def classify(state: dict) -> dict:
        thread = state["run_id"]
        route = state.get("route", "")
        reason = state.get("last_reason_code", "")
        if route == "cancel":
            deps.store.event(thread, "classify", {"route": "cancel", "reason": reason})
            return {"route": "cancel_owned_operations", "status": "running"}
        if route == "repair_or_hold":
            count = deps.store.repair_count(thread)
            maximum = int(state.get("max_repairs", 0))
            if count < maximum:
                deps.store.event(
                    thread, "classify", {"route": "repair", "repairs": count}
                )
                return {"route": "repair", "status": "running"}
            return {
                "route": "hold_budget",
                "last_reason_code": "budget_exhausted",
                "last_reason_detail": f"repair budget exhausted ({count}/{maximum})",
                "status": "held",
            }
        if route in HOLD_ROUTES:
            return {"route": route, "status": "held"}
        return {
            "route": "hold_operator",
            "last_reason_code": reason or "protocol_error",
            "status": "held",
        }

    def request_repair(state: dict) -> dict:
        thread = state["run_id"]
        count = deps.store.bump_repair(thread)
        deps.store.event(thread, "repair_requested", {"repair_count": count})
        return _advance(
            state,
            "request_implementation",
            repair_count=count,
            status="running",
        )

    def hold(node: str):
        def held(state: dict) -> dict:
            reason = (
                state.get("last_reason_code")
                or {
                    "hold_budget": "budget_exhausted",
                    "hold_infrastructure": "worker_unavailable",
                    "hold_reconciliation": "effect_unknown",
                    "hold_operator": "protocol_error",
                }[node]
            )
            deps.store.set_status(state["run_id"], "held")
            deps.store.event(
                state["run_id"],
                "held",
                {"node": node, "reason": reason},
            )
            return {
                "stage": node,
                "stage_sequence": int(state.get("stage_sequence", 0)) + 1,
                "status": "held",
                "route": "end",
                "last_reason_code": reason,
            }

        return held

    def cancel_owned_operations(state: dict) -> dict:
        thread = state["run_id"]
        operations = dict(state.get("operations") or {})
        for key, record in list(operations.items()):
            if record.get("state") not in {"submitted", "unknown"}:
                continue
            handle = _handle(
                _adapter_for(deps, record.get("kind")),
                key,
                record.get("request_id", ""),
            )
            adapter = _adapter_for(deps, record.get("kind"))
            try:
                report = adapter.cancel(handle)
            except Exception as exc:  # pragma: no cover - adapter specific
                deps.store.event(
                    thread, "cancel_error", {"key": key, "error": str(exc)[:200]}
                )
                continue
            deps.store.mark_operation(
                key,
                "cancelled" if report.cancelled else "unknown",
                detail=report.detail,
            )
            operations[key]["state"] = "cancelled" if report.cancelled else "unknown"
            deps.store.event(
                thread, "operation_cancelled", {"key": key, "ok": report.cancelled}
            )
        return {
            "stage": "reconcile",
            "stage_sequence": int(state.get("stage_sequence", 0)) + 1,
            "cancellation_requested": True,
            "operations": operations,
            "route": "continue",
        }

    def reconcile(state: dict) -> dict:
        thread = state["run_id"]
        unknown: list[str] = []
        for record in deps.store.operations(thread):
            if record["state"] not in {"submitted", "unknown"}:
                continue
            adapter = _adapter_for(deps, record["kind"])
            probe = adapter.probe(record["operation_key"])
            if probe.state == "present":
                deps.store.mark_operation(
                    record["operation_key"],
                    "confirmed",
                    receipt=probe.receipt,
                    detail="effect retained during cancellation",
                )
            else:
                unknown.append(record["operation_key"])
        if unknown:
            deps.store.set_status(thread, "held")
            deps.store.event(thread, "cancellation_pending", {"operations": unknown})
            return {
                "stage": "reconcile",
                "stage_sequence": int(state.get("stage_sequence", 0)) + 1,
                "status": "held",
                "route": "end",
                "last_reason_code": "cancellation_pending",
                "last_reason_detail": ", ".join(unknown),
            }
        deps.store.set_status(thread, "cancelled")
        deps.store.event(thread, "cancelled", {})
        return {
            "stage": "return_cancelled",
            "stage_sequence": int(state.get("stage_sequence", 0)) + 1,
            "status": "cancelled",
            "route": "end",
            "last_reason_code": "cancelled",
        }

    builder = StateGraph(FactoryRunState)
    stages = [
        ("admit", admit),
        ("validate_spec", validate_spec),
        ("prepare_workspace", prepare_workspace),
        ("request_implementation", request_implementation),
        ("await_worker", await_worker),
        ("bind_candidate", bind_candidate),
        ("run_acceptance", run_acceptance),
        ("verify_evidence", verify_evidence),
        ("request_independent_review", request_independent_review),
        ("await_review", await_review),
        ("return_result", return_result),
    ]
    next_of = {
        "admit": "validate_spec",
        "validate_spec": "prepare_workspace",
        "prepare_workspace": "request_implementation",
        "request_implementation": "await_worker",
        "await_worker": "bind_candidate",
        "bind_candidate": "run_acceptance",
        "run_acceptance": "verify_evidence",
        "verify_evidence": "request_independent_review",
        "request_independent_review": "await_review",
        "await_review": "return_result",
        "return_result": END,
    }
    for name, fn in stages:
        builder.add_node(name, node(name, fn))
    builder.add_node(
        "classify", node("classify", classify, ownership=False, cancellable=False)
    )
    builder.add_node("request_repair", node("request_repair", request_repair))
    for name in HOLD_ROUTES:
        builder.add_node(
            name, node(name, hold(name), ownership=False, cancellable=False)
        )
    builder.add_node(
        "cancel_owned_operations",
        node(
            "cancel_owned_operations",
            cancel_owned_operations,
            ownership=False,
            cancellable=False,
        ),
    )
    builder.add_node(
        "reconcile", node("reconcile", reconcile, ownership=False, cancellable=False)
    )

    builder.add_edge(START, "admit")
    all_stages = {stage for stage, _ in stages}
    path_map = {name: name for name in all_stages}
    path_map["classify"] = "classify"
    path_map[END] = END
    for name in [stage for stage, _ in stages]:
        target = next_of[name]
        builder.add_conditional_edges(name, _select(target), path_map)
    builder.add_conditional_edges(
        "classify",
        _classify_select,
        {
            "repair": "request_repair",
            "cancel_owned_operations": "cancel_owned_operations",
            **{route: route for route in HOLD_ROUTES},
            "end": END,
        },
    )
    builder.add_edge("request_repair", "request_implementation")
    builder.add_edge("cancel_owned_operations", "reconcile")
    builder.add_edge("reconcile", END)
    for name in HOLD_ROUTES:
        builder.add_edge(name, END)
    return builder.compile(checkpointer=checkpointer)


def _select(next_node: str):
    def select(state: dict) -> str:
        if (
            state.get("route", "continue") == "continue"
            and state.get("status") != "held"
        ):
            return state.get("next_stage") or next_node
        return "classify"

    return select


def _classify_select(state: dict) -> str:
    return state.get("route", "hold_operator")


def _adapter_for(deps: Deps, kind: str | None):
    if kind == "gate":
        return deps.gate
    if kind == "review":
        return deps.reviewer
    return deps.worker


def _handle(adapter, key: str, request_id: str):
    from .adapters.base import WorkerHandle

    return WorkerHandle(operation_key=key, request_id=request_id)


def _pending_key(operations: dict, kind: str) -> str | None:
    candidates = [
        key
        for key, record in operations.items()
        if record.get("kind") == kind
        and record.get("state") in {"submitted", "unknown"}
    ]
    return candidates[-1] if candidates else None


def _receipt_payload(receipt: WorkerReceipt) -> dict:
    return {
        "operation_key": receipt.operation_key,
        "attempt": receipt.attempt.as_dict(),
        "session_id": receipt.session_id,
        "outcome": receipt.outcome,
        "candidate_sha": receipt.candidate_sha,
        "artifact_references": list(receipt.artifact_references),
        "reported_tests": list(receipt.reported_tests),
        "detail": receipt.detail,
    }


def _implementation_prompt(state: dict, repair_index: int) -> str:
    """A bounded, data-only assignment. Never executable graph Python."""

    return (
        "You are the implementation worker for a bounded factory card.\n"
        f"Card: {state['card_id']}\n"
        f"Workspace: {state['workspace_path']}\n"
        f"Base commit: {state['immutable_base_sha']}\n"
        f"Specification: {state['spec_reference']}\n"
        f"Spec digest: {state['spec_digest']}\n"
        f"Repair index: {repair_index} of {state['max_repairs']}\n"
        "Read the specification file and make only the change it describes,\n"
        "inside the workspace. Run the checks it names. Commit the change so the\n"
        "workspace is clean, and report the full candidate commit SHA.\n"
        "Do not push, merge, deploy or promote.\n"
    )


def _assignment_from_state(state: dict, deps: Deps) -> RunAssignment:
    """Rebuild the immutable assignment from persisted state for a resume."""

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


def serialize_state(state: dict) -> str:
    return json.dumps(state, indent=2, sort_keys=True, default=str)
