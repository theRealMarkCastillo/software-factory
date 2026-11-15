"""C5B repair RED: checkpoint ingress, projection provenance and legacy transport."""

import json
import secrets
import sqlite3
from pathlib import Path

import pytest
from factory_orch_fixtures import seed
from test_worker_decisions import MARKER, decision, launch, receipt, result

from factory_orchestration.adapters.hermes import HermesWorker
from factory_orchestration.decisions import sanitize_decision
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import _receipt_payload, thread_id
from factory_orchestration.runner import drive, resume
from factory_orchestration.store import RunStore


@pytest.mark.parametrize(
    "field",
    [
        "spec_ref",
        "spec_excerpt",
        "question",
        "label",
        "consequence",
        "answer_format",
        "extra",
    ],
)
def test_resume_decision_never_writes_raw_checkpoint_bytes(
    tmp_path, monkeypatch, field
):
    s = seed(tmp_path)
    s["worker"].outcomes = ["pending"]
    assert drive(s["deps"], s["assignment"], s["attempt"]).status == "awaiting_worker"
    # Retain a second real connection so the checkpoint WAL/SHM survive saver
    # close and are actually scanned, not merely absent from a glob.
    keeper = sqlite3.connect(s["state_dir"] / "checkpoints.sqlite")
    assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    canary = secrets.token_hex(24)
    monkeypatch.setenv("REPAIR_INGRESS_SECRET", canary)
    d = decision()
    if field in ("label", "consequence"):
        d["options"][0][field] = canary
    else:
        d[field] = canary
    key = s["worker"].submitted[0]
    payload = {
        "receipt": {
            "operation_key": key,
            "attempt": s["attempt"].as_dict(),
            "session_id": "s",
            "outcome": "needs_decision",
            "decision": d,
        }
    }
    thread = thread_id(s["assignment"])
    r = resume(
        s["deps"], thread, s["attempt"], payload=payload, resolved_hold="await_worker"
    )
    assert r.state["last_reason_code"] == (
        "protocol_error" if field == "extra" else "decision_required"
    )
    assert r.stage == "hold_operator"
    assert s["deps"].store.repair_count(thread) == 0
    assert {op["kind"] for op in s["deps"].store.operations(thread)} == {"worker"}
    s["deps"].store = RunStore(s["state_dir"])
    assert drive(s["deps"], s["assignment"], s["attempt"]).stage == "hold_operator"
    database = s["state_dir"] / "checkpoints.sqlite"
    with sqlite3.connect(database) as connection:
        values = connection.execute("SELECT value FROM writes").fetchall()
    leaked_writes = sum(canary.encode() in bytes(row[0]) for row in values)
    leaked_files = [
        path.name
        for path in s["state_dir"].glob("*.sqlite*")
        if canary.encode() in path.read_bytes()
    ]
    assert (s["state_dir"] / "checkpoints.sqlite-wal").exists()
    assert (s["state_dir"] / "checkpoints.sqlite-shm").exists()
    keeper.close()
    # Report locations/counts only, never emit runtime canary values.
    assert (leaked_writes, leaked_files) == (0, [])


@pytest.fixture
def overlapping_secret(monkeypatch):
    canary = secrets.token_hex(24)
    monkeypatch.setenv("REPAIR_SOURCE_SECRET", canary)
    monkeypatch.setenv("REPAIR_MARKER_SECRET", "REDACTED")
    return canary


@pytest.mark.parametrize("serializer", ["as_dict", "graph"])
def test_constructor_projection_serializes_without_redaction_again(
    overlapping_secret, serializer
):
    r = receipt(
        outcome="needs_decision",
        decision={**decision(), "question": overlapping_secret},
    )
    expected = json.dumps(r.decision, sort_keys=True)
    payload = r.as_dict() if serializer == "as_dict" else _receipt_payload(r)
    assert json.dumps(payload["decision"], sort_keys=True) == expected


def test_external_wire_has_indistinguishable_raw_and_sanitized_origins(
    overlapping_secret,
):
    """A passing collision proof: no current wire field can establish provenance.

    The exact bytes exported by one receipt can also be legitimate *raw* input
    from another producer. That raw input contains a real credential substring
    and must not be trusted just because it resembles a replacement marker.
    """
    original = {**decision(), "question": overlapping_secret}
    sanitized = sanitize_decision(original)
    raw_other_producer = json.loads(json.dumps(sanitized))
    assert json.dumps(sanitized) == json.dumps(raw_other_producer)
    assert "REDACTED" in raw_other_producer["question"]
    assert sanitize_decision(raw_other_producer) != sanitized


def test_poll_fresh_probe_stable_but_external_receipt_is_new_raw(
    tmp_path, overlapping_secret
):
    d = {**decision(), "question": overlapping_secret}
    worker, handle = launch(tmp_path, [result(MARKER + "\n" + json.dumps(d))])
    polled = worker.poll(handle).as_dict()
    fresh = HermesWorker(worker.artifact_root).probe("op").receipt
    worker._receipt_path("op").write_text(json.dumps(polled))
    persisted = HermesWorker(worker.artifact_root).probe("op").receipt
    assert json.dumps(polled, sort_keys=True) == json.dumps(fresh, sort_keys=True)
    assert persisted["decision"] == sanitize_decision(polled["decision"])
    assert persisted["decision"] != polled["decision"]


@pytest.mark.parametrize(
    "source", ["external", "external_collision", "typed_adoption", "journal"]
)
def test_engine_journal_replay_is_byte_identical(tmp_path, overlapping_secret, source):
    from factory_orchestration.operations import EffectProbe
    from factory_orchestration.state import WorkerReceipt

    s = seed(tmp_path)
    s["worker"].outcomes = ["pending"]
    drive(s["deps"], s["assignment"], s["attempt"])
    key = s["worker"].submitted[0]
    raw = {**decision(), "question": overlapping_secret}
    typed = WorkerReceipt(key, s["attempt"], "s", "needs_decision", decision=raw)
    expected = sanitize_decision(raw)
    thread = thread_id(s["assignment"])
    if source == "external":
        payload = {"receipt": {**typed.as_dict(), "decision": raw}}
    elif source == "external_collision":
        # Diagnostic projection re-ingressed as JSON is NEW RAW input. Neither
        # caller flags nor marker-looking text can confer internal provenance.
        expected = sanitize_decision(expected)
        payload = {"receipt": {**typed.as_dict(), "already_sanitized": True}}
    elif source == "typed_adoption":
        s["worker"].probe = lambda key: EffectProbe(
            "present", receipt=typed.as_dict(), worker_receipt=typed
        )
        payload = None
    else:
        s["deps"].store.mark_operation(
            key, "confirmed", receipt=_receipt_payload(typed)
        )
        # Restart the interrupted graph using the confirmed canonical journal.
        payload = {"kind": "poll"}
    r = resume(
        s["deps"], thread, s["attempt"], payload=payload, resolved_hold="await_worker"
    )
    assert r.state["last_reason_code"] == "decision_required"
    assert {k: r.state["hold_record"][k] for k in expected} == expected
    held_bytes = json.dumps(r.state["hold_record"], sort_keys=True)
    canonical = json.loads(s["deps"].store.operation(key)["receipt_json"])
    assert canonical["decision"] == expected
    for _ in range(2):
        s["deps"].store = RunStore(s["state_dir"])
        again = drive(s["deps"], s["assignment"], s["attempt"])
        assert json.dumps(again.state["hold_record"], sort_keys=True) == held_bytes


def test_real_producer_all_engine_owned_paths_have_identical_hold_bytes(
    tmp_path, overlapping_secret
):
    from factory_orchestration.graph import _pending_key
    from factory_orchestration.runner import export_run, inspect_run

    raw = {**decision(), "question": overlapping_secret}
    template, _ = launch(tmp_path, [result(MARKER + "\n" + json.dumps(raw))])
    expected = sanitize_decision(raw)
    records = []
    for source in ("poll", "fresh_adopt", "external", "journal"):
        s = seed(tmp_path / source)
        worker = HermesWorker(
            tmp_path / source / "producer", executable=template.executable
        )
        s["deps"].worker = worker
        real_poll = worker.poll
        worker.poll = lambda handle: None  # deterministically suspend the live child
        first = drive(s["deps"], s["assignment"], s["attempt"])
        worker.poll = real_poll
        assert first.status == "awaiting_worker"
        thread = thread_id(s["assignment"])
        key = next(iter(worker._processes))
        worker._processes[key].wait(timeout=10)
        if first.status == "awaiting_worker":
            assert _pending_key(first.state["operations"], "worker") == key
            if source == "fresh_adopt":
                s["deps"].worker = HermesWorker(worker.artifact_root)
                s["deps"].worker.poll = lambda handle: None
                payload = None
            elif source == "external":
                worker.poll = lambda handle: None
                payload = {
                    "receipt": {
                        "operation_key": key,
                        "attempt": s["attempt"].as_dict(),
                        "session_id": "s",
                        "outcome": "needs_decision",
                        "decision": raw,
                    }
                }
            elif source == "journal":
                typed = worker.poll(worker_handle(key))
                assert typed is not None
                s["deps"].store.mark_operation(
                    key, "confirmed", receipt=_receipt_payload(typed)
                )
                s["deps"].worker = HermesWorker(worker.artifact_root)
                payload = None
            else:
                payload = {"kind": "poll"}
            observed = resume(
                s["deps"],
                thread,
                s["attempt"],
                payload=payload,
                resolved_hold="await_worker",
            )
        else:
            observed = first
        assert observed.state["last_reason_code"] == "decision_required"
        hold = observed.state["hold_record"]
        assert {k: hold[k] for k in expected} == expected
        encoded = json.dumps(hold, sort_keys=True)
        records.append(encoded)
        for _ in range(2):
            s["deps"].store = RunStore(s["state_dir"])
            assert (
                json.dumps(
                    inspect_run(s["deps"], thread)["hold_record"], sort_keys=True
                )
                == encoded
            )
            exported = export_run(s["deps"], thread)
            assert exported["state"]["hold_record"] == hold
            assert (
                drive(s["deps"], s["assignment"], s["attempt"]).state["hold_record"]
                == hold
            )
        assert s["deps"].store.repair_count(thread) == 0
        assert {op["kind"] for op in s["deps"].store.operations(thread)} == {"worker"}
    assert len(set(records)) == 1


def worker_handle(key):
    from factory_orchestration.adapters.base import WorkerHandle

    return WorkerHandle(key, "fixture-request")


@pytest.mark.parametrize("malformed", [False, True])
def test_crash_after_safe_ingress_replays_pending_canonical_journal(
    tmp_path, overlapping_secret, monkeypatch, malformed
):
    import factory_orchestration.runner as runner

    s = seed(tmp_path)
    s["worker"].outcomes = ["pending"]
    drive(s["deps"], s["assignment"], s["attempt"])
    key = s["worker"].submitted[0]
    thread = thread_id(s["assignment"])
    raw = {**decision(), "question": overlapping_secret}
    if malformed:
        raw["extra"] = overlapping_secret
    payload = {
        "receipt": {
            "operation_key": key,
            "attempt": s["attempt"].as_dict(),
            "session_id": "s",
            "outcome": "needs_decision",
            "decision": raw,
        }
    }
    real_build = runner.build_graph

    def crashing_build(*args, **kwargs):
        graph = real_build(*args, **kwargs)

        def crash(command, config):
            assert command.resume == (
                {"kind": "worker_protocol_error"}
                if malformed
                else {"worker_journal_key": key}
            )
            raise RuntimeError("injected crash before graph invocation")

        graph.invoke = crash
        return graph

    with monkeypatch.context() as scoped:
        scoped.setattr(runner, "build_graph", crashing_build)
        with pytest.raises(RuntimeError, match="injected crash"):
            resume(
                s["deps"],
                thread,
                s["attempt"],
                payload=payload,
                resolved_hold="await_worker",
            )
    assert s["deps"].store.operation(key)["state"] == "submitted"
    s["deps"].store = RunStore(s["state_dir"])

    def no_source(*args):
        raise AssertionError(
            "canonical decision replay must not probe vanished raw source"
        )

    s["deps"].worker.probe = no_source
    s["deps"].worker.poll = no_source
    r = resume(s["deps"], thread, s["attempt"], resolved_hold="await_worker")
    assert r.state["last_reason_code"] == (
        "protocol_error" if malformed else "decision_required"
    )
    if not malformed:
        assert {k: r.state["hold_record"][k] for k in raw} == sanitize_decision(raw)
    assert len(s["worker"].submitted) == 1


def test_malformed_resume_cannot_be_overridden_by_completed_poll(tmp_path):
    s = seed(tmp_path)
    s["worker"].outcomes = ["pending", "completed"]
    drive(s["deps"], s["assignment"], s["attempt"])
    key = s["worker"].submitted[0]
    s["worker"]._receipt_path(key).parent.mkdir(parents=True, exist_ok=True)
    s["worker"]._receipt_path(key).write_text(
        json.dumps(
            {
                "operation_key": key,
                "attempt": s["attempt"].as_dict(),
                "session_id": "s",
                "outcome": "completed",
                "candidate_sha": s["candidate"],
            }
        )
    )
    payload = {
        "receipt": {
            "operation_key": key,
            "attempt": s["attempt"].as_dict(),
            "outcome": "needs_decision",
            "decision": {**decision(), "extra": "invalid"},
        }
    }
    r = resume(
        s["deps"],
        thread_id(s["assignment"]),
        s["attempt"],
        payload=payload,
        resolved_hold="await_worker",
    )
    assert r.state["last_reason_code"] == "protocol_error"
    assert r.stage == "hold_operator"
    assert {op["kind"] for op in s["deps"].store.operations(r.thread_id)} == {"worker"}
    assert s["deps"].store.repair_count(r.thread_id) == 0


def test_internal_receipt_owns_detached_immutable_decision(overlapping_secret):
    raw = {**decision(), "question": overlapping_secret}
    r = receipt(outcome="needs_decision", decision=raw)
    expected = json.dumps(r.as_dict(), sort_keys=True)
    raw["options"][0]["label"] = "producer changed"
    exported = r.as_dict()
    exported["decision"]["options"][0]["label"] = "caller changed"
    with pytest.raises(TypeError):
        r.decision["question"] = "changed"
    with pytest.raises(TypeError):
        r.decision["options"][0]["label"] = "changed"
    with pytest.raises(TypeError):
        r.decision["options"].append({})
    assert json.dumps(r.as_dict(), sort_keys=True) == expected


@pytest.mark.parametrize("new_reason", ["protocol_error", "decision_required"])
def test_only_current_decision_hold_is_preserved(tmp_path, new_reason):
    from factory_orchestration.graph import build_graph
    from factory_orchestration.state import initial_state

    s = seed(tmp_path)
    old = {
        "stage": "await_worker",
        "reason": "decision_required",
        "detail": "worker requires a decision",
        **decision(),
    }
    state = {
        **initial_state(s["assignment"], s["attempt"], owner_token="fixture-owner"),
        "stage": "await_worker",
        "route": "hold_operator",
        "last_reason_code": new_reason,
        "last_reason_detail": "new reason",
        "hold_record": old,
    }
    graph = build_graph(s["deps"])
    classified = graph.nodes["classify"].bound.invoke(state)
    held = graph.nodes["hold_operator"].bound.invoke({**state, **classified})
    assert held["hold_record"]["reason"] == new_reason
    if new_reason == "decision_required":
        assert held["hold_record"] == old
    else:
        assert held["hold_record"]["question"] == ""


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("sidechannel", ["invalid", "duplicates", "nan"])
def test_no_decision_sidechannel_preserves_legacy_completion(
    tmp_path, fresh, sidechannel
):
    s = seed(tmp_path / "subject")
    worker, handle = launch(tmp_path, [result("normal final")])
    record = worker._records["op"]
    record.update(workspace=str(s["workspace"]), base_sha=s["base"])
    request_path = worker._request_path("op")
    request = json.loads(request_path.read_text())
    request.update(workspace=record["workspace"], base_sha=record["base_sha"])
    request_path.write_text(json.dumps(request))
    payload = {
        "operation_key": "op",
        "attempt": record["attempt"],
        "outcome": "completed",
        "candidate_sha": s["candidate"],
    }
    if sidechannel == "invalid":
        raw = "not JSON"
    elif sidechannel == "duplicates":
        raw = json.dumps(payload).replace(
            '"outcome":', '"outcome":"failed", "outcome":'
        )
    else:
        raw = json.dumps({**payload, "ignored_metric": float("nan")})
    Path(record["result_path"]).write_text(raw)
    observed = (
        HermesWorker(worker.artifact_root).probe("op").receipt
        if fresh
        else worker.poll(handle).as_dict()
    )
    assert observed["outcome"] == "completed"
    assert observed["candidate_sha"] == s["candidate"]


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("hidden", ["decision", "outcome"])
def test_duplicate_keys_cannot_hide_actual_decision_protocol(tmp_path, fresh, hidden):
    worker, handle = launch(tmp_path, [result("normal final")])
    payload = {
        "attempt": worker._records["op"]["attempt"],
        "outcome": "needs_decision",
        "decision": decision(),
    }
    raw = json.dumps(payload)
    if hidden == "decision":
        raw = raw[:-1] + ', "decision":null}'
    else:
        raw = raw[:-1] + ', "outcome":"completed", "decision":null}'
    Path(worker._records["op"]["result_path"]).write_text(raw)
    with pytest.raises(OrchestrationError) as exc:
        HermesWorker(worker.artifact_root).probe("op") if fresh else worker.poll(handle)
    assert exc.value.reason == "protocol_error"
