"""Closed worker decision contract: real transports, safe receipts and graph holds."""

import copy
import json
import sys
from dataclasses import asdict

import pytest
from factory_orch_fixtures import seed

from factory_orchestration.adapters.hermes import HermesWorker
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import _receipt_payload
from factory_orchestration.graph import thread_id
from factory_orchestration.runner import export_run, inspect_run, drive
from factory_orchestration.state import Attempt, WorkerReceipt, WorkerRequest

MARKER = "FACTORY_NEEDS_DECISION_V1"


def decision():
    return {
        "spec_ref": "SPEC.md#storage",
        "spec_excerpt": "Choose storage before migration.",
        "question": "Which storage contract?",
        "options": [{"id": "A", "label": "Local", "consequence": "No remote sync"}],
        "recommendation": "A",
        "answer_format": "Reply A",
    }


def receipt(**kwargs):
    return WorkerReceipt("op", Attempt("t_abcd1234", 7, "factory-runner"), "s", **kwargs)


def test_decision_receipt_and_legacy_serialization():
    r = receipt(outcome="needs_decision", decision=decision())
    assert r.as_dict()["decision"] == decision()
    assert _receipt_payload(r)["decision"] == decision()
    for outcome in ("completed", "failed", "unavailable", "ambiguous", "cancelled"):
        r = receipt(outcome=outcome, candidate_sha="a" * 40 if outcome == "completed" else None)
        expected = asdict(r)
        expected.pop("decision", None)
        assert r.as_dict() == expected
        expected["artifact_references"] = []
        expected["reported_tests"] = []
        assert _receipt_payload(r) == expected


def invalid_decisions():
    values = [None, [], True, {}, {**decision(), "extra": "x"}]
    for key in decision():
        d = decision()
        del d[key]
        values.append(d)
    for key in ("spec_ref", "spec_excerpt", "question", "recommendation", "answer_format"):
        for value in (False, 1, None, [], "\ud800"):
            values.append({**decision(), key: value})
    for key in ("question", "answer_format"):
        values.append({**decision(), key: " \n"})
    for options in ([], [decision()["options"][0]] * 5, {}, [False]):
        values.append({**decision(), "options": options})
    for key in ("id", "label", "consequence"):
        for value in (None, False, 3, "\udfff", ""):
            d = decision()
            d["options"][0][key] = value
            values.append(d)
    for value in ("a b", "a" * 17, "é"):
        d = decision()
        d["options"][0]["id"] = value
        values.append(d)
    for option in ({"id": "A", "label": "L"}, {**decision()["options"][0], "extra": "x"}):
        values.append({**decision(), "options": [option]})
    values += [
        {**decision(), "options": decision()["options"] * 2},
        {**decision(), "recommendation": "B"},
        {**decision(), "question": "x" * 17000},
        {**decision(), "question": float("nan")},
    ]
    return values


@pytest.mark.parametrize("value", invalid_decisions())
def test_invalid_decisions_are_constant_protocol_errors(value):
    with pytest.raises(OrchestrationError) as exc:
        receipt(outcome="needs_decision", decision=value)
    assert exc.value.reason == "protocol_error"
    assert exc.value.detail == "invalid worker decision"


@pytest.mark.parametrize("outcome,candidate", [("completed", "a" * 40), ("failed", None), ("needs_decision", "a" * 40)])
def test_decision_outcome_binding(outcome, candidate):
    with pytest.raises(OrchestrationError) as exc:
        receipt(outcome=outcome, candidate_sha=candidate, decision=decision())
    assert exc.value.reason == "protocol_error"


def result(text, exit_code=0):
    return {"type": "result", "session_id": "session", "exit_code": exit_code,
            "text": text, "tokens": 12, "duration_ms": 10, "timestamp": "2026-01-01T00:00:00Z"}


def launch(tmp_path, events, *, exit_code=0):
    executable = tmp_path / "fake-hermes"
    executable.write_text(f"#!{sys.executable}\nimport json\n" + "\n".join(f"print({json.dumps(json.dumps(e))})" for e in events) + f"\nraise SystemExit({exit_code})\n")
    executable.chmod(0o755)
    worker = HermesWorker(tmp_path / "effects", executable=str(executable))
    request = WorkerRequest("op", Attempt("t_abcd1234", 7, "factory-runner"), "worker", "sha256:" + "a" * 64,
                            str(tmp_path), "a" * 40, (str(tmp_path),), 0, 30, str(tmp_path), "bounded")
    handle = worker.submit(request).handle
    worker._processes["op"].wait(timeout=10)
    return worker, handle


@pytest.mark.parametrize("fresh", [False, True])
def test_real_process_final_decision_transport(tmp_path, fresh):
    text = MARKER + "\n" + json.dumps(decision())
    worker, handle = launch(tmp_path, [result(text)])
    if fresh:
        payload = HermesWorker(worker.artifact_root).probe("op").receipt
    else:
        payload = worker.poll(handle).as_dict()
    assert payload["outcome"] == "needs_decision"
    assert payload["decision"] == decision()
    assert payload["candidate_sha"] is None
    assert payload["attempt"] == worker._records["op"]["attempt"]


BAD_TEXTS = [
    MARKER, MARKER + "\n", "prose\n" + MARKER + "\n{}",
    "```\n" + MARKER + "\n{}\n```", MARKER + " {}", MARKER + "\r\n{}",
    MARKER + "\n{}\n" + MARKER, MARKER + "\n{} trailing",
    MARKER + "\n" + json.dumps(decision()).replace('"question":', '"question":"other", "question":'),
    MARKER + "\n" + json.dumps(decision()).replace('"question": "Which storage contract?"', '"question": NaN'),
    MARKER + "\n" + json.dumps({**decision(), "question": "x" * 17000}),
]


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("text", BAD_TEXTS)
def test_invalid_marker_never_falls_back(tmp_path, fresh, text):
    worker, handle = launch(tmp_path, [result(text)])
    with pytest.raises(OrchestrationError) as exc:
        if fresh:
            HermesWorker(worker.artifact_root).probe("op")
        else:
            worker.poll(handle)
    assert exc.value.reason == "protocol_error"
    assert text not in str(exc.value)


@pytest.mark.parametrize("fresh", [False, True])
def test_last_result_only_and_nonzero_exit(tmp_path, fresh):
    text = MARKER + "\n" + json.dumps(decision())
    worker, handle = launch(tmp_path, [result(text), {"type": "assistant", "text": text}, result("done")], exit_code=3)
    payload = HermesWorker(worker.artifact_root).probe("op").receipt if fresh else worker.poll(handle).as_dict()
    # A restarted adapter has only the producer exit status; make that failure explicit too.
    if fresh:
        assert payload["outcome"] != "needs_decision"
    else:
        assert payload["outcome"] == "failed"


def test_graph_decision_holds_without_spending_or_acceptance(tmp_path):
    s = seed(tmp_path)
    s["worker"].outcomes = ["needs_decision"]
    s["worker"].decision = decision()
    r = drive(s["deps"], s["assignment"], s["attempt"])
    thread = thread_id(s["assignment"])
    assert r.state["status"] == "held"
    record = {"stage": "await_worker", "reason": "decision_required", "detail": "worker requires a decision", **decision()}
    assert r.state["hold_record"] == record
    assert s["deps"].store.repair_count(thread) == 0
    assert not list((tmp_path / "effects" / "gate-requests").glob("*"))
    assert not list((tmp_path / "effects" / "review-requests").glob("*"))
    assert inspect_run(s["deps"], thread)["hold_record"] == record
    assert export_run(s["deps"], thread)["state"]["hold_record"] == record
    assert drive(s["deps"], s["assignment"], s["attempt"]).state["hold_record"] == record
    assert len(s["worker"].submitted) == 1
