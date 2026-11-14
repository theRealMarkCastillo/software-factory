"""Offline runner regressions: real Git/evidence, mocked gates and Docker."""

import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest

from factory import evidence, policy, runner, verify


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def subject(tmp_path):
    repo = tmp_path / "subject"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "compose.yml").write_text("services:\n  test:\n    image: example:test\n")
    (repo / ".env.example").write_text("EXAMPLE=value\n")
    (repo / "app.py").write_text("value = 1\n")
    base = _commit(repo, "base")
    _git(repo, "branch", "slice-base")
    (repo / "app.py").write_text("value = 2\n")
    candidate = _commit(repo, "candidate")
    return repo, base, candidate


@pytest.fixture
def offline_runtime(monkeypatch):
    calls = []

    def compose(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess([], 0, "offline fixture output", "")

    monkeypatch.setattr(runner, "run_compose", compose)
    monkeypatch.setattr(runner, "teardown", compose)
    monkeypatch.setattr(
        runner.ProtectedProjectSnapshot,
        "capture",
        classmethod(lambda cls: policy.ProtectedProjectSnapshot()),
    )
    monkeypatch.setattr(
        runner,
        "run_dependency_audit",
        lambda *a, **k: {"status": "pass", "passed": True},
    )
    monkeypatch.setattr(
        runner, "run_coverage_gate", lambda *a, **k: {"status": "pass", "passed": True}
    )
    return calls


def _argv(repo, tmp_path):
    return [
        "--card",
        "regression",
        "--repo",
        str(repo),
        "--compose",
        "compose.yml",
        "--artifacts-root",
        str(tmp_path / "artifacts"),
    ]


def test_cli_base_pins_coverage_and_verifiable_evidence(
    subject, offline_runtime, monkeypatch, tmp_path, capsys
):
    repo, base, candidate = subject
    resolutions = []
    real_resolve = runner.resolve_base_info

    def resolve(*args):
        resolutions.append(args)
        return real_resolve(*args)

    monkeypatch.setattr(runner, "resolve_base_info", resolve)

    def audit(*args, **kwargs):
        # The symbolic base moves after capture; coverage must not follow it.
        _git(repo, "branch", "-f", "slice-base", candidate)
        return {"status": "pass", "passed": True}

    monkeypatch.setattr(runner, "run_dependency_audit", audit)
    measured = []

    def coverage(*args, **kwargs):
        measured.append(kwargs["base"])
        return {"status": "pass", "passed": True}

    monkeypatch.setattr(runner, "run_coverage_gate", coverage)
    assert runner.main(_argv(repo, tmp_path) + ["--base", "slice-base"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(resolutions) == 1
    assert measured == [base]
    artifacts = Path(result["artifacts_dir"])
    manifest = json.loads((artifacts / evidence.MANIFEST_FILENAME).read_text())
    assert manifest["base"] == {"ref": "slice-base", "kind": "commit", "sha": base}
    assert manifest["coverage"]["base_sha"] == base
    args = [
        "--artifacts-dir",
        str(artifacts),
        "--repo",
        str(repo),
        "--candidate",
        candidate,
        "--json",
        "--base",
    ]
    assert verify.main(args + [base]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert verify.main(args + [candidate]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "base_mismatch"


@pytest.mark.parametrize("gates", [True, False])
def test_unknown_base_refused_before_gates_or_docker(
    subject, offline_runtime, monkeypatch, tmp_path, gates
):
    repo, _, _ = subject
    touched = []
    monkeypatch.setattr(
        runner, "_run_preflight_gates", lambda *a: touched.append("gate")
    )
    result = runner.run_factory_card(
        "regression",
        str(repo),
        ["compose.yml"],
        base_ref="missing-ref",
        run_gates=gates,
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.passed is False
    assert result.stage == "base_resolution"
    assert "missing-ref" in result.error
    assert touched == []
    assert offline_runtime == []
    assert not (tmp_path / "artifacts").exists()


def test_cli_defaults_base_to_main(subject, offline_runtime, tmp_path, capsys):
    repo, _, candidate = subject
    assert runner.main(_argv(repo, tmp_path)) == 0
    result = json.loads(capsys.readouterr().out)
    manifest = json.loads(
        (Path(result["artifacts_dir"]) / evidence.MANIFEST_FILENAME).read_text()
    )
    assert manifest["base"] == {"ref": "main", "kind": "commit", "sha": candidate}


def test_external_env_file_is_private_hash_only_and_not_injected(
    subject, offline_runtime, tmp_path, capsys
):
    repo, _, _ = subject
    external = tmp_path / "private-runtime.env"
    external.write_text("PRIVATE_SETTING=not-for-evidence\n")
    assert runner.main(_argv(repo, tmp_path) + ["--env-file", str(external)]) == 0
    result = json.loads(capsys.readouterr().out)
    manifest_path = Path(result["artifacts_dir"]) / evidence.MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text())
    assert manifest["environment"]["runtime_env"] == {
        "location": "external",
        "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
    }
    assert str(external) not in manifest_path.read_text()
    assert external.name not in manifest_path.read_text()
    assert "PRIVATE_SETTING" not in manifest_path.read_text()
    assert all("--env-file" not in str(call) for call in offline_runtime)


@pytest.mark.parametrize("inside_dirty", [False, True])
def test_subdirectory_cleanliness_ignores_parent_but_rejects_inside(
    subject, offline_runtime, tmp_path, inside_dirty
):
    repo, _, _ = subject
    subdir = repo / "examples" / "minimal"
    subdir.mkdir(parents=True)
    (subdir / "compose.yml").write_text((repo / "compose.yml").read_text())
    (subdir / ".env.example").write_text("EXAMPLE=value\n")
    head = _commit(repo, "example")
    (repo / "app.py").write_text("unrelated parent change\n")
    (repo / "untracked-parent.txt").write_text("unrelated untracked file\n")
    if inside_dirty:
        (subdir / "compose.yml").write_text(
            "services:\n  test:\n    image: example:changed\n"
        )
    source = evidence.capture_source(subdir)
    assert source["sha"] == head
    assert bool(source["status_porcelain"]) is inside_dirty
    result = runner.run_factory_card(
        "regression",
        str(subdir),
        ["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.passed is not inside_dirty
    if inside_dirty:
        assert "source_dirty" in result.error


@pytest.mark.parametrize("selection", ["factory", "xdg", "home"])
def test_port_lock_uses_private_user_state_not_package(
    tmp_path, monkeypatch, selection
):
    monkeypatch.delenv("FACTORY_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    package = tmp_path / "readonly-package"
    package.mkdir(mode=0o555)
    monkeypatch.setattr(policy, "__file__", str(package / "policy.py"))
    if selection == "factory":
        state = tmp_path / "explicit-state"
        monkeypatch.setenv("FACTORY_STATE_DIR", str(state))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-xdg"))
    elif selection == "xdg":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        state = tmp_path / "xdg" / "software-factory"
    else:
        state = home / ".local" / "state" / "software-factory"
    monkeypatch.setattr(policy, "_is_factory_port_free", lambda port: True)
    with policy._acquire_port_lock():
        assert (state / "ports" / ".lock").is_file()
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert not (state / "ports" / ".lock").exists()
    assert policy.allocate_host_ports(2) == [49000, 49001]
    assert list(package.iterdir()) == []
