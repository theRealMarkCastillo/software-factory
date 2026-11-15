"""Decision binding and refusal precedence at the supported resume boundary."""

import json
import secrets
import sqlite3
import sys
from dataclasses import replace

import pytest
from factory_orch_fixtures import seed
from test_worker_decisions import decision, result

from factory_orchestration import runner
from factory_orchestration.adapters.base import WorkerHandle
from factory_orchestration.adapters.hermes import HermesWorker
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import thread_id
from factory_orchestration.operations import _WorkerResume
from factory_orchestration.state import WorkerReceipt
from factory_orchestration.store import RunStore


def pending(tmp_path, poll):
    s = seed(tmp_path)
    executable = tmp_path / "inert-worker"
    executable.write_text(
        f"#!{sys.executable}\nprint({json.dumps(json.dumps(result('ordinary completion')))})\n"
    )
    executable.chmod(0o755)
    worker = HermesWorker(tmp_path / "producer", executable=str(executable))
    s["deps"].worker = worker
    actual_poll = worker.poll
    worker.poll = lambda handle: None
    first = runner.drive(s["deps"], s["assignment"], s["attempt"])
    assert first.status == "awaiting_worker"
    key = next(iter(worker._processes))
    worker._processes[key].wait(timeout=10)
    worker.poll = actual_poll
    assert actual_poll(WorkerHandle(key, "fixture")).outcome == "completed"
    if poll == "pending":
        worker.poll = lambda handle: None
    s.update(real_worker=worker, key=key, thread=thread_id(s["assignment"]))
    return s


def incoming(s, kind, d=None):
    key = s["key"]
    attempt = s["attempt"]
    if "stale" in kind:
        attempt = replace(attempt, run_id=attempt.run_id - 1)
    if "wrong_op" in kind:
        key = "unrelated-worker-operation"
    raw = {
        "operation_key": key,
        "attempt": attempt.as_dict(),
        "session_id": "fixture",
        "outcome": "needs_decision",
        "decision": decision() if d is None else d,
    }
    if kind == "malformed_current":
        raw["decision"]["extra"] = "invalid"
    if kind.startswith("reference"):
        return {"worker_journal_key": key, "already_sanitized": True}
    if kind == "missing_binding":
        del raw["operation_key"]
    if kind == "wrong_task":
        raw["attempt"]["task_id"] = "t_deadbeef"
    if kind.startswith("typed"):
        return _WorkerResume(WorkerReceipt(**{**raw, "attempt": attempt}))
    return {"receipt": raw}


def invoke_crash(s, monkeypatch, payload):
    build = runner.build_graph

    def crashing_build(*args, **kwargs):
        graph = build(*args, **kwargs)

        def crash(command, config):
            assert command.resume in (
                {"worker_journal_key": s["key"]},
                {"kind": "worker_protocol_error"},
                {"kind": "worker_stale_attempt"},
            )
            raise RuntimeError("injected pre-invoke crash")

        graph.invoke = crash
        return graph

    with monkeypatch.context() as scoped:
        scoped.setattr(runner, "build_graph", crashing_build)
        with pytest.raises(RuntimeError, match="injected pre-invoke crash"):
            runner.resume(
                s["deps"],
                s["thread"],
                s["attempt"],
                payload=payload,
                resolved_hold="await_worker",
            )
    assert s["deps"].store.operation(s["key"])["state"] == "submitted"


def no_progress(s):
    assert s["deps"].store.repair_count(s["thread"]) == 0
    operations = s["deps"].store.operations(s["thread"])
    assert len(operations) == 1
    assert operations[0]["kind"] == "worker"
    assert len(s["real_worker"]._processes) == 1


KINDS = (
    "raw_current",
    "typed_current",
    "raw_stale",
    "typed_stale",
    "raw_wrong_op",
    "typed_wrong_op",
    "malformed_current",
    "reference_current",
    "reference_wrong_op",
    "missing_binding",
    "wrong_task",
)
UNBOUND = {
    "raw_wrong_op",
    "typed_wrong_op",
    "reference_current",
    "reference_wrong_op",
    "missing_binding",
    "wrong_task",
}


@pytest.mark.parametrize("poll", ["pending", "completed"])
@pytest.mark.parametrize("canonical", ["absent", "valid", "static_refusal"])
@pytest.mark.parametrize("kind", KINDS)
def test_binding_precedes_mutation_and_bound_refusal_precedes_success(
    tmp_path, monkeypatch, poll, canonical, kind
):
    s = pending(tmp_path, poll)
    if canonical != "absent":
        invoke_crash(
            s,
            monkeypatch,
            incoming(s, "raw_current" if canonical == "valid" else "malformed_current"),
        )
    before = s["deps"].store.operation(s["key"])
    payload = incoming(s, kind)
    if kind in UNBOUND:
        # Invalid external identities must not even enter graph invocation.
        def forbidden(*args, **kwargs):
            raise AssertionError("unbound decision reached graph invocation")

        with monkeypatch.context() as scoped:
            build = runner.build_graph

            def guarded(*args, **kwargs):
                graph = build(*args, **kwargs)
                graph.invoke = forbidden
                return graph

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
        assert s["deps"].store.operation(s["key"]) == before
    else:
        observed = runner.resume(
            s["deps"],
            s["thread"],
            s["attempt"],
            payload=payload,
            resolved_hold="await_worker",
        )
        expected = (
            "stale_attempt"
            if "stale" in kind
            else "protocol_error"
            if kind == "malformed_current" or canonical == "static_refusal"
            else "decision_required"
        )
        assert observed.stage == "hold_operator"
        assert observed.state["last_reason_code"] == expected
        if expected == "decision_required":
            assert observed.state["hold_record"]["question"] == decision()["question"]
            if canonical == "valid":
                assert (
                    s["deps"].store.operation(s["key"])["receipt_json"]
                    == before["receipt_json"]
                )
    no_progress(s)


@pytest.mark.parametrize("stale", [False, True])
def test_fresh_standalone_probe_refusal_wins_actual_completed_live_poll(
    tmp_path, stale
):
    s = pending(tmp_path, "completed")
    raw = incoming(s, "raw_stale" if stale else "raw_current")["receipt"]
    path = s["real_worker"]._receipt_path(s["key"])
    path.write_text(json.dumps(raw))
    fresh = HermesWorker(s["real_worker"].artifact_root)
    probe = fresh.probe(s["key"])
    assert probe.worker_receipt.attempt.run_id == (6 if stale else 7)
    # Keep the real live adapter's completed poll; only probe through a new adapter.
    s["real_worker"].probe = fresh.probe
    observed = runner.resume(
        s["deps"], s["thread"], s["attempt"], resolved_hold="await_worker"
    )
    assert observed.stage == "hold_operator"
    assert observed.state["last_reason_code"] == (
        "stale_attempt" if stale else "decision_required"
    )
    no_progress(s)


@pytest.mark.parametrize(
    "kind", ["raw_current", "malformed_current", "raw_stale", "typed_stale"]
)
def test_preinvoke_crash_reopens_without_source_or_raw_checkpoint_payload(
    tmp_path, monkeypatch, kind
):
    s = pending(tmp_path, "completed")
    canary = secrets.token_hex(24)
    monkeypatch.setenv("REFUSAL_TEST_SECRET", canary)
    payload = incoming(s, kind, {**decision(), "question": canary})
    invoke_crash(s, monkeypatch, payload)
    record = s["deps"].store.operation(s["key"])
    expected = (
        "stale_attempt"
        if "stale" in kind
        else "protocol_error"
        if kind == "malformed_current"
        else "decision_required"
    )
    if "stale" in kind:
        assert json.loads(record["receipt_json"]) == {
            "operation_key": s["key"],
            "kind": "worker_stale_attempt",
        }
    # Raw producer data is no longer available. A completed live poll remains
    # available, but recovery must not consult it or the vanished raw source.
    for path in s["real_worker"]._dir(s["key"]).iterdir():
        if path.is_file():
            path.unlink()

    def forbidden(*args):
        raise AssertionError("recovery consulted producer instead of canonical journal")

    s["real_worker"].probe = forbidden
    s["real_worker"].poll = forbidden
    s["deps"].store = RunStore(s["state_dir"])
    observed = runner.resume(
        s["deps"], s["thread"], s["attempt"], resolved_hold="await_worker"
    )
    assert observed.stage == "hold_operator"
    assert observed.state["last_reason_code"] == expected
    with sqlite3.connect(s["state_dir"] / "checkpoints.sqlite") as connection:
        assert all(
            canary.encode() not in bytes(row[0])
            for row in connection.execute("SELECT value FROM writes")
        )
    assert all(
        canary.encode() not in p.read_bytes() for p in s["state_dir"].glob("*.sqlite*")
    )
    no_progress(s)
