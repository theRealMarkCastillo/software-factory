"""CLI refusals are inspectable without weakening the library safety contract."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_runner_cli_regressions import _argv

from factory import evidence, policy, runner, verify

pytest_plugins = ["test_runner_cli_regressions"]


@pytest.fixture
def recording_docker(tmp_path, monkeypatch):
    log = tmp_path / "docker.calls"
    binary = tmp_path / "bin" / "docker"
    binary.parent.mkdir()
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALL_LOG"\nexit 99\n')
    binary.chmod(0o755)
    monkeypatch.setenv("DOCKER_CALL_LOG", str(log))
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    return log


@pytest.mark.parametrize(
    "setting",
    [
        'ports: ["8080:80"]',
        'volumes: ["./.env:/app/.env"]',
        'volumes: ["/home/x:/app/data"]',
        'volumes: ["/var/run/docker.sock:/var/run/docker.sock"]',
        "privileged: true",
        "network_mode: host",
    ],
)
@pytest.mark.parametrize("flags", [[], ["--no-gates"], ["--mode", "diagnostic"]])
def test_cli_static_policy_refusal_is_structured(
    subject, recording_docker, tmp_path, capsys, setting, flags
):
    repo, base, candidate = subject
    (repo / "compose.yml").write_text(
        "services:\n  test:\n    image: example:test\n    " + setting + "\n"
    )
    # Keep the library's refusal and exact diagnostic as the comparison oracle.
    with pytest.raises(policy.PolicyViolation) as refusal:
        runner.run_factory_card(
            "regression",
            str(repo),
            ["compose.yml"],
            artifacts_root=str(tmp_path / "library-artifacts"),
        )
    assert runner.main(_argv(repo, tmp_path) + flags) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["card_id"] == "regression"
    assert result["project_name"].startswith("factory-")
    assert result["passed"] is False
    assert result["stage"] == "policy_violation"
    assert result["error"] == str(refusal.value)
    assert result["gates_run"] is ("--no-gates" not in flags)
    assert result["mode"] == ("acceptance" if not flags else "diagnostic")
    artifacts = Path(result["artifacts_dir"])
    assert artifacts == tmp_path / "artifacts" / result["project_name"]
    assert json.loads((artifacts / "result.json").read_text()) == result
    assert not (artifacts / evidence.MANIFEST_FILENAME).exists()
    assert not recording_docker.exists()
    assert (
        verify.main(
            [
                "--artifacts-dir",
                str(artifacts),
                "--repo",
                str(repo),
                "--candidate",
                candidate,
                "--base",
                base,
                "--json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not recording_docker.exists()


@pytest.mark.parametrize("command", ["factory-run", "factory"])
def test_installed_cli_policy_refusal(subject, recording_docker, tmp_path, command):
    repo, _, _ = subject
    (repo / "compose.yml").write_text(
        'services:\n  test:\n    image: example:test\n    ports: ["8080:80"]\n'
    )
    executable = Path(sys.executable).parent / command
    argv = [str(executable)] + (["run"] if command == "factory" else [])
    completed = subprocess.run(
        argv + _argv(repo, tmp_path), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 1
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["stage"] == "policy_violation"
    artifacts = Path(result["artifacts_dir"])
    assert json.loads((artifacts / "result.json").read_text()) == result
    assert not (artifacts / evidence.MANIFEST_FILENAME).exists()
    assert not recording_docker.exists()


@pytest.mark.parametrize("command", ["factory-run", "factory"])
def test_installed_cli_snapshot_failure_propagates(
    subject, recording_docker, tmp_path, command
):
    repo, _, _ = subject
    (repo / "compose.yml").write_text(
        "services:\n  test:\n    image: example:test\n    environment: {A: b}\n"
    )
    executable = Path(sys.executable).parent / command
    argv = [str(executable)] + (["run"] if command == "factory" else [])
    completed = subprocess.run(
        argv + _argv(repo, tmp_path), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 1
    calls = recording_docker.read_text().splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("ps ")
    result = json.loads(completed.stdout) if completed.stdout else {}
    assert result.get("stage") != "policy_violation"
    directories = list((tmp_path / "artifacts").glob("factory-*"))
    assert len(directories) == 1
    for directory in directories:
        result_path = directory / "result.json"
        if result_path.exists():
            assert json.loads(result_path.read_text())["project_name"] == directory.name
    assert completed.stdout == ""
    assert "Traceback" in completed.stderr
    assert "PolicyViolation: could not snapshot docker state:" in completed.stderr
    assert list(directories[0].iterdir()) == []


def test_library_static_refusal_marks_original_project(
    subject, recording_docker, tmp_path, monkeypatch, capsys
):
    repo, _, _ = subject
    (repo / "compose.yml").write_text(
        "services:\n  test:\n    image: example:test\n    privileged: true\n"
    )
    projects = []

    def project(card):
        name = f"factory-{card}-{len(projects)}"
        projects.append(name)
        return name

    monkeypatch.setattr(runner, "new_factory_project", project)
    with pytest.raises(policy.PolicyViolation) as refusal:
        runner.run_factory_card("regression", str(repo), ["compose.yml"])
    assert isinstance(refusal.value, runner.PreDockerPolicyViolation)
    assert refusal.value.project_name == projects[0]
    assert isinstance(refusal.value.__cause__, policy.PolicyViolation)
    assert refusal.value.args == refusal.value.__cause__.args
    assert str(refusal.value) == str(refusal.value.__cause__)
    assert runner.main(_argv(repo, tmp_path)) == 1
    result = json.loads(capsys.readouterr().out)
    assert projects == ["factory-regression-0", "factory-regression-1"]
    assert result["project_name"] == projects[1]
    assert Path(result["artifacts_dir"]).name == projects[1]
    assert not recording_docker.exists()


def test_cli_sensitive_env_refusal_in_home_checkout(
    subject, recording_docker, tmp_path, monkeypatch, capsys
):
    repo, _, _ = subject
    # Model a home checkout without depending on the real host's home layout.
    monkeypatch.setattr(policy, "_is_home_checkout", lambda root: root == repo)
    (repo / ".env").write_text("EXAMPLE=local\n")
    (repo / "compose.yml").write_text(
        'services:\n  test:\n    image: example:test\n'
        '    volumes: ["./.env:/app/.env"]\n'
    )
    assert runner.main(_argv(repo, tmp_path)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "policy_violation"
    assert "mounting sensitive repository path './.env'" in result["error"]
    artifacts = Path(result["artifacts_dir"])
    assert artifacts.name == result["project_name"]
    assert json.loads((artifacts / "result.json").read_text()) == result
    assert list(artifacts.parent.glob("factory-*")) == [artifacts]
    assert not recording_docker.exists()


def test_cli_deployment_policy_mode(subject, recording_docker, tmp_path, capsys):
    repo, _, _ = subject
    check = repo / "check.sh"
    check.write_text("#!/bin/sh\nexit 0\n")
    check.chmod(0o755)
    (repo / "compose.yml").write_text(
        "services:\n  test:\n    image: example:test\n    privileged: true\n"
    )
    assert (
        runner.main(_argv(repo, tmp_path) + ["--deployment", "--check", "check.sh"])
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "policy_violation"
    assert result["mode"] == "deployment"
    assert result["gates_run"] is True
    assert not recording_docker.exists()


def test_cli_other_exceptions_propagate(subject, monkeypatch, tmp_path, capsys):
    repo, _, _ = subject

    def fail(**kwargs):
        raise RuntimeError("unrelated failure")

    monkeypatch.setattr(runner, "run_factory_card", fail)
    with pytest.raises(RuntimeError, match="unrelated failure"):
        runner.main(_argv(repo, tmp_path))
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "artifacts").exists()


def test_cli_postflight_policy_violation_keeps_stage(
    subject, offline_runtime, monkeypatch, tmp_path, capsys
):
    repo, _, _ = subject
    snapshots = [
        policy.ProtectedProjectSnapshot(),
        policy.PolicyViolation("postflight unavailable"),
    ]

    def capture(cls):
        item = snapshots.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(
        runner.ProtectedProjectSnapshot, "capture", classmethod(capture)
    )
    assert runner.main(_argv(repo, tmp_path)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "postflight_failed"
    assert result["error"] == "postflight unavailable"
    assert offline_runtime
    artifacts = Path(result["artifacts_dir"])
    assert (artifacts / "POSTFLIGHT_FAILURE.txt").read_text() == result["error"]
    assert not (artifacts / "result.json").exists()
