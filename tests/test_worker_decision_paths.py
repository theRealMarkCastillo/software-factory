"""Decision persistence, redaction boundaries, restart and RFI graph paths."""

import json
import secrets
import sys
from pathlib import Path

import pytest
from factory_orch_fixtures import commit, seed
from test_worker_decisions import (
    MARKER,
    decision,
    invalid_decisions,
    launch,
    receipt,
    result,
)

from factory_orchestration.adapters.fake import FakeWorker
from factory_orchestration.adapters.hermes import HermesWorker
from factory_orchestration.decisions import sanitize_decision, terminal_decision
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import _receipt_payload, _worker_receipt, thread_id
from factory_orchestration.runner import drive, export_run, inspect_run, resume
from factory_orchestration.store import RunStore


@pytest.mark.parametrize("fresh", [False, True])
def test_real_graph_real_subprocess_decision_restart(tmp_path, monkeypatch, fresh):
    s = seed(tmp_path / "subject")
    canary = secrets.token_hex(18)
    monkeypatch.setenv("GRAPH_DECISION_SECRET", canary)
    d = {**decision(), "question": canary}
    executable = tmp_path / "hermes-producer"
    event = json.dumps(result(MARKER + "\n" + json.dumps(d)))
    executable.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(0.2)\nprint({event!r})\n"
    )
    executable.chmod(0o755)
    worker = HermesWorker(tmp_path / "real-effects", executable=str(executable))
    s["deps"].worker = worker
    r = drive(s["deps"], s["assignment"], s["attempt"])
    assert r.status == "awaiting_worker"
    key = next(iter(worker._processes))
    worker._processes[key].wait(timeout=10)
    if fresh:
        s["deps"].worker = HermesWorker(worker.artifact_root)
        s["deps"].store = RunStore(s["state_dir"])
    thread = thread_id(s["assignment"])
    r = resume(s["deps"], thread, s["attempt"], resolved_hold="await_worker")
    assert r.stage == "hold_operator"
    assert r.state["candidate_sha"] is None
    assert r.state["hold_record"]["question"] == "[REDACTED:GRAPH_DECISION_SECRET]"
    assert canary not in projection(s, thread)
    assert s["deps"].store.repair_count(thread) == 0
    assert {op["kind"] for op in s["deps"].store.operations(thread)} == {"worker"}
    assert len(list(worker.artifact_root.glob("hermes-worker/*/request.json"))) == 1
    held = [e for e in s["deps"].store.events(thread) if e["kind"] == "held"]
    assert held[-1]["detail"]["hold_record"] == r.state["hold_record"]


def test_protocol_example_and_public_rfi_guidance_match():
    from factory_orchestration.decisions import WORKER_DECISION_PROTOCOL

    source = Path(
        "integrations/hermes/skills/worker-software-engineer/SKILL.md"
    ).read_text()
    for text in (source, WORKER_DECISION_PROTOCOL):
        raw = text.split(MARKER + "\n", 1)[1].splitlines()[0]
        assert sanitize_decision(json.loads(raw)) == json.loads(raw)
        assert "RFI-n: question | chose | because" in text
        for term in ("irreversible", "schema", "auth", "money", "public"):
            assert term in text


@pytest.mark.parametrize("count", [1, 4])
def test_valid_empty_optional_fields_and_option_range(count):
    d = {**decision(), "spec_ref": "", "spec_excerpt": "", "recommendation": ""}
    d["options"] = [
        {"id": str(i), "label": "Choice", "consequence": "Tradeoff"}
        for i in range(count)
    ]
    assert sanitize_decision(d) == d


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("duplicate", ["question", "label"])
def test_sidechannel_duplicate_json_keys_refused(tmp_path, fresh, duplicate):
    worker, handle = launch(tmp_path, [result("done")])
    raw = json.dumps(
        {
            "attempt": worker._records["op"]["attempt"],
            "outcome": "needs_decision",
            "decision": decision(),
        }
    )
    raw = raw.replace(
        '"' + duplicate + '":', '"' + duplicate + '":"duplicate", "' + duplicate + '":'
    )
    Path(worker._records["op"]["result_path"]).write_text(raw)
    with pytest.raises(OrchestrationError) as exc:
        HermesWorker(worker.artifact_root).probe("op") if fresh else worker.poll(handle)
    assert exc.value.reason == "protocol_error"


@pytest.mark.parametrize("fresh", [False, True])
def test_top_level_result_only_ignores_truncated_and_nested_events(tmp_path, fresh):
    text = MARKER + "\n" + json.dumps(decision())
    worker, handle = launch(
        tmp_path,
        [
            result(text),
            [result("ignore")],
            {"wrapper": result("ignore")},
            {"type": "result", "text": {}},
        ],
    )
    with Path(worker._records["op"]["transcript"]).open("a") as stream:
        stream.write('{"type":"result",')
    observed = (
        HermesWorker(worker.artifact_root).probe("op").receipt
        if fresh
        else worker.poll(handle).as_dict()
    )
    assert observed["outcome"] == "needs_decision"


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize(
    "value",
    invalid_decisions(),
    ids=[f"schema-{i}" for i in range(len(invalid_decisions()))],
)
def test_full_malformed_schema_through_real_transport(tmp_path, fresh, value):
    worker, handle = launch(tmp_path, [result(MARKER + "\n" + json.dumps(value))])
    with pytest.raises(OrchestrationError) as exc:
        HermesWorker(worker.artifact_root).probe("op") if fresh else worker.poll(handle)
    assert exc.value.reason == "protocol_error"
    assert exc.value.detail == "invalid worker decision"


@pytest.mark.parametrize("journal_state", ["submitted", "confirmed"])
def test_graph_request_adopts_or_replays_without_resubmit(tmp_path, journal_state):
    from factory_orchestration.operations import digest_parts, stable_key

    s = seed(tmp_path)
    thread = thread_id(s["assignment"])
    digest = digest_parts(
        "implementation/v1",
        s["base"],
        s["assignment"].spec_digest,
        str(s["workspace"]),
        0,
    )
    key = stable_key(
        run_id=thread,
        stage="request_implementation",
        repair_index=0,
        input_digest=digest,
    )
    s["deps"].store.begin_operation(
        key,
        thread,
        stage="request_implementation",
        kind="worker",
        repair_index=0,
        input_digest=digest,
    )
    payload = {
        "operation_key": key,
        "attempt": s["attempt"].as_dict(),
        "session_id": "s",
        "outcome": "needs_decision",
        "decision": decision(),
    }
    if journal_state == "submitted":
        path = s["worker"]._receipt_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        s["deps"].store.mark_operation(key, journal_state)
    else:
        s["deps"].store.mark_operation(key, journal_state, receipt=payload)
    r = drive(s["deps"], s["assignment"], s["attempt"])
    assert r.state["last_reason_code"] == "decision_required"
    assert r.state["hold_record"]["question"] == decision()["question"]
    assert not s["worker"].submitted


def test_invalid_direct_receipt_is_protocol_hold_not_raw_error(tmp_path):
    s = seed(tmp_path)
    canary = secrets.token_hex(18)
    s["worker"].outcomes = ["needs_decision"]
    s["worker"].decision = {**decision(), "question": canary, "extra": canary}
    r = drive(s["deps"], s["assignment"], s["attempt"])
    assert r.state["last_reason_code"] == "protocol_error"
    assert r.stage == "hold_operator"
    assert s["deps"].store.repair_count(thread_id(s["assignment"])) == 0
    assert canary not in projection(s, thread_id(s["assignment"]))


@pytest.mark.parametrize("fresh", [False, True])
def test_raw_sidechannel_decision_size_is_checked_before_parsing(tmp_path, fresh):
    worker, handle = launch(tmp_path, [result("done")])
    raw = json.dumps(
        {
            "attempt": worker._records["op"]["attempt"],
            "outcome": "needs_decision",
            "decision": decision(),
        }
    )
    raw = raw.replace('"decision": {', '"decision": {' + " " * 16384)
    Path(worker._records["op"]["result_path"]).write_text(raw)
    with pytest.raises(OrchestrationError) as exc:
        HermesWorker(worker.artifact_root).probe("op") if fresh else worker.poll(handle)
    assert exc.value.reason == "protocol_error"


def projection(s, thread):
    return json.dumps(
        {
            "inspect": inspect_run(s["deps"], thread),
            "export": export_run(s["deps"], thread),
            "operations": s["deps"].store.operations(thread),
            "events": s["deps"].store.events(thread),
        },
        default=str,
    )


@pytest.mark.parametrize(
    "field",
    ["spec_ref", "spec_excerpt", "question", "label", "consequence", "answer_format"],
)
def test_runtime_canary_redacted_in_all_new_projections(tmp_path, monkeypatch, field):
    canary = secrets.token_hex(18)
    monkeypatch.setenv("WORKER_DECISION_SECRET", canary)
    d = decision()
    if field in ("label", "consequence"):
        d["options"][0][field] = "before " + canary + " after"
    else:
        d[field] = "before " + canary + " after"
    s = seed(tmp_path)
    s["worker"].outcomes = ["needs_decision"]
    s["worker"].decision = d
    r = drive(s["deps"], s["assignment"], s["attempt"])
    thread = thread_id(s["assignment"])
    assert r.state["hold_record"]["question"]
    text = projection(s, thread)
    assert canary not in text
    assert "REDACTED:WORKER_DECISION_SECRET" in text
    s["deps"].store = RunStore(s["state_dir"])
    assert (
        drive(s["deps"], s["assignment"], s["attempt"]).state["hold_record"]
        == r.state["hold_record"]
    )
    assert canary not in projection(s, thread)
    assert len(s["worker"].submitted) == 1


@pytest.mark.parametrize("field", ["id", "recommendation"])
def test_identity_control_redaction_is_refused(monkeypatch, field):
    canary = secrets.token_hex(4)
    monkeypatch.setenv("WORKER_CONTROL_SECRET", canary)
    d = decision()
    d["options"][0]["id"] = canary
    d["recommendation"] = canary if field == "recommendation" else ""
    with pytest.raises(OrchestrationError) as exc:
        receipt(outcome="needs_decision", decision=d)
    assert canary not in str(exc.value)
    assert exc.value.reason == "protocol_error"


@pytest.mark.parametrize(
    "field,limit",
    [
        ("question", 1024),
        ("spec_ref", 1024),
        ("spec_excerpt", 1024),
        ("answer_format", 256),
        ("label", 256),
        ("consequence", 512),
    ],
)
def test_utf8_bounds_and_redaction_before_bounding(monkeypatch, field, limit):
    canary = secrets.token_hex(10)
    monkeypatch.setenv("DECISION_BOUND_SECRET", canary)
    d = decision()
    value = "é" * (limit // 2 - 2) + canary
    if field in ("label", "consequence"):
        d["options"][0][field] = value
    else:
        d[field] = value
    safe = sanitize_decision(d)
    text = (
        safe["options"][0][field] if field in ("label", "consequence") else safe[field]
    )
    assert len(text.encode("utf-8")) <= limit
    assert canary[:4] not in text
    assert text.endswith("[RED")


@pytest.mark.parametrize(
    "field,limit",
    [("question", 1024), ("answer_format", 256), ("label", 256), ("consequence", 512)],
)
def test_required_field_blank_after_bound_is_invalid(field, limit):
    d = decision()
    value = " " * limit + "nonblank"
    if field in ("label", "consequence"):
        d["options"][0][field] = value
    else:
        d[field] = value
    with pytest.raises(OrchestrationError):
        sanitize_decision(d)


def test_raw_limit_is_not_a_truncation_allowance():
    d = decision()
    compact = json.dumps(d, separators=(",", ":"))
    assert (
        terminal_decision(MARKER + "\n" + compact + " " * (16384 - len(compact))) == d
    )
    with pytest.raises(OrchestrationError):
        terminal_decision(MARKER + "\n" + compact + " " * (16385 - len(compact)))
    with pytest.raises(OrchestrationError):
        sanitize_decision({**d, "question": "é" * 8200})


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("source", ["sidechannel", "terminal", "durable"])
def test_real_adapter_all_decision_sources_sanitized(
    tmp_path, monkeypatch, fresh, source
):
    canary = secrets.token_hex(18)
    monkeypatch.setenv("DECISION_SOURCE_SECRET", canary)
    d = {**decision(), "question": canary}
    worker, handle = launch(
        tmp_path,
        [result(MARKER + "\n" + json.dumps(d) if source == "terminal" else "done")],
    )
    payload = {
        "operation_key": "op",
        "attempt": worker._records["op"]["attempt"],
        "session_id": "s",
        "outcome": "needs_decision",
        "decision": d,
    }
    if source != "terminal":
        path = (
            worker._receipt_path("op")
            if source == "durable"
            else Path(worker._records["op"]["result_path"])
        )
        path.write_text(json.dumps(payload))
    if source == "durable" and not fresh:
        worker._processes.clear()
    observed = (
        HermesWorker(worker.artifact_root).probe("op").receipt
        if fresh
        else worker.poll(handle).as_dict()
    )
    assert observed["outcome"] == "needs_decision"
    assert canary not in json.dumps(observed)
    assert "REDACTED:DECISION_SOURCE_SECRET" in json.dumps(observed)


@pytest.mark.parametrize("fresh", [False, True])
def test_marker_takes_precedence_over_sidechannel_even_if_invalid(tmp_path, fresh):
    worker, handle = launch(tmp_path, [result(MARKER + "\n" + json.dumps(decision()))])
    Path(worker._records["op"]["result_path"]).write_text("this is not JSON")
    observed = (
        HermesWorker(worker.artifact_root).probe("op").receipt
        if fresh
        else worker.poll(handle).as_dict()
    )
    assert observed["decision"] == decision()


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize(
    "events",
    [[], [{"type": "assistant", "text": MARKER + "\n" + json.dumps(decision())}]],
)
def test_missing_result_stays_pending(tmp_path, fresh, events):
    worker, handle = launch(tmp_path, events)
    if fresh:
        assert HermesWorker(worker.artifact_root).probe("op").state == "unknown"
    else:
        assert worker.poll(handle) is None


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("exit_code", [0, 3])
def test_last_result_and_interim_prose_do_not_authorize_decision(
    tmp_path, fresh, exit_code
):
    text = MARKER + "\n" + json.dumps(decision())
    s = seed(tmp_path / "subject")
    worker, handle = launch(
        tmp_path,
        [
            result(text),
            {"type": "tool_result", "text": text},
            result("normal final", exit_code),
        ],
        exit_code=exit_code,
    )
    # Bind the durable request to a real committed candidate, not prose.
    record = worker._records["op"]
    record.update(workspace=str(s["workspace"]), base_sha=s["base"])
    request_path = worker._request_path("op")
    request = json.loads(request_path.read_text())
    request.update(workspace=record["workspace"], base_sha=record["base_sha"])
    request_path.write_text(json.dumps(request))
    observed = (
        HermesWorker(worker.artifact_root).probe("op").receipt
        if fresh
        else worker.poll(handle).as_dict()
    )
    assert observed["outcome"] == ("failed" if exit_code else "completed")
    assert "decision" not in observed
    if not exit_code:
        assert observed["candidate_sha"] == s["candidate"]


@pytest.mark.parametrize("fresh", [False, True])
def test_nonzero_terminal_with_marker_is_failure(tmp_path, fresh):
    worker, handle = launch(
        tmp_path, [result(MARKER + "\n" + json.dumps(decision()), 3)], exit_code=3
    )
    observed = (
        HermesWorker(worker.artifact_root).probe("op").receipt
        if fresh
        else worker.poll(handle).as_dict()
    )
    assert observed["outcome"] == "failed"
    assert "decision" not in observed


def test_graph_interrupted_receipt_and_restart(tmp_path):
    s = seed(tmp_path)
    s["worker"].outcomes = ["pending"]
    r = drive(s["deps"], s["assignment"], s["attempt"])
    assert r.status == "awaiting_worker"
    thread = thread_id(s["assignment"])
    key = s["worker"].submitted[0]
    payload = {
        "operation_key": key,
        "attempt": s["attempt"].as_dict(),
        "session_id": "s",
        "outcome": "needs_decision",
        "decision": decision(),
    }
    s["deps"].store = RunStore(s["state_dir"])
    r = resume(
        s["deps"],
        thread,
        s["attempt"],
        payload={"receipt": payload},
        resolved_hold="await_worker",
    )
    assert r.state["hold_record"]["question"] == decision()["question"]
    assert r.state["last_reason_code"] == "decision_required"
    assert len(s["worker"].submitted) == 1
    assert (
        drive(s["deps"], s["assignment"], s["attempt"]).state["hold_record"]
        == r.state["hold_record"]
    )


def test_graph_adopts_decision_payload_without_legacy_reconstructor_loss(tmp_path):
    s = seed(tmp_path)
    s["worker"].outcomes = ["pending"]
    r = drive(s["deps"], s["assignment"], s["attempt"])
    key = s["worker"].submitted[0]
    payload = {
        "operation_key": key,
        "attempt": s["attempt"].as_dict(),
        "session_id": "s",
        "outcome": "needs_decision",
        "decision": decision(),
    }
    s["worker"]._receipt_path(key).parent.mkdir(parents=True, exist_ok=True)
    s["worker"]._receipt_path(key).write_text(json.dumps(payload))
    s["deps"].worker = FakeWorker(s["worker"].root, s["candidate"])
    s["deps"].store = RunStore(s["state_dir"])
    r = resume(
        s["deps"],
        thread_id(s["assignment"]),
        s["attempt"],
        resolved_hold="await_worker",
    )
    assert r.state["last_reason_code"] == "decision_required"
    assert r.state["hold_record"]["options"] == decision()["options"]
    assert not s["deps"].worker.submitted


def test_adoption_and_graph_serializer_preserve_decision():
    payload = receipt(outcome="needs_decision", decision=decision()).as_dict()
    assert _receipt_payload(_worker_receipt(payload))["decision"] == decision()


def test_minor_rfi_is_normal_committed_candidate(tmp_path):
    s = seed(tmp_path)
    (s["workspace"] / "DECISIONS.md").write_text(
        "RFI-1: naming? | chose simple name | because reversible\n"
    )
    candidate = commit(s["workspace"], "minor RFI")
    s["worker"].candidate_sha = candidate
    # Rebuild fixture evidence for this actual candidate.
    from factory_orch_fixtures import make_evidence

    make_evidence(s["artifacts"], s["repo"], s["base"], candidate)
    r = drive(s["deps"], s["assignment"], s["attempt"])
    assert r.status == "ready_for_authorized_completion"
    assert r.state["candidate_sha"] == candidate
    assert "hold_record" not in r.state


@pytest.mark.parametrize("fresh", [False, True])
def test_malformed_sidechannel_canary_never_enters_graph_diagnostics(tmp_path, fresh):
    canary = secrets.token_hex(18)
    worker, handle = launch(tmp_path, [result("done")])
    d = {**decision(), "unknown": canary}
    payload = {
        "attempt": worker._records["op"]["attempt"],
        "outcome": "needs_decision",
        "decision": d,
    }
    Path(worker._records["op"]["result_path"]).write_text(json.dumps(payload))
    with pytest.raises(OrchestrationError) as exc:
        HermesWorker(worker.artifact_root).probe("op") if fresh else worker.poll(handle)
    assert canary not in str(exc.value)
    assert exc.value.reason == "protocol_error"
