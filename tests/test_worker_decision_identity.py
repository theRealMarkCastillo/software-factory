"""Incomplete decision identities never acquire canonical mutation authority."""

import json
import secrets
import sqlite3
from copy import deepcopy

import pytest
from test_worker_decision_refusal import incoming, invoke_crash, no_progress, pending

from factory_orchestration import runner
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.store import RunStore

# Each JSON field class is explicit: omission is different from an allowed null
# diagnostic lock. No string/float/bool run-id coercion is a decision identity.
OMITTED = object()
BAD_STRING = [
    ("omitted", OMITTED),
    ("null", None),
    ("bool", True),
    ("integer", 7),
    ("float", 7.5),
    ("list", []),
    ("object", {}),
    ("empty", ""),
    ("whitespace", " "),
    ("invalid_slug", "not/a/slug"),
]
CASES = (
    [
        (f"{field}-{label}", field, value)
        for field in ("task_id", "profile")
        for label, value in BAD_STRING
    ]
    + [
        (f"run_id-{label}", "run_id", value)
        for label, value in [
            ("omitted", OMITTED),
            ("null", None),
            ("bool", True),
            ("false", False),
            ("string_integer", "7"),
            ("empty", ""),
            ("whitespace", " "),
            ("float_integer", 7.0),
            ("float_fraction", 7.5),
            ("zero", 0),
            ("negative", -1),
            ("list", []),
            ("object", {}),
        ]
    ]
    + [
        (f"claim_lock-{label}", "claim_lock", value)
        for label, value in [
            ("omitted", OMITTED),
            ("bool", True),
            ("integer", 7),
            ("float", 7.5),
            ("list", []),
            ("object", {}),
        ]
    ]
    + [
        (f"attempt-{label}", "attempt", value)
        for label, value in [
            ("omitted", OMITTED),
            ("null", None),
            ("bool", True),
            ("integer", 7),
            ("float", 7.5),
            ("string", "identity"),
            ("empty_string", ""),
            ("list", []),
            ("empty_object", {}),
        ]
    ]
    + [
        ("wrong_task_missing_run", "conflict", "task"),
        ("wrong_profile_missing_lock", "conflict", "profile"),
        ("wrong_op_malformed_attempt", "conflict", "op"),
        ("wrong_task", "conflict", "complete_task"),
        ("wrong_profile", "conflict", "complete_profile"),
        ("reference", "conflict", "reference"),
    ]
)


def malformed(s, field, value):
    payload = deepcopy(incoming(s, "raw_current"))
    raw = payload["receipt"]
    if field == "conflict":
        if value == "task":
            raw["attempt"]["task_id"] = "t_deadbeef"
            del raw["attempt"]["run_id"]
        elif value == "profile":
            raw["attempt"]["profile"] = "other-profile"
            del raw["attempt"]["claim_lock"]
        elif value == "op":
            raw["operation_key"] = "unrelated-worker-operation"
            raw["attempt"] = []
        elif value == "complete_task":
            raw["attempt"]["task_id"] = "t_deadbeef"
        elif value == "complete_profile":
            raw["attempt"]["profile"] = "other-profile"
        else:
            return {"worker_journal_key": s["key"], "already_sanitized": True}
    else:
        target = raw if field == "attempt" else raw["attempt"]
        if value is OMITTED:
            del target[field]
        else:
            target[field] = value
    return payload


@pytest.mark.parametrize("poll", ["pending", "completed"])
@pytest.mark.parametrize("canonical", ["absent", "valid", "static_refusal"])
@pytest.mark.parametrize("case,field,value", CASES, ids=[c[0] for c in CASES])
def test_unbound_identity_preserves_whole_row_before_graph(
    tmp_path, monkeypatch, poll, canonical, case, field, value
):
    s = pending(tmp_path, poll)
    if canonical != "absent":
        invoke_crash(
            s,
            monkeypatch,
            incoming(s, "raw_current" if canonical == "valid" else "malformed_current"),
        )
    # Reopen the engine store after supported ingress/crash, not DB tampering.
    s["deps"].store = RunStore(s["state_dir"])
    before = s["deps"].store.operation(s["key"])
    receipt_bytes = before["receipt_json"]
    payload = malformed(s, field, value)
    canary = secrets.token_hex(24)
    if "receipt" in payload:
        payload["receipt"]["decision"]["question"] = canary
    build = runner.build_graph
    invocations = []

    def guarded(*args, **kwargs):
        graph = build(*args, **kwargs)

        def forbidden(*args, **kwargs):
            invocations.append(True)
            raise AssertionError("unbound identity reached graph invocation")

        graph.invoke = forbidden
        return graph

    with monkeypatch.context() as scoped:
        scoped.setattr(runner, "build_graph", guarded)
        with pytest.raises(OrchestrationError) as exc:
            runner.resume(
                s["deps"],
                s["thread"],
                s["attempt"],
                payload=payload,
                resolved_hold="await_worker",
            )
    assert exc.value.reason == "protocol_error"
    assert str(exc.value) == "protocol_error: invalid worker decision"
    assert not invocations
    after = s["deps"].store.operation(s["key"])
    assert after == before
    assert after["receipt_json"] == receipt_bytes
    with sqlite3.connect(s["state_dir"] / "checkpoints.sqlite") as connection:
        assert all(
            canary.encode() not in bytes(row[0])
            for row in connection.execute("SELECT value FROM writes")
        )
    assert all(
        canary.encode() not in p.read_bytes() for p in s["state_dir"].glob("*.sqlite*")
    )
    no_progress(s)


@pytest.mark.parametrize("lock", [None, "", "different-diagnostic-lock"])
@pytest.mark.parametrize("kind", ["raw_current", "raw_stale", "malformed_current"])
def test_complete_identity_preserves_nullable_diagnostic_lock_semantics(
    tmp_path, monkeypatch, lock, kind
):
    s = pending(tmp_path, "completed")
    payload = incoming(s, kind)
    payload["receipt"]["attempt"]["claim_lock"] = lock
    invoke_crash(s, monkeypatch, payload)
    record = s["deps"].store.operation(s["key"])
    if kind == "raw_current":
        assert json.loads(record["receipt_json"])["attempt"]["claim_lock"] == lock
    s["deps"].store = RunStore(s["state_dir"])
    observed = runner.resume(
        s["deps"], s["thread"], s["attempt"], resolved_hold="await_worker"
    )
    assert observed.stage == "hold_operator"
    assert (
        observed.state["last_reason_code"]
        == {
            "raw_current": "decision_required",
            "raw_stale": "stale_attempt",
            "malformed_current": "protocol_error",
        }[kind]
    )
    no_progress(s)
