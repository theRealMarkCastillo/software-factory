"""C5A: deterministic engine holds; offline graphs and durable checkpoints."""

from __future__ import annotations

import json
import secrets

import pytest
from factory_orch_fixtures import seed

from factory_orchestration.adapters.base import CancelReport
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import build_graph, thread_id
from factory_orchestration.operations import EffectProbe, digest_parts
from factory_orchestration.runner import (
    _checkpoint_saver,
    assignment_from_state,
    cancel,
    current_state,
    drive,
    export_run,
    inspect_run,
    next_action,
    resume,
)
from factory_orchestration.state import initial_state
from factory_orchestration.store import RunStore

FIELDS = {
    "stage",
    "reason",
    "detail",
    "spec_ref",
    "spec_excerpt",
    "question",
    "options",
    "recommendation",
    "answer_format",
}


def expected(state, stage):
    return {
        "stage": (state.get("repair_context") or {}).get("source") or stage,
        "reason": state["last_reason_code"],
        "detail": state.get("last_reason_detail", ""),
        "spec_ref": state["spec_reference"],
        "spec_excerpt": "",
        "question": "",
        "options": [],
        "recommendation": "",
        "answer_format": "",
    }


def assert_record(seeded, state, stage, *, event=True):
    record = state["hold_record"]
    assert set(record) == FIELDS
    assert record == expected(state, stage)
    thread = thread_id(seeded["assignment"])
    assert current_state(seeded["deps"], thread)["hold_record"] == record
    assert inspect_run(seeded["deps"], thread)["hold_record"] == record
    assert export_run(seeded["deps"], thread)["state"]["hold_record"] == record
    if event:
        held = [e for e in seeded["deps"].store.events(thread) if e["kind"] == "held"]
        assert held[-1]["detail"]["hold_record"] == record
        assert held[-1]["detail"]["reason"] == state["last_reason_code"]
    return record


@pytest.mark.parametrize(
    "reason,node",
    [
        ("budget_exhausted", "hold_budget"),
        ("worker_unavailable", "hold_infrastructure"),
        ("effect_unknown", "hold_reconciliation"),
        ("protocol_error", "hold_operator"),
    ],
)
def test_every_hold_route_persists_record_and_restart(tmp_path, reason, node):
    seeded = seed(tmp_path)

    def fail(request):
        raise OrchestrationError(reason, "diagnostic detail")

    seeded["worker"].submit = fail
    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert result.status == "held"
    assert result.stage == node
    record = assert_record(seeded, result.state, "request_implementation")
    seeded["deps"].store = RunStore(seeded["state_dir"])
    again = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert again.state["hold_record"] == record
    assert again.state["repair_count"] == 0


@pytest.mark.parametrize("case", ["budget", "not_converging", "bad_tree", "fallback"])
def test_classifier_hold_entries_preserve_context(tmp_path, case):
    seeded = seed(tmp_path)
    deps = seeded["deps"]
    state = initial_state(seeded["assignment"], seeded["attempt"], owner_token="test")
    context = {"source": "acceptance", "detail": "original failure"}
    state.update(
        stage="run_acceptance",
        route="repair_or_hold",
        max_repairs=0,
        last_reason_code="evidence_rejected",
        last_reason_detail="failed",
        repair_context=context,
    )
    if case == "not_converging":
        from factory_orchestration.graph import _failure_signature

        state["previous_failure_signature"] = _failure_signature(context)
    elif case == "bad_tree":
        state["repair_context"] = {**context, "previous_candidate_sha": "f" * 40}
    elif case == "fallback":
        state.update(route="unexpected", repair_context={})
    thread = thread_id(seeded["assignment"])
    deps.store.record_admission(thread, seeded["assignment"], seeded["attempt"])
    before = dict(state["repair_context"])
    with _checkpoint_saver(deps.store) as saver:
        graph = build_graph(deps, saver)
        config = {"configurable": {"thread_id": thread}}
        graph.update_state(config, state, as_node="run_acceptance")
        result = graph.invoke(None, config)
        # Every intermediate status=held checkpoint must already carry the record.
        for snapshot in graph.get_state_history(config):
            if snapshot.values.get("status") == "held":
                assert "hold_record" in snapshot.values
    assert result["status"] == "held"
    assert result["repair_context"] == before
    assert_record(seeded, result, "run_acceptance")
    assert deps.store.repair_count(thread) == 0


def test_cancellation_reconciliation_hold_has_record(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    paused = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert paused.awaiting
    seeded["worker"].cancel = lambda handle: CancelReport(
        handle.operation_key, False, "not confirmed"
    )
    seeded["worker"].probe = lambda key: EffectProbe("unknown", detail="unknown")
    thread = thread_id(seeded["assignment"])
    result = cancel(seeded["deps"], thread, by="operator")
    assert result["status"] == "held"
    state = current_state(seeded["deps"], thread)
    assert state["last_reason_code"] == "cancellation_pending"
    assert_record(seeded, state, "cancel_owned_operations")


def test_legacy_held_inspection_synthesizes_without_writes(tmp_path):
    seeded = seed(tmp_path)
    deps = seeded["deps"]
    thread = thread_id(seeded["assignment"])
    state = initial_state(seeded["assignment"], seeded["attempt"], owner_token="test")
    state.update(
        status="held",
        stage="hold_operator",
        route="end",
        last_reason_code="protocol_error",
        last_reason_detail="legacy",
    )
    deps.store.record_admission(thread, seeded["assignment"], seeded["attempt"])
    with _checkpoint_saver(deps.store) as saver:
        graph = build_graph(deps, saver)
        graph.update_state(
            {"configurable": {"thread_id": thread}}, state, as_node="hold_operator"
        )
    deps.store.set_status(thread, "held")
    before = (deps.store.db_path.read_bytes(), deps.store.checkpoint_path.read_bytes())
    view = inspect_run(deps, thread)
    assert view["hold_record"] == expected(state, "hold_operator")
    assert "hold_record" not in view["state"]
    assert view["next_action"] == next_action(state, view)
    assert (
        deps.store.db_path.read_bytes(),
        deps.store.checkpoint_path.read_bytes(),
    ) == before
    # A legacy terminal checkpoint still drives without a schema migration.
    result = drive(deps, seeded["assignment"], seeded["attempt"])
    assert result.status == "held"


def test_nonheld_inspection_and_assignment_digest_unchanged(tmp_path):
    seeded = seed(tmp_path)
    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    thread = thread_id(seeded["assignment"])
    original = seeded["deps"].store.inspect_view(thread, result.state)
    original["next_action"] = next_action(result.state, original)
    assert json.dumps(
        inspect_run(seeded["deps"], thread), sort_keys=True
    ) == json.dumps(original, sort_keys=True)
    assert "hold_record" not in result.state
    assignment = assignment_from_state(result.state)
    enriched = assignment_from_state(
        {
            **result.state,
            "hold_record": expected(
                {**result.state, "last_reason_code": "test"}, "test"
            ),
        }
    )
    assert assignment.as_dict() == enriched.as_dict()
    assert assignment.binding() == enriched.binding()
    assert digest_parts(assignment.as_dict()) == digest_parts(enriched.as_dict())


def test_legacy_wait_checkpoint_resumes_without_record(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert "hold_record" not in result.state
    finished = resume(
        seeded["deps"],
        thread_id(seeded["assignment"]),
        seeded["attempt"],
        resolved_hold="worker wait",
        payload={
            "receipt": {
                "operation_key": result.awaiting["operation_key"],
                "outcome": "completed",
                "candidate_sha": seeded["candidate"],
            }
        },
    )
    assert finished.status == "ready_for_authorized_completion"


@pytest.mark.parametrize(
    "field,limit",
    [("detail", 4096), ("spec_ref", 1024), ("stage", 128), ("reason", 128)],
)
def test_hold_fields_redact_before_utf8_bound(field, limit, monkeypatch):
    from factory_orchestration.holds import hold_record

    canary = secrets.token_hex(24)
    monkeypatch.setenv("C5_TEST_SECRET", canary)
    # Put the credential across the byte limit: bounding first would leak a prefix.
    value = "界" * ((limit - 12) // 3) + canary + '\n"\\' * 2000
    state = {
        "stage": "bind_candidate",
        "last_reason_code": "protocol_error",
        "last_reason_detail": "detail",
        "spec_reference": "spec",
    }
    state[
        {
            "detail": "last_reason_detail",
            "spec_ref": "spec_reference",
            "stage": "stage",
            "reason": "last_reason_code",
        }[field]
    ] = value
    record = hold_record(state)
    assert len(record[field].encode("utf-8")) <= limit
    assert canary not in json.dumps(record)
    assert canary[:12] not in record[field]
    assert json.loads(json.dumps(record, ensure_ascii=True)) == record
    assert (
        record["question"] == record["recommendation"] == record["answer_format"] == ""
    )
    assert record["options"] == []


def test_canary_absent_from_persisted_hold_outputs(tmp_path, monkeypatch):
    seeded = seed(tmp_path)
    canary = secrets.token_hex(24)
    monkeypatch.setenv("C5_TEST_SECRET", canary)
    # No spec-path admission change: invoke graph directly with synthetic diagnostic data.
    state = initial_state(seeded["assignment"], seeded["attempt"], owner_token="test")
    state.update(
        stage="bind_candidate",
        route="hold_operator",
        status="running",
        spec_reference="/spec/" + canary,
        last_reason_code="protocol_error",
        last_reason_detail="failure " + canary,
    )
    deps = seeded["deps"]
    thread = thread_id(seeded["assignment"])
    deps.store.record_admission(thread, seeded["assignment"], seeded["attempt"])
    with _checkpoint_saver(deps.store) as saver:
        graph = build_graph(deps, saver)
        config = {"configurable": {"thread_id": thread}}
        graph.update_state(config, state, as_node="bind_candidate")
        graph.invoke(None, config)
    record = current_state(deps, thread)["hold_record"]
    assert "[REDACTED:C5_TEST_SECRET]" in record["detail"]
    assert "[REDACTED:C5_TEST_SECRET]" in record["spec_ref"]
    # Existing assignment/state reference is not this card's redaction scope.
    # All NEW record projections, including JSON-escaped exports, must be safe.
    outputs = [
        record,
        inspect_run(deps, thread)["hold_record"],
        export_run(deps, thread)["state"]["hold_record"],
        [e for e in deps.store.events(thread) if e["kind"] == "held"],
    ]
    for output in outputs:
        assert canary not in json.dumps(output, ensure_ascii=True)
