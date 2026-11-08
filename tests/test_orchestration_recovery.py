"""Crash windows, single-writer admission, stale attempts and mismatch holds.

Child processes are killed or exit at a named phase; every assertion inspects
the durable effect directory, never an in-process call count.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from factory_orch_fixtures import CARD, request_count, seed

HERE = Path(__file__).resolve().parent
CHILD = HERE / "factory_orch_child.py"


def _config(
    seeded, tmp_path: Path, *, config_name: str = "child-config.json", **overrides
) -> Path:
    config = {
        "root": str(tmp_path),
        "repo": str(seeded["repo"]),
        "workspace": str(seeded["workspace"]),
        "base": seeded["base"],
        "candidate": seeded["candidate"],
        "spec": str(seeded["spec"]),
        "digest": seeded["digest"],
        "artifacts": str(seeded["artifacts"]),
        "effects": str(seeded["worker"].root),
        "state_dir": str(seeded["state_dir"]),
        "result_path": str(tmp_path / "child-result.json"),
        "worker_outcomes": list(seeded["worker"].outcomes),
        "reviewer_outcome": seeded["reviewer"].outcome,
    }
    config.update(overrides)
    path = tmp_path / config_name
    path.write_text(json.dumps(config, indent=2, sort_keys=True))
    return path


def _run(config_path: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHILD), str(config_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _child_result(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "child-result.json").read_text())


def _receipt_payload(result: dict, candidate: str) -> dict:
    key = result["result"]["awaiting"]["operation_key"]
    return {
        "receipt": {
            "operation_key": key,
            "session_id": "child-session",
            "outcome": "completed",
            "candidate_sha": candidate,
            "artifact_references": [],
            "reported_tests": ["pytest -q"],
            "detail": "",
        }
    }


def test_crash_before_submission_resubmits_only_after_proving_absence(tmp_path):
    seeded = seed(tmp_path)
    config = _config(
        seeded,
        tmp_path,
        crash_phase="before_submit",
        crash_stage="request_implementation",
        marker_dir=str(tmp_path),
    )
    crashed = _run(config)
    assert crashed.returncode == 97, crashed.stderr
    assert request_count(seeded["worker"]) == 0
    assert (tmp_path / "crashed-before_submit-request_implementation.json").is_file()

    resumed = _run(_config(seeded, tmp_path))
    assert resumed.returncode == 0, resumed.stderr
    assert request_count(seeded["worker"]) == 1
    assert (
        _child_result(tmp_path)["result"]["status"] == "ready_for_authorized_completion"
    )


def test_crash_after_submission_attaches_without_a_second_request(tmp_path):
    seeded = seed(tmp_path)
    config = _config(
        seeded,
        tmp_path,
        crash_phase="after_submit",
        crash_stage="request_implementation",
    )
    crashed = _run(config)
    assert crashed.returncode == 97
    assert request_count(seeded["worker"]) == 1

    restart = _run(_config(seeded, tmp_path))
    assert restart.returncode == 0, restart.stderr
    result = _child_result(tmp_path)["result"]
    assert result["status"] == "awaiting_worker"
    assert request_count(seeded["worker"]) == 1

    payload_path = tmp_path / "resume.json"
    payload_path.write_text(
        json.dumps(_receipt_payload(_child_result(tmp_path), seeded["candidate"]))
    )
    final = _run(
        _config(seeded, tmp_path, action="resume", resume_payload=str(payload_path))
    )
    assert final.returncode == 0, final.stderr
    assert (
        _child_result(tmp_path)["result"]["status"] == "ready_for_authorized_completion"
    )
    assert request_count(seeded["worker"]) == 1


def test_crash_after_receipt_replays_state_not_the_effect(tmp_path):
    seeded = seed(tmp_path)
    config = _config(
        seeded,
        tmp_path,
        crash_phase="after_receipt",
        crash_stage="request_implementation",
    )
    crashed = _run(config)
    assert crashed.returncode == 97
    assert request_count(seeded["worker"]) == 1

    restart = _run(_config(seeded, tmp_path))
    assert restart.returncode == 0, restart.stderr
    assert (
        _child_result(tmp_path)["result"]["status"] == "ready_for_authorized_completion"
    )
    assert request_count(seeded["worker"]) == 1


def test_crash_after_checkpoint_does_not_repeat_confirmed_work(tmp_path):
    seeded = seed(tmp_path)
    crashed = _run(_config(seeded, tmp_path, exit_after_drive=True))
    assert crashed.returncode == 97
    assert request_count(seeded["worker"]) == 1
    events = seeded["store_events"] = seeded["deps"].store.events(
        "software-factory:" + CARD
    )
    assert [event["kind"] for event in events].count("admitted") == 1

    restart = _run(_config(seeded, tmp_path))
    assert restart.returncode == 0, restart.stderr
    assert (
        _child_result(tmp_path)["result"]["status"] == "ready_for_authorized_completion"
    )
    assert request_count(seeded["worker"]) == 1


def test_crash_after_acceptance_receipt_does_not_rerun_the_gate(tmp_path):
    seeded = seed(tmp_path)
    crashed = _run(
        _config(
            seeded,
            tmp_path,
            gate_crash_phase="after_receipt",
        )
    )
    assert crashed.returncode == 97
    receipts = list((seeded["worker"].root / "gate-receipts").glob("*.json"))
    assert len(receipts) == 1

    restart = _run(_config(seeded, tmp_path))
    assert restart.returncode == 0, restart.stderr
    assert (
        _child_result(tmp_path)["result"]["status"] == "ready_for_authorized_completion"
    )
    assert len(list((seeded["worker"].root / "gate-receipts").glob("*.json"))) == 1
    assert request_count(seeded["worker"]) == 1


def test_two_concurrent_admissions_have_one_effect_owner(tmp_path):
    seeded = seed(tmp_path)
    start = tmp_path / "start"
    first_config = _config(
        seeded, tmp_path, config_name="first-config.json", start_file=str(start)
    )
    second_result = tmp_path / "second-result.json"
    second_config = _config(
        seeded,
        tmp_path,
        config_name="second-config.json",
        start_file=str(start),
        result_path=str(second_result),
    )
    first = subprocess.Popen(
        [sys.executable, str(CHILD), str(first_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        [sys.executable, str(CHILD), str(second_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    start.write_text("go\n")
    first_stdout, first_stderr = first.communicate(timeout=120)
    second_stdout, second_stderr = second.communicate(timeout=120)

    assert first.returncode in (0, 4), first_stderr
    assert second.returncode in (0, 4), second_stderr
    assert request_count(seeded["worker"]) == 1
    events = seeded["deps"].store.events("software-factory:" + CARD)
    assert [event["kind"] for event in events].count("admitted") == 1
    outcomes = [
        json.loads((tmp_path / "child-result.json").read_text()),
        json.loads(second_result.read_text()),
    ]
    refused = [item for item in outcomes if item.get("reason") == "workspace_conflict"]
    assert len(refused) <= 1


def test_superseded_attempt_cannot_resume_or_act(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    paused = _run(_config(seeded, tmp_path))
    assert paused.returncode == 0, paused.stderr
    result = _child_result(tmp_path)["result"]
    assert result["status"] == "awaiting_worker"

    payload_path = tmp_path / "resume.json"
    payload_path.write_text(
        json.dumps(_receipt_payload(_child_result(tmp_path), seeded["candidate"]))
    )
    stale = _run(
        _config(
            seeded,
            tmp_path,
            action="resume",
            resume_payload=str(payload_path),
            attempt_run_id=7,
            live_run_id=8,
        )
    )
    assert stale.returncode == 4, stale.stderr
    stale_result = _child_result(tmp_path)
    assert stale_result["reason"] == "stale_attempt"
    assert request_count(seeded["worker"]) == 1


def test_cancellation_is_idempotent_and_blocks_resume(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    assert _run(_config(seeded, tmp_path)).returncode == 0

    first = _run(_config(seeded, tmp_path, action="cancel"))
    assert first.returncode == 0, first.stderr
    assert _child_result(tmp_path)["cancel"]["status"] == "cancelled"
    cancels = list((seeded["worker"].root / "worker-cancels").glob("*.json"))
    assert len(cancels) == 1

    second = _run(_config(seeded, tmp_path, action="cancel"))
    assert second.returncode == 0, second.stderr
    assert _child_result(tmp_path)["cancel"]["new_request"] is False
    assert request_count(seeded["worker"]) == 1

    payload_path = tmp_path / "resume.json"
    payload_path.write_text(
        json.dumps(
            _receipt_payload(
                {"result": {"awaiting": {"operation_key": "k"}}}, seeded["candidate"]
            )
        )
    )
    blocked = _run(
        _config(
            seeded,
            tmp_path,
            action="resume",
            resume_payload=str(payload_path),
        )
    )
    assert blocked.returncode == 4
    assert _child_result(tmp_path)["reason"] == "cancelled"


def test_moved_base_and_changed_spec_fail_closed(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    assert _run(_config(seeded, tmp_path)).returncode == 0

    moved = _run(_config(seeded, tmp_path, base=seeded["candidate"]))
    assert moved.returncode == 4
    assert _child_result(tmp_path)["reason"] == "version_mismatch"

    seeded["spec"].write_text("# changed spec\n")
    changed = _run(_config(seeded, tmp_path))
    assert changed.returncode == 4
    assert _child_result(tmp_path)["reason"] == "version_mismatch"
    assert request_count(seeded["worker"]) == 1


def test_unrecognized_release_and_graph_version_hold(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    assert _run(_config(seeded, tmp_path)).returncode == 0

    from factory_orchestration.graph import build_graph
    from factory_orchestration.runner import _checkpoint_saver

    with _checkpoint_saver(seeded["deps"].store) as saver:
        graph = build_graph(seeded["deps"], saver)
        graph.update_state(
            {"configurable": {"thread_id": "software-factory:" + CARD}},
            {"graph_version": "factory-card/v0"},
        )
    payload_path = tmp_path / "resume.json"
    payload_path.write_text(
        json.dumps(_receipt_payload(_child_result(tmp_path), seeded["candidate"]))
    )
    result = _run(
        _config(
            seeded,
            tmp_path,
            action="resume",
            resume_payload=str(payload_path),
        )
    )
    assert result.returncode == 4
    assert _child_result(tmp_path)["reason"] == "version_mismatch"
    assert request_count(seeded["worker"]) == 1
