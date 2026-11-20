"""Required environment examples fail before gates or host execution."""

import json
import subprocess
from pathlib import Path

import pytest
from test_runner import _wire_successful_run

from factory import runner
from factory.policy import ProtectedProjectSnapshot


@pytest.fixture
def subject(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text("services:\n  test:\n    image: python:3.12\n")
    check = repo / "check.sh"
    check.write_text("#!/bin/sh\nexit 0\n")
    check.chmod(0o755)
    private = tmp_path / "private"
    private.mkdir()
    (private / "test_hidden.py").write_text("assert True\n")
    (private / "holdout.json").write_text(
        json.dumps(
            {
                "version": 1,
                "service": "test",
                "command": ["python", "/factory-holdout/test_hidden.py"],
                "files": ["test_hidden.py"],
            }
        )
    )
    _wire_successful_run(monkeypatch, tmp_path)
    calls = []

    def record_process(argv, *args, **kwargs):
        calls.append(("subprocess", argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def gates(repo, result, base):
        calls.append(("gate", None))
        result.stage = "audit_gate"
        return False

    def snapshot(cls):
        calls.append(("snapshot", None))
        return ProtectedProjectSnapshot()

    def compose(*args, **kwargs):
        calls.append(("compose", args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def ports(*args, **kwargs):
        calls.append(("ports", None))
        return []

    monkeypatch.setattr(subprocess, "run", record_process)
    monkeypatch.setattr(runner, "_run_preflight_gates", gates)
    monkeypatch.setattr(
        runner.ProtectedProjectSnapshot, "capture", classmethod(snapshot)
    )
    monkeypatch.setattr(runner, "run_compose", compose)
    monkeypatch.setattr(runner, "allocate_host_ports", ports)
    monkeypatch.setattr(runner, "capture_source", lambda repo: {})
    return repo, private, calls


@pytest.mark.parametrize("mode", ["acceptance", "diagnostic", "deployment", "holdout"])
@pytest.mark.parametrize("example", [None, "config/custom.env.example"])
def test_missing_example_refuses_before_any_work(
    subject, tmp_path, capsys, mode, example
):
    repo, private, calls = subject
    argv = [
        "--card",
        "env-input",
        "--repo",
        str(repo),
        "--compose",
        "compose.yml",
        "--artifacts-root",
        str(tmp_path / "artifacts"),
    ]
    if mode == "diagnostic":
        argv += ["--no-gates"]
    elif mode == "deployment":
        argv += ["--deployment", "--check", "check.sh"]
    elif mode == "holdout":
        argv += ["--holdout", str(private)]
    if example is not None:
        argv += ["--env-example", example]

    assert runner.main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "env_example_missing"
    assert result["passed"] is False
    assert result["exit_code"] != 0
    assert (example or ".env.example") in result["error"]
    assert "add" in result["error"].lower()
    assert "--env-example <path>" in result["error"]
    assert calls == []
    saved = Path(result["artifacts_dir"]) / "result.json"
    assert json.loads(saved.read_text()) == result


@pytest.mark.parametrize("example", [".env.example", "config/custom.env.example"])
def test_present_example_preserves_next_stage(subject, tmp_path, example):
    repo, _, calls = subject
    path = repo / example
    path.parent.mkdir(exist_ok=True)
    path.write_text("SETTING=example\n")
    result = runner.run_factory_card(
        "env-input",
        str(repo),
        ["compose.yml"],
        env_example=example,
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.stage == "audit_gate"
    assert calls == [("gate", None)]
    assert result.artifacts_dir is None


def test_help_marks_example_required(capsys):
    with pytest.raises(SystemExit) as exc:
        runner.main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    option_help = help_text.split("--env-example ENV_EXAMPLE")[-1].split("--fixture")[0]
    assert "required" in option_help.lower()
