"""Unit tests for factory.audit. Mock subprocess so tests are deterministic
(no network, no real pip-audit dependency), matching the user's preference
for synthetic fixture tests before live-system runs."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from factory import audit


class FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_pip_env_payload(vulns):
    deps = []
    if vulns:
        deps.append(
            {
                "name": "pytest",
                "version": "8.4.2",
                "vulns": [{"id": "PYSEC-2026-1845", "fix_versions": ["9.0.3"]}],
            }
        )
    return {"dependencies": deps}


def _tmp_venv(tmp_path, with_pip=True):
    """A minimal site-packages layout, optionally with a pip executable."""
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    if with_pip:
        (venv / "bin" / "pip").write_text("x")  # existence marker
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    return venv


def _repo_with(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return repo


def test_no_repo_path_returns_unavailable(tmp_path):
    r = audit.run_dependency_audit(str(tmp_path / "nonexistent"), "python")
    assert r.status == "unavailable"


def test_no_manifest_and_no_venv_returns_unavailable(tmp_path):
    repo = _repo_with(tmp_path, {})
    r = audit.run_dependency_audit(str(repo), "python", "")
    assert r.status == "no_manifest"


def test_python_env_scan_reports_fail_on_vulns(tmp_path, monkeypatch):
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(1, stdout=json.dumps(make_pip_env_payload(True))),
    )
    r = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert r.status == "fail"
    assert r.findings and r.findings[0]["name"] == "pytest"


def test_python_env_scan_reports_pass_when_clean(tmp_path, monkeypatch):
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(0, stdout=json.dumps(make_pip_env_payload(False))),
    )
    r = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert r.status == "pass"


@pytest.mark.parametrize("vulnerable", [False, True])
def test_pip_free_environment_is_audited(tmp_path, monkeypatch, vulnerable):
    import venv as stdlib_venv

    venv = tmp_path / "pip-free"
    stdlib_venv.EnvBuilder(with_pip=False).create(venv)
    site_packages = next((venv / "lib").glob("python*/site-packages"))
    assert not (venv / "bin" / "pip").exists()
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    calls = []

    def scan(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[cmd.index("--path") + 1] == str(site_packages)
        return FakeProc(
            int(vulnerable), stdout=json.dumps(make_pip_env_payload(vulnerable))
        )

    monkeypatch.setattr(audit.subprocess, "run", scan)
    r = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert r.status == ("fail" if vulnerable else "pass")
    assert len(calls) == 1


def test_environment_without_site_packages_reports_unavailable(tmp_path, monkeypatch):
    venv = tmp_path / "broken-venv"
    (venv / "lib").mkdir(parents=True)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")

    def unexpected_scan(*args, **kwargs):
        pytest.fail("must not audit an empty lib directory as an environment")

    monkeypatch.setattr(audit.subprocess, "run", unexpected_scan)
    r = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert r.status == "unavailable"
    assert "no site-packages" in r.detail


def test_missing_pip_audit_binary_reports_unavailable(tmp_path, monkeypatch):
    # No FACTORY_PIP_AUDIT, nothing in the engine interpreter environment,
    # nothing on an isolated PATH, and no historical Hermes venv.
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.delenv("FACTORY_PIP_AUDIT", raising=False)
    empty = tmp_path / "isolated"
    empty.mkdir()
    monkeypatch.setattr(audit.sys, "executable", str(empty / "bin" / "python"))
    monkeypatch.setattr(audit.sysconfig, "get_path", lambda _name: str(empty / "bin"))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(empty / "home"))
    r = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert r.status == "unavailable"
    assert "not found" in r.detail


def test_manifest_scan_flags_vulnerable_pin(tmp_path, monkeypatch):
    # requirements.txt with an old vulnerable pin; _run_pip_audit used.
    repo = _repo_with(tmp_path, {"requirements.txt": "pytest==7.4.4\n"})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(1, stdout=json.dumps(make_pip_env_payload(True))),
    )
    # no venv path => manifest path
    r = audit.run_dependency_audit(str(repo), "python", "")
    assert r.status == "fail"


def test_audit_unavailable_map_to_warning_not_fail(tmp_path, monkeypatch):
    # Non-zero return AND unparseable stdout -> "unavailable" (warning tier).
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(2, stdout="", stderr="registry unreachable"),
    )
    r = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert r.status == "unavailable"


def test_bad_pip_audit_override_reports_unavailable_not_raise(tmp_path, monkeypatch):
    """A FACTORY_PIP_AUDIT pointing at a nonexistent binary must be reported,
    not raised -- this module's contract is 'never raises on non-policy'."""
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", str(tmp_path / "nonexistent-pip-audit"))
    r = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert r.status == "unavailable"
    assert "could not execute" in r.detail


def test_npm_missing_reports_unavailable_not_raise(tmp_path, monkeypatch):
    repo = _repo_with(tmp_path, {"package.json": "{}\n"})

    def _boom(*a, **k):
        raise FileNotFoundError("npm not installed")

    monkeypatch.setattr(audit.subprocess, "run", _boom)
    r = audit.run_dependency_audit(str(repo), "typescript")
    assert r.status == "unavailable"
    assert "could not execute npm" in r.detail


# ---------------------------------------------------------------------------
# audit CLI (python3 -m factory.audit / python3 factory/audit.py)
# ---------------------------------------------------------------------------


def test_audit_cli_module_exits_nonzero_on_vulnerable_venv(
    tmp_path, capsys, monkeypatch
):
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(1, stdout=json.dumps(make_pip_env_payload(True))),
    )
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["result"]["status"] == "fail"
    assert data["passed"] is False


def test_audit_cli_module_exits_zero_and_emits_json_on_clean(
    tmp_path, capsys, monkeypatch
):
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(0, stdout=json.dumps(make_pip_env_payload(False))),
    )
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["result"]["status"] == "pass"
    assert data["passed"] is True


def test_audit_cli_module_exits_zero_on_warning_tier(tmp_path, capsys, monkeypatch):
    # unavailable (e.g. offline index) is a WARNING that must not hard-fail the gate.
    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(2, stdout="", stderr="registry unreachable"),
    )
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["result"]["status"] == "unavailable"
    assert data["passed"] is True


def test_audit_cli_module_entrypoint_via_runpy(tmp_path, monkeypatch, capsys):
    """Exercise the real `if __name__ == '__main__'` entry (python3 -m factory.audit)."""
    import runpy

    venv = _tmp_venv(tmp_path)
    repo = _repo_with(tmp_path, {})
    monkeypatch.setenv("FACTORY_PIP_AUDIT", "/fake/pip-audit")
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: FakeProc(0, stdout=json.dumps(make_pip_env_payload(False))),
    )
    monkeypatch.setattr(
        sys, "argv", ["factory/audit.py", "--repo", str(repo), "--venv", str(venv)]
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("factory.audit", run_name="__main__")
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["passed"] is True
    assert data["result"]["status"] == "pass"
