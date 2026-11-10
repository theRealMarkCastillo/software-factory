"""Regression tests for the frozen rollout harness (D1/D2 in REPAIR-SPEC)."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "langgraph_rollout_pilot.py"


def _module():
    spec = importlib.util.spec_from_file_location("rollout_pilot_under_test", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pilot(tmp_path: Path, module):
    return module.Pilot(
        argparse.Namespace(
            run_root=str(tmp_path / "run"),
            launcher=str(tmp_path / "staged" / "bin" / "factory"),
            card="t_pilot",
            attempt_run_id="7",
            attempt_profile="factory-runner",
            claim_lock="host:1",
        )
    )


def test_status_write_never_creates_the_immutable_run_root(tmp_path):
    module = _module()
    pilot = _pilot(tmp_path, module)

    with pytest.raises(FileNotFoundError):
        pilot.write_status({"phase": "preparing"})
    assert not pilot.root.exists()


def test_prepare_owns_creation_and_refuses_reuse(tmp_path):
    module = _module()
    pilot = _pilot(tmp_path, module)

    base, wrapper = pilot.prepare()
    assert pilot.root.is_dir()
    assert len(base) == 40
    assert wrapper.is_file()
    pilot.write_status({"phase": "preparing"})
    assert json.loads(pilot.status_path.read_text())["phase"] == "preparing"
    with pytest.raises(SystemExit) as excinfo:
        pilot.prepare()
    assert "never reused" in str(excinfo.value)


def test_execute_prepares_before_it_writes_status(tmp_path, monkeypatch):
    module = _module()
    pilot = _pilot(tmp_path, module)
    order: list[str] = []

    def fake_prepare():
        order.append("prepare")
        pilot.root.mkdir(parents=True)
        pilot.spec.write_text("# spec\n")
        return "0" * 40, pilot.root / "acceptance.py"

    def fake_status(payload):
        order.append(f"status:{payload.get('phase')}")

    def fake_cli(*args, **kwargs):
        order.append("cli")
        return module.subprocess.CompletedProcess(
            ["factory", *args],
            1,
            json.dumps({"status": "refused"}),
            "",
        )

    monkeypatch.setattr(pilot, "prepare", fake_prepare)
    monkeypatch.setattr(pilot, "write_status", fake_status)
    monkeypatch.setattr(pilot, "cli", fake_cli)
    monkeypatch.setattr(pilot, "finish", lambda *a: (order.append("finish"), 1)[1])

    assert pilot.execute() == 1
    assert order[:3] == ["prepare", "status:preparing", "status:admission"]


def _awaiting(key: str) -> dict:
    return {"kind": "await_review", "operation_key": key}


def test_review_completion_accepts_the_transcript_result_event(tmp_path):
    module = _module()
    pilot = _pilot(tmp_path, module)
    key = "run:request_independent_review:abc"
    directory = pilot.artifacts / "hermes-reviewer" / key.replace(":", "_")
    directory.mkdir(parents=True)
    (directory / "stream.jsonl").write_text(
        json.dumps({"type": "tool_use", "name": "read_file"})
        + "\n"
        + json.dumps({"type": "result", "text": '{"outcome":"approve"}'})
        + "\n"
    )

    assert pilot.operation_finished(_awaiting(key)) is True


def test_review_completion_holds_without_a_result(tmp_path):
    module = _module()
    pilot = _pilot(tmp_path, module)
    key = "run:request_independent_review:crash"
    directory = pilot.artifacts / "hermes-reviewer" / key.replace(":", "_")
    directory.mkdir(parents=True)
    (directory / "stream.jsonl").write_text(
        json.dumps({"type": "tool_use", "name": "read_file"}) + "\n"
    )

    assert pilot.operation_finished(_awaiting(key)) is False


def test_review_completion_ignores_a_result_from_another_operation(tmp_path):
    module = _module()
    pilot = _pilot(tmp_path, module)
    other = "run:request_independent_review:other"
    directory = pilot.artifacts / "hermes-reviewer" / other.replace(":", "_")
    directory.mkdir(parents=True)
    (directory / "review-result.json").write_text('{"outcome":"approve"}')

    assert (
        pilot.operation_finished(_awaiting("run:request_independent_review:mine"))
        is False
    )


def test_worker_completion_still_requires_its_own_transcript(tmp_path):
    module = _module()
    pilot = _pilot(tmp_path, module)
    key = "run:request_implementation:abc"
    directory = pilot.artifacts / "hermes-worker" / key.replace(":", "_")
    directory.mkdir(parents=True)
    transcript = directory / "stream.jsonl"
    transcript.write_text(json.dumps({"type": "result", "text": "done"}) + "\n")

    assert (
        pilot.operation_finished({"kind": "await_worker", "operation_key": key}) is True
    )
