"""Durable receipt adoption across process boundaries (T3/T4 shipped CLI gap).

The shipped ``run`` command submits a real worker or reviewer, then persists an
interrupt as soon as the child is not finished. The operator's ``resume`` call
runs in a *new* process, which owns no child handle. Before this change the
adapters could only report a durable receipt that the same process had written,
so a real worker or reviewer that finished after the run process exited left no
adoptable effect and ``resume`` needed a hand-written receipt payload.

These tests describe the required behaviour: a finished child is provable from
its own durable artifacts (the ``result`` event it writes into ``stream.jsonl``,
plus its workspace and its result file); an unfinished or unprovable child is
never adopted and never replayed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from factory_orch_fixtures import CARD, enable_admission, seed

from factory_orchestration.adapters.hermes import HermesWorker
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import thread_id
from factory_orchestration.operations import EffectProbe, _attempt_from
from factory_orchestration.runner import drive, resume


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _workspace(root: Path) -> tuple[Path, str]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    _git(workspace, "init", "-q", "-b", "main")
    (workspace / "app.py").write_text("x = 1\n")
    _git(workspace, "add", "-A")
    _git(
        workspace,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "base",
    )
    return workspace, _git(workspace, "rev-parse", "HEAD")


def _commit_candidate(workspace: Path) -> str:
    (workspace / "app.py").write_text("x = 2\n")
    _git(workspace, "add", "-A")
    _git(
        workspace,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "candidate",
    )
    return _git(workspace, "rev-parse", "HEAD")


def _write_transcript(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def _request(
    root: Path,
    key: str,
    *,
    attempt: dict,
    workspace: Path,
    base_sha: str,
    finished: bool,
    exit_code: int = 0,
    session_id: str = "session-recorded",
) -> Path:
    directory = root / "hermes-worker" / key.replace(":", "_")
    directory.mkdir(parents=True, exist_ok=True)
    transcript = directory / "stream.jsonl"
    events: list[dict] = [
        {"type": "system", "session_id": session_id},
        {"type": "text", "text": "working"},
    ]
    if finished:
        events.append(
            {
                "type": "result",
                "session_id": session_id,
                "exit_code": exit_code,
                "text": "done",
            }
        )
    _write_transcript(transcript, events)
    (directory / "request.json").write_text(
        json.dumps(
            {
                "operation_key": key,
                "argv": ["hermes", "-p", "factory-runner", "chat"],
                "workspace": str(workspace),
                "base_sha": base_sha,
                "attempt": attempt,
                "transcript": str(transcript),
                "result_path": str(directory / "worker-result.json"),
                "pid": 999_999,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return transcript


def _attempt_payload() -> dict:
    return {
        "task_id": CARD,
        "run_id": 7,
        "profile": "factory-runner",
        "claim_lock": None,
    }


def _pending_keys(seeded: dict) -> list[str]:
    records = seeded["deps"].store.operations(thread_id(seeded["assignment"]))
    return [
        record["operation_key"]
        for record in records
        if record["state"] in {"submitted", "unknown"}
    ]


def test_finished_worker_is_adopted_from_its_durable_artifacts(tmp_path):
    workspace, base = _workspace(tmp_path)
    candidate = _commit_candidate(workspace)
    root = tmp_path / "artifacts"
    key = "sha256:worker-key"
    _request(
        root,
        key,
        attempt=_attempt_payload(),
        workspace=workspace,
        base_sha=base,
        finished=True,
    )

    probe = HermesWorker(artifact_root=root).probe(key)

    assert probe.state == "present"
    assert probe.receipt is not None
    assert probe.receipt["operation_key"] == key
    assert probe.receipt["outcome"] == "completed"
    assert probe.receipt["candidate_sha"] == candidate
    assert probe.receipt["session_id"] == "session-recorded"
    assert _attempt_from(probe.receipt["attempt"]).run_id == 7


def test_unfinished_worker_is_never_adopted(tmp_path):
    workspace, base = _workspace(tmp_path)
    root = tmp_path / "artifacts"
    key = "sha256:worker-key"
    _request(
        root,
        key,
        attempt=_attempt_payload(),
        workspace=workspace,
        base_sha=base,
        finished=False,
    )

    probe = HermesWorker(artifact_root=root).probe(key)

    assert probe.state == "unknown"
    assert probe.receipt is None
    assert "not provably finished" in probe.detail


def test_failed_worker_is_adopted_as_failed_not_completed(tmp_path):
    workspace, base = _workspace(tmp_path)
    _commit_candidate(workspace)
    root = tmp_path / "artifacts"
    key = "sha256:worker-key"
    _request(
        root,
        key,
        attempt=_attempt_payload(),
        workspace=workspace,
        base_sha=base,
        finished=True,
        exit_code=2,
    )

    probe = HermesWorker(artifact_root=root).probe(key)

    assert probe.state == "present"
    assert probe.receipt is not None
    assert probe.receipt["outcome"] == "failed"
    assert "exited 2" in probe.receipt["detail"]


def test_worker_without_a_candidate_commit_is_not_a_success(tmp_path):
    workspace, base = _workspace(tmp_path)
    root = tmp_path / "artifacts"
    key = "sha256:worker-key"
    _request(
        root,
        key,
        attempt=_attempt_payload(),
        workspace=workspace,
        base_sha=base,
        finished=True,
    )

    probe = HermesWorker(artifact_root=root).probe(key)

    assert probe.state == "present"
    assert probe.receipt is not None
    assert probe.receipt["outcome"] == "failed"
    assert "without a candidate commit" in probe.receipt["detail"]


def test_worker_that_never_launched_is_absent(tmp_path):
    probe = HermesWorker(artifact_root=tmp_path / "artifacts").probe("sha256:none")
    assert isinstance(probe, EffectProbe)
    assert probe.state == "absent"


def test_resume_without_a_payload_adopts_the_durable_worker_receipt(tmp_path):
    seeded = seed(tmp_path / "seed")
    seeded["deps"].worker.outcomes = ["pending"]
    drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    pending = _pending_keys(seeded)
    assert pending, "the worker operation must be pending before resume"

    # The child finished and committed after the run process exited; only its
    # durable request/transcript/workspace remain. The workspace HEAD is the
    # candidate commit the fixture's evidence was generated for.
    root = tmp_path / "artifacts"
    candidate = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=seeded["workspace"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert candidate == seeded["candidate"]
    _request(
        root,
        pending[-1],
        attempt=_attempt_payload(),
        workspace=seeded["workspace"],
        base_sha=seeded["base"],
        finished=True,
    )
    seeded["deps"].worker = HermesWorker(artifact_root=root)

    result = resume(
        seeded["deps"],
        thread_id(seeded["assignment"]),
        seeded["attempt"],
        resolved_hold="worker request awaited",
    )

    assert result.status == "ready_for_authorized_completion"
    assert result.state["candidate_sha"] == candidate


def test_resume_refuses_to_adopt_an_unprovable_effect(tmp_path):
    seeded = seed(tmp_path / "seed")
    seeded["deps"].worker.outcomes = ["pending"]
    drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    pending = _pending_keys(seeded)
    root = tmp_path / "artifacts"
    _request(
        root,
        pending[-1],
        attempt=_attempt_payload(),
        workspace=seeded["workspace"],
        base_sha=seeded["base"],
        finished=False,
    )
    seeded["deps"].worker = HermesWorker(artifact_root=root)

    with pytest.raises(OrchestrationError) as excinfo:
        resume(
            seeded["deps"],
            thread_id(seeded["assignment"]),
            seeded["attempt"],
            resolved_hold="worker request awaited",
        )

    assert excinfo.value.reason == "effect_unknown"


def test_resume_requires_a_named_hold(tmp_path):
    seeded = seed(tmp_path / "seed")
    drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    with pytest.raises(OrchestrationError) as excinfo:
        resume(
            seeded["deps"],
            thread_id(seeded["assignment"]),
            seeded["attempt"],
            resolved_hold="   ",
        )
    assert excinfo.value.reason == "invalid_assignment"


def test_admission_is_still_required_before_any_adoption(tmp_path):
    seeded = seed(tmp_path / "seed")
    enable_admission(seeded["state_dir"], cards=(), enabled=True)
    with pytest.raises(OrchestrationError):
        drive(seeded["deps"], seeded["assignment"], seeded["attempt"])


def _review_request(
    root: Path,
    key: str,
    *,
    candidate: str,
    base: str,
    verdict: str,
    audit_violation: bool = False,
    finished: bool = True,
) -> Path:
    directory = root / "hermes-reviewer" / key.replace(":", "_")
    directory.mkdir(parents=True, exist_ok=True)
    transcript = directory / "stream.jsonl"
    events: list[dict] = [
        {"type": "system", "session_id": "review-session"},
        {
            "type": "tool_use",
            "name": "read_file",
            "input": {
                "path": "/etc/passwd"
                if audit_violation
                else "/evidence/candidate/app.py"
            },
        },
    ]
    if finished:
        events.append(
            {
                "type": "result",
                "session_id": "review-session",
                "exit_code": 0,
                "text": json.dumps({"outcome": verdict, "comment": "bounded"}),
            }
        )
    _write_transcript(transcript, events)
    (directory / "request.json").write_text(
        json.dumps(
            {
                "operation_key": key,
                "argv": ["hermes", "chat"],
                "reviewer": "factory-qa",
                "attempt": _attempt_payload(),
                "transcript": str(transcript),
                "result_path": str(directory / "review-result.json"),
                "graph_version": "factory-card/v1",
                "candidate_sha": candidate,
                "base_sha": base,
                "spec_digest": "sha256:" + "c" * 64,
                "evidence_digest": "sha256:" + "d" * 64,
                "attestation_digest": "sha256:" + "e" * 64,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return transcript


def _reviewer(root: Path):
    from factory_orchestration.adapters.hermes import HermesReviewer

    return HermesReviewer(HermesWorker(artifact_root=root))


def test_finished_review_is_adopted_with_its_binding_and_audit(tmp_path):
    root = tmp_path / "artifacts"
    key = "sha256:review-key"
    candidate = "1" * 40
    base = "2" * 40
    _review_request(root, key, candidate=candidate, base=base, verdict="approve")

    probe = _reviewer(root).probe(key)

    assert probe.state == "present"
    receipt = probe.receipt
    assert receipt["outcome"] == "approve"
    assert receipt["candidate_sha"] == candidate
    assert receipt["base_sha"] == base
    assert receipt["graph_version"] == "factory-card/v1"
    assert receipt["audit"]["violations"] == []
    # The durable receipt was written so a later restart adopts the same audit.
    assert (
        root / "hermes-reviewer" / key.replace(":", "_") / "review-result.json"
    ).is_file()


def test_review_scope_violation_is_adopted_as_inconclusive_never_green(tmp_path):
    root = tmp_path / "artifacts"
    key = "sha256:review-key"
    _review_request(
        root,
        key,
        candidate="1" * 40,
        base="2" * 40,
        verdict="approve",
        audit_violation=True,
    )

    probe = _reviewer(root).probe(key)

    assert probe.state == "present"
    assert probe.receipt["outcome"] == "inconclusive"
    assert "review_scope_violation" in probe.receipt["comment"]


def test_unfinished_review_is_never_adopted(tmp_path):
    root = tmp_path / "artifacts"
    key = "sha256:review-key"
    _review_request(
        root,
        key,
        candidate="1" * 40,
        base="2" * 40,
        verdict="approve",
        finished=False,
    )

    probe = _reviewer(root).probe(key)

    assert probe.state == "unknown"
    assert probe.receipt is None
