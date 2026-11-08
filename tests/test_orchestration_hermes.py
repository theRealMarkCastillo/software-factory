"""Supported Hermes surfaces: attempt projection, worker invocation, capability (G0/T4/T8/T10)."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest
from factory_orch_fixtures import CARD

from factory_orchestration.adapters.hermes import (
    HermesBoard,
    HermesReviewer,
    HermesWorker,
    NoReadonlyEnforcement,
)
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.state import Attempt, WorkerRequest

STUB = """#!/usr/bin/env python3
import json, os, sys, time

args = sys.argv[1:]
log = os.environ.get("STUB_LOG")
if log:
    interesting = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("HERMES_KANBAN") or key == "HERMES_PROFILE"
    }
    with open(log, "a") as handle:
        handle.write(json.dumps({"argv": args, "env": interesting}) + "\\n")
if "kanban" in args:
    if "show" in args:
        print(open(os.environ["STUB_SHOW"]).read())
        raise SystemExit(0)
    if "heartbeat" in args:
        raise SystemExit(int(os.environ.get("STUB_HEARTBEAT_EXIT", "0")))
    raise SystemExit(2)
if "chat" in args:
    time.sleep(float(os.environ.get("STUB_CHAT_SLEEP", "0")))
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "stub-session", "model": "stub"}))
    print(json.dumps({"type": "result", "session_id": "stub-session", "exit_code": 0, "text": "done"}))
    raise SystemExit(int(os.environ.get("STUB_CHAT_EXIT", "0")))
raise SystemExit(2)
"""

SHOW = {
    "task": {
        "id": CARD,
        "status": "running",
        "assignee": "factory-runner",
        "workspace_path": "/tmp/ws",
        "claim_lock": None,
        "current_run_id": None,
    },
    "runs": [
        {"id": 6, "profile": "factory-runner", "status": "completed"},
        {"id": 7, "profile": "factory-runner", "status": "running"},
    ],
}


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    path = tmp_path / "hermes-stub"
    path.write_text(STUB)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "show.json").write_text(json.dumps(SHOW))
    return path


def test_board_projection_uses_the_live_run_not_claim_lock(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_SHOW", str(tmp_path / "show.json"))
    board = HermesBoard(executable=str(stub), board="software-factory")
    attempt = Attempt(task_id=CARD, run_id=7, profile="factory-runner")
    view = board.view(attempt)
    assert view.status == "running"
    assert view.live_run_ids == (7,)
    assert view.current(attempt) is True
    assert (
        view.current(Attempt(task_id=CARD, run_id=6, profile="factory-runner")) is False
    )


def test_heartbeat_is_run_scoped(tmp_path, stub, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    board = HermesBoard(executable=str(stub), board="software-factory")
    attempt = Attempt(
        task_id=CARD, run_id=7, profile="factory-runner", claim_lock="host:1"
    )
    assert board.heartbeat(attempt) is True
    payload = json.loads(log.read_text().splitlines()[-1])
    assert payload["env"]["HERMES_KANBAN_RUN_ID"] == "7"
    assert payload["env"]["HERMES_KANBAN_CLAIM_LOCK"] == "host:1"
    monkeypatch.setenv("STUB_HEARTBEAT_EXIT", "1")
    assert board.heartbeat(attempt) is False


def _request(workspace: Path) -> WorkerRequest:
    return WorkerRequest(
        operation_key="sha256:" + "1" * 64,
        attempt=Attempt(task_id=CARD, run_id=7, profile="factory-runner"),
        profile="factory-runner",
        spec_digest="sha256:" + "2" * 64,
        workspace=str(workspace),
        base_sha="a" * 40,
        allowed_scope=(str(workspace),),
        repair_index=0,
        timeout_s=60,
        artifact_dir=str(workspace),
        prompt="bounded prompt",
    )


def _result_file(root: Path, key: str) -> Path:
    directory = root / "hermes-worker" / key.replace(":", "_")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "worker-result.json"


def test_worker_invocation_uses_stream_json_and_scrubs_ownership(
    tmp_path, stub, monkeypatch
):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    root = tmp_path / "artifacts"
    worker = HermesWorker(artifact_root=root, executable=str(stub))
    request = _request(tmp_path)
    submitted = worker.submit(request)
    _result_file(root, request.operation_key).write_text(
        json.dumps(
            {
                "attempt": request.attempt.as_dict(),
                "outcome": "completed",
                "candidate_sha": "c" * 40,
                "artifact_references": ["artifacts/log.txt"],
                "session_id": "stub-session",
            }
        )
    )
    deadline = time.time() + 20
    receipt = None
    while time.time() < deadline:
        receipt = worker.poll(submitted.handle)
        if receipt is not None:
            break
        time.sleep(0.05)

    assert receipt is not None
    assert receipt.session_id == "stub-session"
    assert receipt.candidate_sha == "c" * 40
    assert submitted.handle.pgid == submitted.handle.pid
    call = json.loads(log.read_text().splitlines()[-1])
    assert call["argv"][:3] == ["-p", "factory-runner", "chat"]
    assert "--format" in call["argv"] and "stream-json" in call["argv"]
    assert "HERMES_KANBAN_TASK" not in call["env"]


def test_worker_failure_is_a_typed_outcome(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_CHAT_EXIT", "1")
    worker = HermesWorker(artifact_root=tmp_path / "artifacts", executable=str(stub))
    request = _request(tmp_path)
    submitted = worker.submit(request)
    deadline = time.time() + 20
    receipt = None
    while time.time() < deadline:
        receipt = worker.poll(submitted.handle)
        if receipt is not None:
            break
        time.sleep(0.05)
    assert receipt is not None
    assert receipt.outcome == "failed"


def test_worker_cancellation_targets_its_own_process_group(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_CHAT_SLEEP", "30")
    worker = HermesWorker(
        artifact_root=tmp_path / "artifacts", executable=str(stub), terminate_grace_s=2
    )
    submitted = worker.submit(_request(tmp_path))
    report = worker.cancel(submitted.handle)
    assert report.cancelled is True
    assert worker.poll(submitted.handle) is None or report.detail


def test_live_reviewer_refuses_unenforced_readonly(tmp_path):
    reviewer = HermesReviewer(
        HermesWorker(artifact_root=tmp_path), NoReadonlyEnforcement()
    )
    with pytest.raises(OrchestrationError) as excinfo:
        reviewer.submit(
            operation_key="k",
            attempt=Attempt(task_id=CARD, run_id=7, profile="factory-runner"),
            reviewer="factory-qa",
            workspace=str(tmp_path),
            candidate_sha="a" * 40,
            base_sha="b" * 40,
            spec_digest="sha256:" + "c" * 64,
            evidence_digest="sha256:" + "d" * 64,
            artifacts=str(tmp_path),
            timeout_s=60,
        )
    assert excinfo.value.reason == "capability_unenforced"
    assert "blocked, not passed" in excinfo.value.detail


def test_missing_executable_is_reported_as_unavailable(tmp_path):
    board = HermesBoard(executable=str(tmp_path / "absent"))
    with pytest.raises(OrchestrationError) as excinfo:
        board.view(Attempt(task_id=CARD, run_id=1, profile="factory-runner"))
    assert excinfo.value.reason == "worker_unavailable"


def test_g0_show_fixture_matches_captured_shape(tmp_path):
    # The captured CLI output carries no claim_lock/current_run_id; the adapter
    # must not depend on them (see docs/langgraph-hybrid/g0-capabilities.md).
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "kanban-show.json").read_text()
    )
    assert "claim_lock" in payload["task"]
    assert payload["task"]["claim_lock"] is None
    assert payload["task"]["current_run_id"] is None
    assert any(run["status"] == "running" for run in payload["runs"])
