"""CLI refusals are inspectable without weakening the library safety contract."""

import json
import os
from pathlib import Path

import pytest
from test_runner_cli_regressions import _argv, offline_runtime, subject  # noqa: F401

from factory import evidence, policy, runner, verify


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
