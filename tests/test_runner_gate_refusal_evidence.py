"""Pre-Docker gate refusals retain diagnostics, never certifiable evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_runner_cli_regressions import _commit, _git

from factory.evidence import MANIFEST_FILENAME


@pytest.mark.parametrize("gate", ["coverage", "datamodel"])
def test_cli_gate_refusal_saves_result_without_docker_or_manifest(tmp_path, gate):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".env.example").write_text("SETTING=example\n")
    (repo / "compose.yml").write_text("services:\n  test:\n    image: example:test\n")
    base = _commit(repo, "base")
    if gate == "coverage":
        (repo / "app.py").write_text("def unused():\n    return 1\n")
        (repo / "test_app.py").write_text(
            "import app\n\ndef test_import():\n    assert app\n"
        )
    else:
        (repo / "data-model.yml").write_text("invalid: [\n")
    candidate = _commit(repo, "candidate")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\nexit 99\n')
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "DOCKER_CALLS": str(calls),
    }
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory.runner",
            "--card",
            "gate-refusal",
            "--repo",
            str(repo),
            "--compose",
            "compose.yml",
            "--base",
            base,
            "--artifacts-root",
            str(tmp_path / "artifacts"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert process.returncode == 1, process.stderr
    result = json.loads(process.stdout)
    assert result["stage"] == gate + "_gate"
    assert result["passed"] is False
    assert result["artifacts_dir"] is not None
    artifacts = Path(result["artifacts_dir"])
    assert artifacts == tmp_path / "artifacts" / result["project_name"]
    assert json.loads((artifacts / "result.json").read_text()) == result
    assert not (artifacts / MANIFEST_FILENAME).exists()
    verification = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory.verify",
            "--artifacts-dir",
            str(artifacts),
            "--repo",
            str(repo),
            "--candidate",
            candidate,
            "--base",
            base,
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert verification.returncode == 1, verification.stderr
    assert json.loads(verification.stdout)["reason"] == "manifest_missing"
    assert not calls.exists()
