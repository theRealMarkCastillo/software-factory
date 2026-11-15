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
        for path in s["state_dir"].glob("checkpoints.sqlite*")
        if canary.encode() in path.read_bytes()
    ]
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


def test_poll_serialized_receipt_fresh_probe_is_byte_identical(
    tmp_path, overlapping_secret
):
    d = {**decision(), "question": overlapping_secret}
    worker, handle = launch(tmp_path, [result(MARKER + "\n" + json.dumps(d))])
    polled = worker.poll(handle).as_dict()
    fresh = HermesWorker(worker.artifact_root).probe("op").receipt
    worker._receipt_path("op").write_text(json.dumps(polled))
    persisted = HermesWorker(worker.artifact_root).probe("op").receipt
    assert json.dumps(polled, sort_keys=True) == json.dumps(fresh, sort_keys=True)
    assert json.dumps(polled, sort_keys=True) == json.dumps(persisted, sort_keys=True)


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
