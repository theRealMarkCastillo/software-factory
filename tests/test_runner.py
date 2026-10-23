"""Isolated lifecycle tests for factory.runner. Docker and git are mocked so
the safety state machine is exercised without touching the host."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import runner
from factory.evidence import (
    artifact_records,
    assert_assembled_evidence,
    engine_identity,
)
from factory.evidence import write_evidence_manifest as real_evidence_manifest
from factory.policy import PolicyViolation, ProtectedProjectSnapshot

CANDIDATE_SHA = "1" * 40
BASE_SHA = "2" * 40


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text(
        "services:\n  test:\n    image: python:3.11-slim\n    command: pytest -q\n"
    )
    return repo


def _clean_runtime() -> dict:
    return {
        "teardown": {"attempted": True, "ok": True, "detail": ""},
        "safety": {
            "postflight": "clean",
            "protected_changes": [],
            "unexpected_changes": [],
            "collateral_damage": False,
        },
    }


def _v2_manifest(artifacts_dir, **kwargs) -> dict:
    """A certifiable schema-v2 manifest, shaped like the real writer's output.

    The lifecycle tests are about sequencing, not git state, so the source
    snapshot is pinned clean here. The runner's own capture of start/end, base,
    teardown and artifacts is asserted by the recording tests that use the real
    writer against a real repository.
    """
    mode = kwargs.get("mode") or (
        "acceptance" if kwargs.get("gates_run") else "diagnostic"
    )
    names = kwargs.get("artifacts")
    if names is None:
        names = [
            name
            for name in ("up.stdout.log", "up.stderr.log", "compose.log")
            if (Path(artifacts_dir) / name).is_file()
        ]
    source = {"sha": CANDIDATE_SHA, "status_porcelain": ""}
    base = kwargs.get("base") or {}
    if not base.get("sha"):
        # Non-git lifecycle fixtures cannot resolve a base; the mock stands in
        # for a writer that did (the real capture is asserted separately).
        base = {"ref": base.get("ref") or "main", "kind": "commit", "sha": BASE_SHA}
    coverage = dict(kwargs.get("coverage") or {"status": "pass", "passed": True})
    coverage.update(
        base="main",
        base_sha=base["sha"],
        base_kind=base.get("kind") or "commit",
    )
    return {
        "schema": {"name": "factory-evidence", "version": 2},
        "mode": mode,
        "generated_at": "2026-01-01T00:00:00Z",
        "card_id": kwargs.get("card_id") or "card",
        "project_name": kwargs.get("project_name") or "factory-card-abc123",
        "source": {
            "sha": source["sha"],
            "clean": True,
            "start": dict(source),
            "end": dict(source),
        },
        "git_revision": source["sha"],
        "git_status_porcelain": "",
        "base": base,
        "coverage": coverage,
        "engine": engine_identity(),
        "runtime": kwargs.get("runtime") or _clean_runtime(),
        "artifacts": artifact_records(artifacts_dir, names) if names else [],
        "entry_point": kwargs.get("entry_point") or "docker compose test service",
        "command": kwargs.get("command") or ["docker", "compose", "up"],
        "audit": kwargs.get("audit") or {"status": "pass", "passed": True},
        "datamodel": kwargs.get("datamodel")
        or {"status": "skipped", "reason": "no_data_model"},
        "gates_run": kwargs.get("gates_run"),
        "fixtures": [],
        "execution": kwargs.get("execution")
        or {"exit_code": 0, "passed": True, "stage": "complete"},
        "environment": {"example_env": {"present": True}},
        "hermes_factory_card": "card",
    }


def _snapshots(monkeypatch, before, after):
    snapshots = [before, after]
    monkeypatch.setattr(
        runner.ProtectedProjectSnapshot,
        "capture",
        classmethod(lambda cls: snapshots.pop(0)),
    )


def _wire_successful_run(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        runner, "new_factory_project", lambda _card: "factory-card-abc123"
    )

    def fake_run_compose(*args, **kwargs):
        calls.append(args[2])
        return subprocess.CompletedProcess(args, 0, "run output", "")

    monkeypatch.setattr(runner, "run_compose", fake_run_compose)
    monkeypatch.setattr(
        runner,
        "teardown",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        runner,
        "run_dependency_audit",
        lambda *a, **k: {"status": "pass", "passed": True},
    )
    monkeypatch.setattr(
        runner, "run_coverage_gate", lambda *a, **k: {"status": "pass", "passed": True}
    )
    monkeypatch.setattr(
        runner,
        "run_data_model_gate",
        lambda *a, **k: {"status": "pass", "passed": True},
    )

    def write_manifest(artifacts_dir, repo_path, **kwargs):
        path = Path(artifacts_dir) / "evidence-manifest.json"
        path.write_text(json.dumps(_v2_manifest(artifacts_dir, **kwargs)))
        return path

    monkeypatch.setattr(runner, "write_evidence_manifest", write_manifest)
    return calls


def test_successful_run_collects_logs_tears_down_and_records_evidence(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    calls = _wire_successful_run(monkeypatch, tmp_path)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is True
    assert result.stage == "complete"
    assert calls[0][0] == "up" and calls[1] == ["logs", "--no-color"]
    assert result.artifacts_dir is not None
    assert (Path(result.artifacts_dir) / "up.stdout.log").read_text() == "run output"
    assert result.to_dict()["log"] == ["data-model gate skipped: no data-model.yml"]


def test_teardown_nonzero_is_visible_and_fails_the_run(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner,
        "teardown",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "down failed"),
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    assert result.stage == "complete_with_errors"
    assert result.error is not None
    assert "teardown failed" in result.error
    # Gate skip lines may precede the teardown warning depending on whether
    # data-model.yml exists; the teardown entry is the last line.
    assert result.to_dict()["log"][-1] == "teardown failed with exit code 1"


def test_protected_change_is_incident_and_uses_custom_artifact_root(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    before = ProtectedProjectSnapshot(
        projects={"example-production": [("db", "old", "running")]}
    )
    after = ProtectedProjectSnapshot(
        projects={"example-production": [("db", "new", "running")]}
    )
    _snapshots(monkeypatch, before, after)
    _wire_successful_run(monkeypatch, tmp_path)
    artifacts_root = tmp_path / "custom-artifacts"

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(artifacts_root)
    )

    assert result.stage == "SAFETY_INCIDENT"
    assert result.collateral_damage is True
    assert result.artifacts_dir is not None
    assert (Path(result.artifacts_dir) / "SAFETY_INCIDENT.txt").is_file()
    assert (artifacts_root / "pending-human" / "factory-card-abc123.danger").is_file()


def test_unknown_change_fails_without_creating_a_danger_marker(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    before = ProtectedProjectSnapshot(
        projects={"other-project": [("db", "old", "running")]}
    )
    after = ProtectedProjectSnapshot(
        projects={"other-project": [("db", "new", "running")]}
    )
    _snapshots(monkeypatch, before, after)
    _wire_successful_run(monkeypatch, tmp_path)
    artifacts_root = tmp_path / "artifacts"

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(artifacts_root)
    )

    assert result.passed is False
    assert result.stage == "COLLATERAL_WARNING"
    assert result.artifacts_dir is not None
    assert (Path(result.artifacts_dir) / "COLLATERAL_WARNING.txt").is_file()
    assert not (artifacts_root / "pending-human").exists()


def test_postflight_snapshot_failure_returns_a_failed_result(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        runner, "new_factory_project", lambda _card: "factory-card-abc123"
    )
    calls = [ProtectedProjectSnapshot(), PolicyViolation("docker unavailable")]

    def capture(cls):
        next_item = calls.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    monkeypatch.setattr(
        runner.ProtectedProjectSnapshot, "capture", classmethod(capture)
    )
    _wire_successful_run(monkeypatch, tmp_path)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    assert result.stage == "postflight_failed"
    assert result.error is not None
    assert "docker unavailable" in result.error
    assert result.artifacts_dir is not None
    assert (Path(result.artifacts_dir) / "POSTFLIGHT_FAILURE.txt").is_file()


# --- gate composition (gates run before docker up, fail closed) --


def _gate_results(monkeypatch, audit=None, coverage=None, datamodel=None, exc=None):
    """Mock the three gate functions at the runner module boundary."""
    audit = audit or {"status": "pass", "passed": True}
    coverage = coverage or {"status": "pass", "passed": True}
    datamodel = datamodel or {"status": "pass", "passed": True}

    def fake(name, payload):
        def run(*args, **kwargs):
            if exc and name == exc:
                raise RuntimeError(f"{name} exploded")
            return payload

        return run

    monkeypatch.setattr(runner, "run_dependency_audit", fake("audit", audit))
    monkeypatch.setattr(runner, "run_coverage_gate", fake("coverage", coverage))
    monkeypatch.setattr(runner, "run_data_model_gate", fake("datamodel", datamodel))


def _wired(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    calls = _wire_successful_run(monkeypatch, tmp_path)
    return repo, calls


def test_gates_pass_and_run_records_them(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    # Datamodel gate only runs when a data-model.yml is present.
    (repo / "data-model.yml").write_text("entities: {}\n")
    _gate_results(
        monkeypatch,
        audit={"status": "pass", "passed": True, "findings": []},
        coverage={"status": "pass", "passed": True, "detail": "ok"},
        datamodel={"status": "pass", "passed": True, "detail": "no data-model.yml"},
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is True
    assert result.stage == "complete"
    assert len(calls) == 2  # docker up actually happened
    assert result.gates["audit"]["status"] == "pass"
    assert result.gates["coverage"]["status"] == "pass"
    assert result.gates["datamodel"]["status"] == "pass"
    assert result.to_dict()["gates"]["audit"]["status"] == "pass"


def test_failing_audit_gate_blocks_docker_up(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    _gate_results(
        monkeypatch,
        audit={"status": "fail", "passed": False, "detail": "2 vulnerable deps"},
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert calls == []  # never touched docker
    assert result.passed is False
    assert result.stage == "audit_gate"
    assert "audit gate failed" in (result.error or "")
    assert "2 vulnerable deps" in result.error


def test_failing_coverage_gate_blocks_docker_up(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    _gate_results(
        monkeypatch,
        coverage={"status": "fail", "passed": False, "detail": "uncovered line 42"},
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert calls == []
    assert result.passed is False
    assert result.stage == "coverage_gate"
    assert "coverage gate failed" in (result.error or "")
    assert "uncovered line 42" in result.error


def test_failing_datamodel_gate_blocks_docker_up(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    # The data-model gate only runs when a data-model.yml is present.
    (repo / "data-model.yml").write_text("entities: {}\n")
    _gate_results(
        monkeypatch,
        datamodel={"status": "fail", "passed": False, "detail": "gap: no owner"},
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert calls == []
    assert result.passed is False
    assert result.stage == "datamodel_gate"
    assert "data-model gate failed" in (result.error or "")
    assert "gap: no owner" in result.error


def test_datamodel_gate_skipped_when_no_data_model_file(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    _gate_results(monkeypatch)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is True
    assert len(calls) == 2  # docker up happened
    assert result.gates["datamodel"] == {"status": "skipped", "reason": "no_data_model"}
    assert any("data-model gate skipped" in line for line in result.log)


def test_gate_exception_fails_closed(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    _gate_results(monkeypatch, exc="coverage")

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert calls == []
    assert result.passed is False
    assert result.stage == "coverage_gate"
    assert "coverage" in result.error


def test_audit_gate_exception_fails_closed(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    _gate_results(monkeypatch, exc="audit")

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert calls == []
    assert result.passed is False
    assert result.stage == "audit_gate"
    assert "audit gate failed" in result.error


def test_datamodel_gate_exception_fails_closed(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    (repo / "data-model.yml").write_text("entities: {}\n")
    _gate_results(monkeypatch, exc="datamodel")

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert calls == []
    assert result.passed is False
    assert result.stage == "datamodel_gate"
    assert "data-model gate failed" in result.error


def test_audit_unavailable_runs_but_cannot_certify_acceptance(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    _gate_results(
        monkeypatch,
        audit={"status": "unavailable", "passed": False, "detail": "index offline"},
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    assert result.stage == "evidence_enforcement"
    assert len(calls) == 2  # docker up happened
    assert any("dependency audit unavailable" in line for line in result.log)


def test_coverage_gate_skipped_when_no_added_python(tmp_path, monkeypatch):
    repo, calls = _wired(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_diff_has_added_python", lambda repo, base: False)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is True
    assert len(calls) == 2
    coverage = result.gates["coverage"]
    assert coverage["status"] == "skipped"
    assert coverage["reason"] == "no_added_python"
    # This fixture is not a git repo, so the base cannot resolve -- but the
    # outcome is recorded rather than silently dropped (the resolved-SHA case
    # is asserted in test_acceptance_run_records_source_base_runtime_and_artifacts).
    assert coverage["base_kind"] == "unresolved"
    assert coverage["base_sha"] == ""
    assert any("coverage gate skipped" in line for line in result.log)


def test_run_compose_exception_records_run_failed(tmp_path, monkeypatch):
    repo, _calls = _wired(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner,
        "run_compose",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker exploded")),
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    assert "docker exploded" in result.error


def test_teardown_exception_is_warning_not_crash(tmp_path, monkeypatch):
    repo, _calls = _wired(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner,
        "teardown",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down boom")),
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    assert any("teardown warning" in line for line in result.log)


# ---------------------------------------------------------------------------
# gate-helper coverage : normalize + diff-skip branches
# ---------------------------------------------------------------------------


class _FakeGateResult:
    def __init__(self, status="pass", **extra):
        self.status = status
        self.extra = extra

    def to_dict(self):
        return {"status": self.status, **self.extra}


def test_gate_dict_passes_through_dict():
    assert runner._gate_dict({"status": "pass", "passed": True}) == {
        "status": "pass",
        "passed": True,
    }


def test_gate_dict_normalizes_dataclass_result():
    converted = runner._gate_dict(_FakeGateResult("pass", detail="ok"))
    assert converted == {"status": "pass", "detail": "ok"}


def test_gate_dict_falls_back_to_raw_for_opaque_object():
    converted = runner._gate_dict(object())
    assert converted["raw"] == str(object())


def test_diff_has_added_python_true_when_diff_unavailable(monkeypatch):
    monkeypatch.setattr(
        runner, "_diff_added_lines", lambda repo, base: (None, "no git")
    )
    assert runner._diff_has_added_python(Path("."), "main") is True


def test_diff_has_added_python_true_when_lines_added(monkeypatch):
    monkeypatch.setattr(
        runner, "_diff_added_lines", lambda repo, base: ({"app.py": {1: "x"}}, "")
    )
    assert runner._diff_has_added_python(Path("."), "main") is True


def test_diff_has_added_python_false_when_no_lines_added(monkeypatch):
    monkeypatch.setattr(runner, "_diff_added_lines", lambda repo, base: ({}, ""))
    assert runner._diff_has_added_python(Path("."), "main") is False


# ---------------------------------------------------------------------------
# evidence folding + enforcement
# ---------------------------------------------------------------------------


def test_evidence_manifest_receives_gate_results(tmp_path, monkeypatch, capsys):
    """Gates pass -> manifest kwargs include audit/coverage gate dicts + gates_run."""
    captured = {}
    _wire_successful_run(monkeypatch, tmp_path)
    mock_writer = runner.write_evidence_manifest

    def capturing_writer(artifacts_dir, repo_path, **kwargs):
        captured.update(kwargs)
        return mock_writer(artifacts_dir, repo_path, **kwargs)

    monkeypatch.setattr(runner, "write_evidence_manifest", capturing_writer)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    result = runner.run_factory_card(
        card_id="t_x",
        repo_path=str(_repo(tmp_path)),
        compose_files=["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.passed
    assert captured["gates_run"] is True
    assert captured["audit"]["passed"] is True
    assert captured["coverage"]["passed"] is True


def test_assembled_evidence_rejection_fails_the_run(tmp_path, monkeypatch):
    """A manifest that cannot certify the claim fails the run even if green."""
    manifest_file = tmp_path / "bad-manifest.json"
    # Missing git_revision -> EvidenceError. Distinct filename because
    # _wire_successful_run writes its own valid manifest at the default path.
    manifest_file.write_text(
        json.dumps(
            {
                "entry_point": "docker compose test service",
                "command": "docker compose up",
                "environment": {"example_env": {"present": True}},
                "fixtures": [],
                "execution": {"exit_code": 0, "passed": True, "stage": "complete"},
                "gates_run": True,
                "audit": {"status": "pass"},
                "coverage": {"status": "pass", "passed": True},
                "datamodel": {"status": "skipped", "reason": "no_data_model"},
            }
        )
    )
    _wire_successful_run(monkeypatch, tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    monkeypatch.setattr(
        runner, "write_evidence_manifest", lambda *a, **k: manifest_file
    )
    result = runner.run_factory_card(
        card_id="t_x",
        repo_path=str(_repo(tmp_path)),
        compose_files=["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert not result.passed
    assert result.stage == "evidence_enforcement"
    assert "evidence_enforcement" in result.error
    assert (
        tmp_path / "artifacts" / "factory-card-abc123" / "EVIDENCE_REJECTED.txt"
    ).is_file()


def test_gates_spy_body_executes_when_enabled(tmp_path, monkeypatch):
    """Cover the spy body used by the run_gates=False test: with gates on, the
    same callable is actually invoked, so its body is exercised."""
    calls = []
    manifest_file = tmp_path / "evidence-manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "git_revision": "abc",
                "entry_point": "docker compose test service",
                "command": "docker compose up",
                "environment": {"example_env": {"present": True}},
                "fixtures": [],
                "execution": {"exit_code": 0, "passed": True, "stage": "complete"},
                "gates_run": True,
                "audit": {"status": "pass"},
                "coverage": {"status": "pass", "passed": True},
                "datamodel": {"status": "skipped", "reason": "no_data_model"},
            }
        )
    )
    _wire_successful_run(monkeypatch, tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())

    def _spy(*a, **k):
        calls.append(True)
        return {"status": "pass", "passed": True}

    monkeypatch.setattr(runner, "run_dependency_audit", _spy)
    monkeypatch.setattr(runner, "run_coverage_gate", _spy)
    monkeypatch.setattr(runner, "run_data_model_gate", _spy)

    result = runner.run_factory_card(
        card_id="t_x",
        repo_path=str(_repo(tmp_path)),
        compose_files=["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.passed is True
    assert calls  # the spy body ran at least once


def test_cli_main_no_gates_flag(tmp_path, monkeypatch, capsys):
    """runner CLI: --no-gates maps to run_gates=False and --compose is parsed."""
    manifest_file = tmp_path / "evidence-manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "git_revision": "abc",
                "entry_point": "docker compose test service",
                "command": "docker compose up",
                "environment": {"example_env": {"present": True}},
                "fixtures": [],
                "execution": {"exit_code": 0, "passed": True, "stage": "complete"},
                "gates_run": True,
                "audit": {"status": "pass"},
                "coverage": {"status": "pass", "passed": True},
                "datamodel": {"status": "skipped", "reason": "no_data_model"},
            }
        )
    )
    _wire_successful_run(monkeypatch, tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    repo = _git_repo(tmp_path)
    gate_called = []

    def _spy(*a, **k):
        gate_called.append(True)
        return {"status": "pass", "passed": True}

    monkeypatch.setattr(runner, "run_dependency_audit", _spy)
    monkeypatch.setattr(runner, "run_coverage_gate", _spy)
    monkeypatch.setattr(runner, "run_data_model_gate", _spy)

    rc = runner.main(
        [
            "--card",
            "t_x",
            "--repo",
            str(repo),
            "--compose",
            "compose.yml",
            "--no-gates",
            "--artifacts-root",
            str(tmp_path / "artifacts"),
        ]
    )
    assert rc == 1  # diagnostic run cannot certify acceptance without gates
    assert not gate_called  # --no-gates means no gate calls


def test_run_gates_false_marks_second_class(tmp_path, monkeypatch):
    """run_gates=False: no gate calls, result + manifest record gates_run=False."""
    gate_called = []

    def _spy(*a, **k):
        gate_called.append(True)
        return {"status": "pass", "passed": True}

    manifest_file = tmp_path / "evidence-manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "git_revision": "abc",
                "entry_point": "docker compose test service",
                "command": "docker compose up",
                "environment": {"example_env": {"present": True}},
                "fixtures": [],
                "execution": {"exit_code": 0, "passed": True, "stage": "complete"},
                "gates_run": True,
                "audit": {"status": "pass"},
                "coverage": {"status": "pass", "passed": True},
                "datamodel": {"status": "skipped", "reason": "no_data_model"},
            }
        )
    )
    _wire_successful_run(monkeypatch, tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    monkeypatch.setattr(runner, "run_dependency_audit", _spy)
    monkeypatch.setattr(runner, "run_coverage_gate", _spy)
    monkeypatch.setattr(runner, "run_data_model_gate", _spy)
    captured = {}
    monkeypatch.setattr(
        runner,
        "write_evidence_manifest",
        lambda *a, **k: captured.update(gates_run=k.get("gates_run")) or manifest_file,
    )
    result = runner.run_factory_card(
        card_id="t_x",
        repo_path=str(_repo(tmp_path)),
        compose_files=["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
        run_gates=False,
    )
    assert not gate_called
    assert result.gates_run is False
    assert captured["gates_run"] is False
    assert any("GATES SKIPPED" in line for line in result.log)


def _git_repo(tmp_path):
    """A real git repo with .env.example + compose.yml so write_evidence_manifest
    (git rev-parse HEAD, env snapshot) can run for real on the runner path."""
    repo = _repo(tmp_path)
    (repo / ".env.example").write_text("REQUIRE_TLS=false\n")
    (repo / ".env").write_text("REQUIRE_TLS=false\n")
    # A tracked source file so a test can dirty the tree during a run.
    (repo / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def test_all_gates_pass_manifest_on_disk_passes_assembled_evidence(
    tmp_path, monkeypatch
):
    """Real write_evidence_manifest: on-disk manifest folds non-empty audit/coverage
    and survives assert_assembled_evidence on read-back."""
    repo = _git_repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    # Let the real write_evidence_manifest run (override the helper's mock).
    from factory.evidence import write_evidence_manifest as real_manifest

    monkeypatch.setattr(runner, "write_evidence_manifest", real_manifest)
    monkeypatch.setattr(
        runner,
        "run_dependency_audit",
        lambda *a, **k: {"status": "pass", "passed": True, "findings": []},
    )
    monkeypatch.setattr(
        runner,
        "run_coverage_gate",
        lambda *a, **k: {"status": "pass", "passed": True, "detail": "ok"},
    )
    monkeypatch.setattr(
        runner,
        "run_data_model_gate",
        lambda *a, **k: {"status": "pass", "passed": True, "detail": "ok"},
    )
    # Force the coverage gate to run (a real fresh repo has no added python
    # lines vs main, which would skip it); we want its pass folded in.
    monkeypatch.setattr(runner, "_diff_has_added_python", lambda repo, base: True)

    result = runner.run_factory_card(
        card_id="t_x",
        repo_path=str(repo),
        compose_files=["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.passed is True
    assert result.stage == "complete"
    assert result.artifacts_dir is not None

    manifest_file = Path(result.artifacts_dir) / "evidence-manifest.json"
    assert manifest_file.is_file()
    manifest = json.loads(manifest_file.read_text())
    assert manifest["audit"] and manifest["coverage"]  # non-empty gate sections
    assert manifest["gates_run"] is True
    assert_assembled_evidence(manifest)  # legacy reader still passes read-back


@pytest.mark.parametrize("ungated", [True, False])
def test_real_manifest_preserves_diagnostics_but_rejects_uncertified_runs(
    tmp_path, monkeypatch, ungated
):
    from factory.evidence import EvidenceError, write_evidence_manifest

    repo = _git_repo(tmp_path)
    _wire_successful_run(monkeypatch, tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    monkeypatch.setattr(runner, "write_evidence_manifest", write_evidence_manifest)
    monkeypatch.setattr(
        runner, "run_dependency_audit", lambda *a, **k: {"status": "unavailable"}
    )
    result = runner.run_factory_card(
        "diagnostic",
        str(repo),
        ["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
        run_gates=not ungated,
    )
    assert not result.passed
    assert result.stage == "evidence_enforcement"
    assert result.exit_code == 0
    manifest = json.loads(
        (Path(result.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    with pytest.raises(EvidenceError):
        assert_assembled_evidence(manifest)


# ---------------------------------------------------------------------------
# compose/env path resolution (repo-relative vs as-given)
# ---------------------------------------------------------------------------


def _compose_repo(tmp_path):
    """A repo with a root compose.yml plus an arbitrary extra compose file."""
    repo = _repo(tmp_path)
    (repo / "override.yml").write_text(
        "services:\n  test:\n    image: python:3.11-slim\n    command: pytest -q\n"
    )
    return repo


def test_resolve_input_path_repo_relative(tmp_path):
    repo = _compose_repo(tmp_path)
    resolved = runner._resolve_input_path(repo, "override.yml", "compose file")
    assert resolved == (repo / "override.yml").resolve()
    assert resolved.is_file()


def test_resolve_input_path_absolute(tmp_path):
    repo = _compose_repo(tmp_path)
    absolute = str((repo / "override.yml").resolve())
    resolved = runner._resolve_input_path(repo, absolute, "compose file")
    assert resolved == Path(absolute)
    assert resolved.is_file()


def test_resolve_input_path_cwd_relative_existing_used_as_is(tmp_path, monkeypatch):
    # A compose path that already exists relative to the cwd (i.e. was passed
    # as-given) must be used as-is, not re-joined onto --repo.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "docker-compose.yml").write_text(
        "services:\n  test:\n    image: python:3.11-slim\n"
    )
    repo = _compose_repo(tmp_path)
    monkeypatch.chdir(outside)
    resolved = runner._resolve_input_path(repo, "docker-compose.yml", "compose file")
    assert resolved == (outside / "docker-compose.yml").resolve()


def test_resolve_input_path_not_found_names_both_tries_and_repo_relative(tmp_path):
    repo = _compose_repo(tmp_path)
    with pytest.raises(PolicyViolation) as exc:
        runner._resolve_input_path(repo, "nope.yml", "compose file")
    msg = str(exc.value)
    assert str(repo / "nope.yml") in msg
    assert "nope.yml" in msg
    assert "repo-relative" in msg


def test_resolve_input_path_not_required_falls_back_to_repo_relative(tmp_path):
    repo = _compose_repo(tmp_path)
    resolved = runner._resolve_input_path(
        repo, "missing.env", "env file", required=False
    )
    assert resolved == (repo / "missing.env").resolve()
    assert not resolved.exists()


def test_load_compose_files_accepts_resolved_absolute_paths(tmp_path):
    repo = _compose_repo(tmp_path)
    resolved = runner._resolve_input_path(repo, "compose.yml", "compose file")
    merged = runner._load_compose_files([resolved])
    assert "test" in merged["services"]


def test_run_factory_card_double_join_compose_path_uses_as_given(tmp_path, monkeypatch):
    # Regression for passing a compose path that is already
    # relative to the cwd (as-given) must not be double-joined onto --repo.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "docker-compose.yml").write_text(
        "services:\n  test:\n    image: python:3.11-slim\n    command: pytest -q\n"
    )
    repo = _repo(tmp_path)  # has only root compose.yml
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    monkeypatch.chdir(outside)

    compose_lists = []

    def fake_run_compose(*args, **kwargs):
        compose_lists.append(args[1])  # resolved compose-file list
        return subprocess.CompletedProcess(args, 0, "run output", "")

    monkeypatch.setattr(runner, "run_compose", fake_run_compose)

    result = runner.run_factory_card(
        "card",
        str(repo),
        ["docker-compose.yml"],  # exists as-given in cwd, not in repo
        artifacts_root=str(tmp_path / "artifacts"),
    )

    assert result.passed is True
    assert result.stage == "complete"
    # The compose file passed to docker compose up (first run_compose call)
    # is the cwd-relative file, resolved and not double-joined onto --repo.
    assert compose_lists[0] == [str((outside / "docker-compose.yml").resolve())]


def test_run_factory_card_absolute_compose_path(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "override.yml").write_text(
        "services:\n  test:\n    image: python:3.11-slim\n    command: pytest -q\n"
    )
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)

    result = runner.run_factory_card(
        "card",
        str(repo),
        [str((repo / "override.yml").resolve())],
        artifacts_root=str(tmp_path / "artifacts"),
    )

    assert result.passed is True
    assert result.stage == "complete"


def test_run_factory_card_compose_not_found_raises_policy_violation(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(PolicyViolation) as exc:
        runner.run_factory_card(
            "card",
            str(repo),
            ["does-not-exist.yml"],
            artifacts_root=str(tmp_path / "artifacts"),
        )
    msg = str(exc.value)
    assert str(repo / "does-not-exist.yml") in msg
    assert "repo-relative" in msg


# ---------------------------------------------------------------------------
# schema-v2 recording: mode, source start/end, base SHA, runtime, artifacts
# ---------------------------------------------------------------------------


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_cli_rejects_explicit_acceptance_with_no_gates_before_any_work(
    tmp_path, monkeypatch, capsys
):
    """--no-gates --mode acceptance is a usage error before gates or Docker."""
    repo = _repo(tmp_path)
    touched = []

    def spy(name, payload):
        def run(*args, **kwargs):
            touched.append(name)
            return payload

        return run

    monkeypatch.setattr(
        runner, "run_dependency_audit", spy("audit", {"status": "pass"})
    )
    monkeypatch.setattr(
        runner,
        "run_coverage_gate",
        spy("coverage", {"status": "pass", "passed": True}),
    )
    monkeypatch.setattr(
        runner, "run_data_model_gate", spy("datamodel", {"status": "pass"})
    )
    monkeypatch.setattr(
        runner,
        "run_compose",
        spy("docker", subprocess.CompletedProcess([], 0, "", "")),
    )

    rc = runner.main(
        [
            "--card",
            "t_x",
            "--repo",
            str(repo),
            "--compose",
            "compose.yml",
            "--no-gates",
            "--mode",
            "acceptance",
            "--artifacts-root",
            str(tmp_path / "artifacts"),
        ]
    )

    assert rc == 2
    assert touched == []
    assert not (tmp_path / "artifacts").exists()
    assert "--mode acceptance" in capsys.readouterr().err


def test_run_factory_card_refuses_acceptance_mode_without_gates(tmp_path):
    with pytest.raises(ValueError, match="acceptance"):
        runner.run_factory_card(
            "card",
            str(_repo(tmp_path)),
            ["compose.yml"],
            artifacts_root=str(tmp_path / "artifacts"),
            run_gates=False,
            mode="acceptance",
        )


def test_acceptance_run_records_source_base_runtime_and_artifacts(
    tmp_path, monkeypatch
):
    """The writer receives the run's real start/end snapshots, base, teardown, safety."""
    repo = _git_repo(tmp_path)
    head = _head(repo)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    mock_writer = runner.write_evidence_manifest
    captured = {}

    def capturing_writer(artifacts_dir, repo_path, **kwargs):
        captured.update(kwargs)
        return mock_writer(artifacts_dir, repo_path, **kwargs)

    monkeypatch.setattr(runner, "write_evidence_manifest", capturing_writer)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is True
    assert result.mode == "acceptance"
    assert captured["mode"] == "acceptance"
    assert captured["source_start"] == {"sha": head, "status_porcelain": ""}
    assert captured["source_end"] == captured["source_start"]
    assert captured["base"] == {"ref": "main", "kind": "commit", "sha": head}
    assert captured["runtime"]["teardown"] == {
        "attempted": True,
        "ok": True,
        "detail": "",
    }
    assert captured["runtime"]["safety"] == {
        "postflight": "clean",
        "protected_changes": [],
        "unexpected_changes": [],
        "collateral_damage": False,
    }
    assert captured["artifacts"] == [
        "up.stdout.log",
        "up.stderr.log",
        "compose.log",
    ]
    assert captured["execution"]["exit_code"] == 0
    assert captured["execution"]["stage"] == "complete"
    assert captured["project_name"] == "factory-card-abc123"
    # The coverage gate was measured against the captured immutable base SHA.
    assert captured["coverage"]["base_sha"] == head
    assert captured["coverage"]["base_kind"] == "commit"


def test_diagnostic_mode_records_mode_and_never_certifies(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)

    result = runner.run_factory_card(
        "card",
        str(repo),
        ["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
        run_gates=False,
    )

    assert result.mode == "diagnostic"
    assert result.gates_run is False
    assert result.passed is False
    assert result.stage == "evidence_enforcement"
    assert "mode_not_acceptance" in (result.error or "")
    manifest = json.loads(
        (Path(result.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    assert manifest["mode"] == "diagnostic"
    assert manifest["gates_run"] is False


def test_cli_explicit_diagnostic_mode_with_gates_cannot_certify(
    tmp_path, monkeypatch, capsys
):
    repo = _git_repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)

    rc = runner.main(
        [
            "--card",
            "t_x",
            "--repo",
            str(repo),
            "--compose",
            "compose.yml",
            "--mode",
            "diagnostic",
            "--artifacts-root",
            str(tmp_path / "artifacts"),
        ]
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "diagnostic"
    assert payload["gates_run"] is True
    assert payload["passed"] is False
    manifest = json.loads(
        (Path(payload["artifacts_dir"]) / "evidence-manifest.json").read_text()
    )
    assert manifest["mode"] == "diagnostic"


def test_source_change_during_run_is_recorded_and_rejected(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "write_evidence_manifest", real_evidence_manifest)

    def dirtying_teardown(*args, **kwargs):
        (repo / "app.py").write_text("value = 999\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runner, "teardown", dirtying_teardown)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    assert result.stage == "evidence_enforcement"
    assert "source_changed_during_run" in (result.error or "")
    manifest = json.loads(
        (Path(result.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    assert manifest["source"]["clean"] is False
    assert manifest["source"]["end"]["status_porcelain"] != ""
    assert manifest["source"]["start"]["status_porcelain"] == ""


def test_teardown_failure_is_recorded_in_the_manifest(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "write_evidence_manifest", real_evidence_manifest)
    monkeypatch.setattr(
        runner,
        "teardown",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "down failed"),
    )

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    manifest = json.loads(
        (Path(result.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    assert manifest["runtime"]["teardown"]["attempted"] is True
    assert manifest["runtime"]["teardown"]["ok"] is False
    assert "1" in manifest["runtime"]["teardown"]["detail"]


def test_runner_enforcement_requires_schema_v2_not_the_legacy_reader(
    tmp_path, monkeypatch
):
    """A v1 manifest the legacy reader accepts still cannot certify a run."""
    repo = _git_repo(tmp_path)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    legacy_manifest = {
        "git_revision": "abc",
        "entry_point": "docker compose test service",
        "command": "docker compose up",
        "environment": {"example_env": {"present": True}},
        "fixtures": [],
        "execution": {"exit_code": 0, "passed": True, "stage": "complete"},
        "gates_run": True,
        "audit": {"status": "pass"},
        "coverage": {"status": "pass", "passed": True},
        "datamodel": {"status": "skipped", "reason": "no_data_model"},
    }
    assert assert_assembled_evidence(legacy_manifest) is None  # old reader
    legacy_path = tmp_path / "legacy-evidence.json"
    legacy_path.write_text(json.dumps(legacy_manifest))
    monkeypatch.setattr(runner, "write_evidence_manifest", lambda *a, **k: legacy_path)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert result.passed is False
    assert result.stage == "evidence_enforcement"
    assert "legacy_schema_unsupported" in (result.error or "")


def test_diff_base_is_captured_before_a_gate_can_move_the_source(tmp_path, monkeypatch):
    """Correction 4: resolving the base at the end could not bind the measurement."""
    repo = _git_repo(tmp_path)
    head_before = _head(repo)
    _snapshots(monkeypatch, ProtectedProjectSnapshot(), ProtectedProjectSnapshot())
    _wire_successful_run(monkeypatch, tmp_path)
    captured = {}

    def capturing_writer(artifacts_dir, repo_path, **kwargs):
        captured.update(kwargs)
        return real_evidence_manifest(artifacts_dir, repo_path, **kwargs)

    monkeypatch.setattr(runner, "write_evidence_manifest", capturing_writer)

    def moving_audit(*args, **kwargs):
        # A gate that advances HEAD must not move the recorded diff base.
        (repo / "app.py").write_text("moved by gate\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                "commit",
                "-qm",
                "gate moved head",
            ],
            cwd=repo,
            check=True,
        )
        return {"status": "pass", "passed": True}

    monkeypatch.setattr(runner, "run_dependency_audit", moving_audit)

    result = runner.run_factory_card(
        "card", str(repo), ["compose.yml"], artifacts_root=str(tmp_path / "artifacts")
    )

    assert captured["base"]["sha"] == head_before
    assert captured["coverage"]["base_sha"] == head_before
    assert captured["source_start"]["sha"] == head_before
    assert captured["source_end"]["sha"] != head_before
    assert result.passed is False
    assert result.stage == "evidence_enforcement"
    assert "source_changed_during_run" in (result.error or "")
