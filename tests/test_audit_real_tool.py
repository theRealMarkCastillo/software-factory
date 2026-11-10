"""Real-tool acceptance tests for ``factory.audit``.

These tests execute the actual pip-audit binary against isolated, throwaway
virtual environments and lock files. They are not mocks: the clean/vulnerable
results come from the live vulnerability database (PyPI JSON API), so they
also record tool/source provenance.

They skip only when the prerequisites are genuinely absent (no pip-audit, no
uv, or no PyPI connectivity to build the fixture environment); a skip is
reported by pytest as a skip, never as a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import audit


def _pip_audit() -> str | None:
    candidates = [
        os.environ.get("FACTORY_TEST_PIP_AUDIT"),
        str(Path(sys.executable).parent / "pip-audit"),
        shutil.which("pip-audit"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


PIP_AUDIT = _pip_audit()
UV = shutil.which("uv")
requires_tool = pytest.mark.skipif(
    PIP_AUDIT is None, reason="pip-audit is not installed in this environment"
)
requires_uv = pytest.mark.skipif(UV is None, reason="uv is required to build fixtures")


def _run(command, **kwargs):
    return subprocess.run(
        command, capture_output=True, text=True, timeout=600, **kwargs
    )


def _uv_venv(path: Path) -> Path:
    completed = _run([UV, "venv", "--python", "3.12", str(path)])
    if completed.returncode != 0:
        pytest.skip(f"could not create fixture venv: {completed.stderr[-400:]}")
    return path / "bin" / "python"


def _uv_install(python: Path, *requirements: str) -> None:
    completed = _run([UV, "pip", "install", "--python", str(python), *requirements])
    if completed.returncode != 0:
        pytest.skip(f"could not install fixture packages: {completed.stderr[-400:]}")


@pytest.fixture(scope="module")
def vulnerable_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("vuln-env")
    python = _uv_venv(
        root / ".venv",
    )
    _uv_install(python, "requests==2.6.0")
    return root / ".venv"


@pytest.fixture(scope="module")
def clean_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("clean-env")
    python = _uv_venv(root / ".venv")
    _uv_install(python, "iniconfig==2.1.0")
    return root / ".venv"


@pytest.fixture(scope="module")
def local_project(tmp_path_factory):
    root = tmp_path_factory.mktemp("local-project")
    project = root / "repo"
    (project / "src" / "factory_audit_local_fixture").mkdir(parents=True)
    (project / "src" / "factory_audit_local_fixture" / "__init__.py").write_text(
        "VALUE = 1\n"
    )
    (project / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "factory-audit-local-fixture"\n'
        'version = "0.1.0"\n'
        'dependencies = ["iniconfig==2.1.0"]\n\n'
        "[tool.setuptools.packages.find]\n"
        'where = ["src"]\n'
    )
    python = _uv_venv(root / ".venv")
    completed = _run(
        [UV, "pip", "install", "--python", str(python), "-e", str(project)]
    )
    if completed.returncode != 0:
        pytest.skip(
            f"could not install the editable fixture: {completed.stderr[-400:]}"
        )
    return project, root / ".venv"


@requires_tool
@requires_uv
def test_real_clean_environment_passes(clean_env, monkeypatch):
    monkeypatch.setenv("FACTORY_PIP_AUDIT", PIP_AUDIT)
    repo = clean_env.parent
    result = audit.run_dependency_audit(str(repo), "python", str(clean_env))
    assert result.status == "pass", result.detail
    assert result.audited >= 1
    assert result.tool_version != ""
    assert result.report_sha256


@requires_tool
@requires_uv
def test_real_vulnerable_environment_fails_with_advisory_details(
    vulnerable_env, monkeypatch, capsys
):
    monkeypatch.setenv("FACTORY_PIP_AUDIT", PIP_AUDIT)
    repo = vulnerable_env.parent
    result = audit.run_dependency_audit(str(repo), "python", str(vulnerable_env))
    assert result.status == "fail", result.detail
    names = [entry["name"] for entry in result.findings]
    assert "requests" in names
    ids = [vuln["id"] for entry in result.findings for vuln in entry["vulns"]]
    assert ids, "a real vulnerable fixture must return at least one advisory id"
    assert audit.main(["--repo", str(repo), "--venv", str(vulnerable_env)]) == 1
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["passed"] is False
    assert emitted["result"]["findings"]


@requires_tool
@requires_uv
def test_real_local_project_skip_is_reported_and_passes(local_project, monkeypatch):
    project, venv = local_project
    monkeypatch.setenv("FACTORY_PIP_AUDIT", PIP_AUDIT)
    result = audit.run_dependency_audit(str(project), "python", str(venv))
    assert result.status == "pass", result.detail
    assert any(item["name"] == "factory-audit-local-fixture" for item in result.skipped)
    assert "factory-audit-local-fixture" in result.detail


@requires_tool
@requires_uv
def test_real_unexpected_skip_blocks(local_project, tmp_path, monkeypatch):
    _project, venv = local_project
    other = tmp_path / "other-repo"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\nname = "some-other-project"\n')
    monkeypatch.setenv("FACTORY_PIP_AUDIT", PIP_AUDIT)
    result = audit.run_dependency_audit(str(other), "python", str(venv))
    assert result.status == "invalid", result.detail
    assert "factory-audit-local-fixture" in result.detail


def _write_uv_lock(repo: Path, pins: list[tuple[str, str]], project="lock-fixture"):
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{project}"\n')
    entries = [
        "version = 1",
        "revision = 1",
        'requires-python = ">=3.11"',
        "",
        "[[package]]",
        f'name = "{project}"',
        'version = "0.1.0"',
        'source = { virtual = "." }',
        "",
    ]
    for name, version in pins:
        entries += [
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            'source = { registry = "https://pypi.org/simple" }',
            "",
        ]
    (repo / "uv.lock").write_text("\n".join(entries))


@requires_tool
def test_real_lockfile_vulnerability_is_reported(tmp_path, monkeypatch):
    repo = tmp_path / "lock-vuln"
    repo.mkdir()
    _write_uv_lock(repo, [("requests", "2.6.0")])
    monkeypatch.setenv("FACTORY_PIP_AUDIT", PIP_AUDIT)
    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "fail", result.detail
    assert [entry["name"] for entry in result.findings] == ["requests"]
    assert result.lock_sha256 and len(result.lock_sha256) == 64


@requires_tool
def test_real_lockfile_clean_passes(tmp_path, monkeypatch):
    repo = tmp_path / "lock-clean"
    repo.mkdir()
    _write_uv_lock(repo, [("iniconfig", "2.1.0")])
    monkeypatch.setenv("FACTORY_PIP_AUDIT", PIP_AUDIT)
    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "pass", result.detail
    assert result.audited == 1


def _fake_tool(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-pip-audit"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@requires_uv
def test_real_present_tool_with_empty_output_fails_closed(
    clean_env, tmp_path, monkeypatch, capsys
):
    """Injection of an empty report from a present tool must not pass."""
    tool = _fake_tool(tmp_path, "exit 1\n")
    monkeypatch.setenv("FACTORY_PIP_AUDIT", str(tool))
    repo = clean_env.parent
    result = audit.run_dependency_audit(str(repo), "python", str(clean_env))
    assert result.status == "invalid"
    assert "unusable report" in result.detail
    assert audit.main(["--repo", str(repo), "--venv", str(clean_env)]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


@requires_uv
def test_real_present_tool_with_usage_error_fails_closed(
    clean_env, tmp_path, monkeypatch
):
    tool = _fake_tool(
        tmp_path, "echo \"ERROR: Cannot combine '--path' with '--local'\" >&2\nexit 2\n"
    )
    monkeypatch.setenv("FACTORY_PIP_AUDIT", str(tool))
    result = audit.run_dependency_audit(str(clean_env.parent), "python", str(clean_env))
    assert result.status == "invalid"
    assert "exited 2" in result.detail


@requires_tool
@requires_uv
def test_real_installed_command_clean_pass_and_vulnerable_nonpass(
    clean_env, vulnerable_env, monkeypatch
):
    """Run the installed ``factory-audit`` console script end to end, with the
    audit override unset so the gate must use its own discovery."""
    script = Path(sys.executable).parent / "factory-audit"
    if not script.is_file():
        pytest.skip("factory-audit console script is not installed here")
    monkeypatch.delenv("FACTORY_PIP_AUDIT", raising=False)

    clean = _run(
        [str(script), "--repo", str(clean_env.parent), "--venv", str(clean_env)]
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    clean_payload = json.loads(clean.stdout)
    assert clean_payload["passed"] is True
    assert clean_payload["result"]["status"] == "pass"

    vulnerable = _run(
        [
            str(script),
            "--repo",
            str(vulnerable_env.parent),
            "--venv",
            str(vulnerable_env),
        ]
    )
    assert vulnerable.returncode != 0, vulnerable.stdout
    payload = json.loads(vulnerable.stdout)
    assert payload["passed"] is False
    assert payload["result"]["status"] == "fail"
    assert payload["result"]["findings"]
