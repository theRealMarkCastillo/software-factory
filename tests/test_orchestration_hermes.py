"""Supported Hermes surfaces: attempt projection, worker invocation, capability (G0/T4/T8/T10)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
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
        if key.startswith("HERMES_") or key.startswith("TERMINAL_") or key.startswith("DOCKER_")
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
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:1")
    board = HermesBoard(
        executable=str(stub),
        board="software-factory",
        extra_env={"STUB_SHOW": str(tmp_path / "show.json")},
    )
    attempt = Attempt(task_id=CARD, run_id=7, profile="factory-runner")
    view = board.view(attempt)
    assert view.status == "running"
    assert view.live_run_ids == (7,)
    assert view.current(attempt) is True
    assert (
        view.current(Attempt(task_id=CARD, run_id=6, profile="factory-runner")) is False
    )


def test_board_refuses_without_a_dispatcher_grant(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_SHOW", str(tmp_path / "show.json"))
    for name in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
    ):
        monkeypatch.delenv(name, raising=False)
    board = HermesBoard(executable=str(stub), board="software-factory")
    attempt = Attempt(task_id=CARD, run_id=7, profile="factory-runner")
    with pytest.raises(OrchestrationError) as excinfo:
        board.view(attempt)
    assert excinfo.value.reason == "owner_grant_absent"


def test_board_refuses_a_grant_for_a_different_run(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_SHOW", str(tmp_path / "show.json"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "8")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:1")
    board = HermesBoard(executable=str(stub), board="software-factory")
    attempt = Attempt(task_id=CARD, run_id=7, profile="factory-runner")
    with pytest.raises(OrchestrationError) as excinfo:
        board.view(attempt)
    assert excinfo.value.reason == "stale_attempt"


def test_superseded_run_is_not_current_even_with_a_grant(tmp_path, stub, monkeypatch):
    # A heartbeat only refreshes liveness; once the board no longer lists the run
    # as live, the attempt cannot act (T4 stale-attempt fencing).
    monkeypatch.setenv("STUB_SHOW", str(tmp_path / "show.json"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "6")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:1")
    payload = json.loads((tmp_path / "show.json").read_text())
    payload["runs"] = [{"id": 6, "profile": "factory-runner", "status": "failed"}]
    (tmp_path / "show.json").write_text(json.dumps(payload))
    board = HermesBoard(executable=str(stub), board="software-factory")
    attempt = Attempt(task_id=CARD, run_id=6, profile="factory-runner")
    assert board.view(attempt).current(attempt) is False


def test_heartbeat_is_run_scoped(tmp_path, stub, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    board = HermesBoard(
        executable=str(stub),
        board="software-factory",
        extra_env={"STUB_LOG": str(log), "STUB_SHOW": str(tmp_path / "show.json")},
    )
    attempt = Attempt(
        task_id=CARD, run_id=7, profile="factory-runner", claim_lock="host:1"
    )
    assert board.heartbeat(attempt) is True
    payload = json.loads(log.read_text().splitlines()[-1])
    assert payload["env"]["HERMES_KANBAN_RUN_ID"] == "7"
    assert payload["env"]["HERMES_KANBAN_CLAIM_LOCK"] == "host:1"
    monkeypatch.setenv("STUB_HEARTBEAT_EXIT", "1")
    board = HermesBoard(
        executable=str(stub),
        board="software-factory",
        extra_env={
            "STUB_LOG": str(log),
            "STUB_SHOW": str(tmp_path / "show.json"),
            "STUB_HEARTBEAT_EXIT": "1",
        },
    )
    assert board.heartbeat(attempt) is False


def _request(workspace: Path, *, base_sha: str = "a" * 40) -> WorkerRequest:
    return WorkerRequest(
        operation_key="sha256:" + "1" * 64,
        attempt=Attempt(task_id=CARD, run_id=7, profile="factory-runner"),
        profile="factory-runner",
        spec_digest="sha256:" + "2" * 64,
        workspace=str(workspace),
        base_sha=base_sha,
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
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "12")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:1")
    monkeypatch.setenv("HERMES_PROFILE", "factory-architect")
    monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")
    monkeypatch.setenv("TERMINAL_BACKEND", "local")
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/other.sock")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "/board/root")
    root = tmp_path / "artifacts"
    worker = HermesWorker(
        artifact_root=root,
        executable=str(stub),
        extra_env={
            "STUB_LOG": str(log),
            "STUB_SHOW": str(tmp_path / "show.json"),
            "STUB_CHAT_EXIT": "0",
        },
    )
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
    env = call["env"]
    for forbidden in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_PROFILE",
        "HERMES_ACCEPT_HOOKS",
        "HERMES_HOME",
        "TERMINAL_BACKEND",
        "DOCKER_HOST",
    ):
        assert forbidden not in env, forbidden
    # The delegation fence is a guard marker: preserved verbatim, never deleted.
    assert env["HERMES_DELEGATED_CHILD_CONTEXT"] == "/board/root"


def test_worker_failure_is_a_typed_outcome(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_CHAT_EXIT", "1")
    worker = HermesWorker(
        artifact_root=tmp_path / "artifacts",
        executable=str(stub),
        extra_env={"STUB_CHAT_EXIT": "1"},
    )
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


def test_worker_derives_a_receipt_from_the_committed_workspace(tmp_path, stub):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "base"],
        check=True,
        env={**os.environ, **env},
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("candidate\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "candidate"],
        check=True,
        env={**os.environ, **env},
    )
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    worker = HermesWorker(
        artifact_root=tmp_path / "artifacts",
        executable=str(stub),
        extra_env={"STUB_CHAT_EXIT": "0"},
    )
    submitted = worker.submit(_request(repo, base_sha=base))
    deadline = time.time() + 20
    receipt = None
    while time.time() < deadline:
        receipt = worker.poll(submitted.handle)
        if receipt is not None:
            break
        time.sleep(0.05)
    assert receipt is not None
    assert receipt.outcome == "completed"
    assert receipt.candidate_sha == candidate


def test_worker_exit_without_a_new_commit_is_not_success(tmp_path, stub):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "base"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    worker = HermesWorker(
        artifact_root=tmp_path / "artifacts",
        executable=str(stub),
        extra_env={"STUB_CHAT_EXIT": "0"},
    )
    submitted = worker.submit(_request(repo, base_sha=base))
    deadline = time.time() + 20
    receipt = None
    while time.time() < deadline:
        receipt = worker.poll(submitted.handle)
        if receipt is not None:
            break
        time.sleep(0.05)
    assert receipt is not None
    assert receipt.outcome == "failed"
    assert "without a candidate commit" in receipt.detail


def test_worker_cancellation_targets_its_own_process_group(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_CHAT_SLEEP", "30")
    worker = HermesWorker(
        artifact_root=tmp_path / "artifacts",
        executable=str(stub),
        terminate_grace_s=2,
        extra_env={"STUB_CHAT_SLEEP": "30"},
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


def _stored_review(key: str, **overrides) -> dict:
    payload = {
        "schema": "factory-review-result/v1",
        "operation_key": key,
        "attempt": {"task_id": CARD, "run_id": 7, "profile": "factory-runner"},
        "reviewer": "factory-qa",
        "outcome": "approve",
        "comment": "fine",
        "candidate_sha": "a" * 40,
        "base_sha": "b" * 40,
        "spec_digest": "sha256:" + "c" * 64,
        "evidence_digest": "sha256:" + "d" * 64,
        "graph_version": "factory-card/v1",
        "attestation_digest": "sha256:" + "e" * 64,
        "audit": {"violations": [], "tool_uses": []},
    }
    payload.update(overrides)
    return payload


def test_reviewer_result_is_readable_after_a_restart(tmp_path):
    key = "run:request_independent_review:abc"
    directory = tmp_path / "hermes-reviewer" / key.replace(":", "_")
    directory.mkdir(parents=True)
    (directory / "review-result.json").write_text(
        json.dumps(_stored_review(key), sort_keys=True)
    )

    # A fresh adapter has no in-memory record; the durable payload must suffice.
    reviewer = HermesReviewer(HermesWorker(artifact_root=tmp_path))
    probe = reviewer.probe(key)
    assert probe.state == "present"
    assert probe.receipt is not None
    assert probe.receipt["candidate_sha"] == "a" * 40
    review = reviewer._read_review(key)
    assert review is not None
    assert review.operation_key == key
    assert review.reviewer == "factory-qa"
    assert review.outcome == "approve"
    assert review.attempt.run_id == 7


def test_compact_review_result_fails_closed_not_keyerror(tmp_path):
    key = "run:request_independent_review:abc"
    directory = tmp_path / "hermes-reviewer" / key.replace(":", "_")
    directory.mkdir(parents=True)
    (directory / "review-result.json").write_text(
        json.dumps({"outcome": "approve", "comment": "thin"})
    )
    reviewer = HermesReviewer(HermesWorker(artifact_root=tmp_path))
    with pytest.raises(OrchestrationError) as excinfo:
        reviewer._read_review(key)
    assert excinfo.value.reason == "protocol_error"
    assert "review receipt" in excinfo.value.detail


def test_missing_executable_is_reported_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:1")
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
