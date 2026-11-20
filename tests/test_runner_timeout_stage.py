"""Timeout classification at the runner boundary, without real Docker."""

import json
import subprocess
from pathlib import Path

import pytest

from factory import runner, verify
from factory.policy import ProtectedProjectSnapshot


@pytest.fixture
def run_case(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text("services:\n  test:\n    image: alpine\n")
    (repo / ".env.example").write_text("# no configuration\n")
    check = repo / "check.sh"
    check.write_text("#!/bin/sh\nexit 0\n")
    check.chmod(0o755)
    monkeypatch.setenv("FACTORY_STATE_DIR", str(tmp_path / "state"))
    source = {"sha": "1" * 40, "status_porcelain": ""}
    monkeypatch.setattr(runner, "capture_source", lambda _: source)
    monkeypatch.setattr(
        runner,
        "resolve_base_info",
        lambda *a: {"ref": "main", "kind": "commit", "sha": "2" * 40},
    )

    def gates(repo, result, base):
        result.gates = {
            "audit": {"status": "pass", "passed": True},
            "coverage": {"status": "pass", "passed": True, "base_sha": base["sha"]},
            "datamodel": {"status": "skipped", "reason": "no_data_model"},
        }
        return True

    monkeypatch.setattr(runner, "_run_preflight_gates", gates)
    calls = []

    def capture(cls):
        calls.append("snapshot")
        return ProtectedProjectSnapshot()

    monkeypatch.setattr(ProtectedProjectSnapshot, "capture", classmethod(capture))

    def teardown(*args, **kwargs):
        calls.append("teardown")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runner, "teardown", teardown)

    def run(exc, **kwargs):
        def compose(*args, **options):
            calls.append(args[2][0])
            assert options["timeout"] == 17
            raise exc

        monkeypatch.setattr(runner, "run_compose", compose)
        result = runner.run_factory_card(
            "timeout-case",
            str(repo),
            ["compose.yml"],
            timeout=17,
            artifacts_root=str(tmp_path / "artifacts"),
            **kwargs,
        )
        artifacts = Path(result.artifacts_dir)
        manifest = json.loads((artifacts / "evidence-manifest.json").read_text())
        assert result.passed is False
        assert result.exit_code is None
        assert calls == ["snapshot", "up", "teardown", "snapshot"]
        assert manifest["execution"]["stage"] == result.stage
        assert manifest["runtime"]["teardown"]["ok"] is True
        assert manifest["runtime"]["safety"]["postflight"] == "clean"
        assert (artifacts / "RUN_FAILED.txt").read_text().startswith(type(exc).__name__)
        return result, artifacts, manifest, repo

    return run


@pytest.mark.parametrize("run_gates", [True, False], ids=["acceptance", "diagnostic"])
def test_compose_timeout_records_run_timeout(run_case, run_gates, capsys):
    exc = subprocess.TimeoutExpired(
        ["docker", "compose", "up"], 17, output=b"partial output", stderr=b"partial error"
    )
    result, artifacts, manifest, repo = run_case(exc, run_gates=run_gates)
    assert result.stage == "run_timeout"
    assert result.error.startswith("run_timeout: exceeded --timeout 17s")
    assert (artifacts / "up.stdout.log").read_text() == "partial output"
    assert (artifacts / "up.stderr.log").read_text() == "partial error"
    assert manifest["mode"] == ("acceptance" if run_gates else "diagnostic")
    if run_gates:
        assert verify.main(
            [
                "--artifacts-dir",
                str(artifacts),
                "--repo",
                str(repo),
                "--candidate",
                "1" * 40,
                "--base",
                "2" * 40,
                "--json",
            ]
        ) == 1
        rejection = json.loads(capsys.readouterr().out)
        assert rejection["reason"] == "execution_incomplete"
        assert "run_timeout" in rejection["detail"]


def test_deployment_compose_timeout_keeps_wait_stage(run_case):
    exc = subprocess.TimeoutExpired(["docker", "compose", "up"], 17)
    result, _, _, _ = run_case(exc, deployment=True, check="check.sh")
    assert result.stage == "deployment_wait_timeout"
    assert result.error == str(exc)


def test_acceptance_runtime_error_keeps_base_stage(run_case):
    result, _, _, _ = run_case(RuntimeError("compose failed"))
    assert result.stage == "complete_with_errors"
    assert result.error == "compose failed"
