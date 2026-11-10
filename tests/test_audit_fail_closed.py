"""Fail-closed regression tests for ``factory.audit``.

These tests encode the contract that the installed dependency audit gate must
never report ``pass`` for a report it could not read and verify:

* the installed-environment invocation must be one pip-audit actually accepts
  (``--format json --path <site-packages>``; ``-l`` cannot be combined with
  ``--path``);
* only a schema-valid report with at least one *audited* dependency and no
  unexpected skips may pass;
* exit-code/report contradictions (rc0 + vulnerabilities, rc1 + no findings),
  empty or malformed stdout, usage errors, timeouts, missing executables and
  zero-audited reports cannot pass;
* lock files are converted to a deterministic pinned requirement set bound to
  the lock file and never handed to pip-audit as an unsupported positional
  manifest.

Every test here is synthetic (``subprocess.run`` is mocked) except the real
tool suite in ``test_audit_real_tool.py``.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import audit


class FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def clean_dependency(name="requests", version="2.32.0"):
    return {"name": name, "version": version, "vulns": []}


def vulnerable_dependency(name="requests", version="2.6.0", vuln_id="PYSEC-2018-28"):
    return {
        "name": name,
        "version": version,
        "vulns": [
            {
                "id": vuln_id,
                "fix_versions": ["2.20.0"],
                "aliases": ["CVE-2018-18074"],
            }
        ],
    }


def report(*dependencies):
    return {"dependencies": list(dependencies)}


def skipped_dependency(name, reason="Dependency not found on PyPI"):
    return {"name": name, "version": "0.1.0", "skip_reason": reason}


def fake_venv(tmp_path, python="python3.12"):
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib" / python / "site-packages").mkdir(parents=True)
    return venv


def write_repo(tmp_path, files, name="repo"):
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        target = repo / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return repo


def fake_tool(tmp_path):
    """An executable file standing in for pip-audit (subprocess is mocked)."""
    bin_dir = tmp_path / "engine" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / "pip-audit"
    tool.write_text("#!/bin/sh\nexit 0\n")
    return tool


def use_tool(monkeypatch, tool):
    monkeypatch.setenv("FACTORY_PIP_AUDIT", str(tool))


def mock_run(monkeypatch, proc):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if callable(proc):
            return proc(cmd)
        if isinstance(proc, list):
            return proc[len(calls) - 1]
        return proc

    monkeypatch.setattr(audit.subprocess, "run", run)
    return calls


# ---------------------------------------------------------------------------
# 1. installed-environment invocation must be supported by pip-audit
# ---------------------------------------------------------------------------


def test_environment_scan_uses_supported_argv(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    site_packages = next((venv / "lib").glob("python*/site-packages"))
    repo = write_repo(tmp_path, {}, name="repo")
    tool = fake_tool(tmp_path)
    use_tool(monkeypatch, tool)
    calls = mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))

    assert result.status == "pass", result.detail
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == str(tool)
    assert "-l" not in argv and "--local" not in argv
    assert argv[argv.index("--format") + 1] == "json"
    assert argv[argv.index("--path") + 1] == str(site_packages)


def test_environment_usage_error_with_empty_stdout_never_passes(tmp_path, monkeypatch):
    """The exact installed defect: rc1 + empty stdout was parsed as clean."""
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(
            1,
            stdout="",
            stderr="ERROR: Cannot combine '--path' with '--user' or '--local'",
        ),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status != "pass"
    assert "empty" in result.detail.lower() or "unusable" in result.detail.lower()
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) != 0


@pytest.mark.parametrize("returncode", [2, 3, 4, 127])
def test_unexpected_exit_never_passes(tmp_path, monkeypatch, returncode):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(monkeypatch, FakeProc(returncode, stdout="", stderr="tool failure"))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "exited" in result.detail


# ---------------------------------------------------------------------------
# 2. exit-code / report consistency
# ---------------------------------------------------------------------------


def test_rc0_with_vulnerabilities_is_never_a_pass(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(0, json.dumps(report(clean_dependency(), vulnerable_dependency()))),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status != "pass"
    assert result.findings and result.findings[0]["name"] == "requests"
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) != 0


def test_rc1_without_findings_is_invalid(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(monkeypatch, FakeProc(1, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "without" in result.detail
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) != 0


def test_rc1_with_findings_is_fail_and_preserves_details(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(1, json.dumps(report(vulnerable_dependency()))),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "fail"
    assert result.findings[0]["vulns"][0]["id"] == "PYSEC-2018-28"
    assert "requests" in result.detail


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", "[]", '{"dependencies": {}}', '{"dependencies": [1]}'],
)
def test_malformed_reports_are_invalid(tmp_path, monkeypatch, stdout):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(monkeypatch, FakeProc(1, stdout=stdout, stderr="noise"))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid", result.detail


def test_dependency_entry_without_vulns_or_skip_is_invalid(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(0, json.dumps({"dependencies": [{"name": "requests"}]})),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"


# ---------------------------------------------------------------------------
# 3. audited vs skipped accounting
# ---------------------------------------------------------------------------


def test_zero_audited_dependencies_cannot_pass(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(monkeypatch, FakeProc(0, json.dumps({"dependencies": []})))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "audited" in result.detail


def test_all_skipped_dependencies_cannot_pass(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(
        tmp_path,
        {"pyproject.toml": '[project]\nname = "agent-trace-auditor"\n'},
        name="repo",
    )
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(0, json.dumps(report(skipped_dependency("agent-trace-auditor")))),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "audited" in result.detail


def test_unexpected_skipped_third_party_blocks_and_is_reported(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(
        tmp_path,
        {"pyproject.toml": '[project]\nname = "agent-trace-auditor"\n'},
        name="repo",
    )
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(
            0,
            json.dumps(
                report(
                    clean_dependency(),
                    skipped_dependency("agent-trace-auditor"),
                    skipped_dependency("mystery-package", "not found on PyPI"),
                )
            ),
        ),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "mystery-package" in result.detail


def test_local_project_skip_is_reported_and_can_pass(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(
        tmp_path,
        {"pyproject.toml": '[project]\nname = "Agent_Trace.Auditor"\n'},
        name="repo",
    )
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(
            0,
            json.dumps(
                report(clean_dependency(), skipped_dependency("agent-trace-auditor"))
            ),
        ),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "pass", result.detail
    assert result.audited == 1
    assert [item["name"] for item in result.skipped] == ["agent-trace-auditor"]
    assert "agent-trace-auditor" in result.detail


# ---------------------------------------------------------------------------
# 4. tool availability failures
# ---------------------------------------------------------------------------


def test_missing_executable_never_passes(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    monkeypatch.setenv("FACTORY_PIP_AUDIT", str(tmp_path / "nope" / "pip-audit"))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "could not execute" in result.detail
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) != 0


def test_timeout_never_passes(tmp_path, monkeypatch):
    import subprocess as real_subprocess

    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))

    def boom(cmd, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(audit.subprocess, "run", boom)
    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "timed out" in result.detail


# ---------------------------------------------------------------------------
# 5. lock-file handling: deterministic conversion, never positional
# ---------------------------------------------------------------------------

UV_LOCK = """\
version = 1
revision = 1
requires-python = ">=3.11"

[[package]]
name = "my-project"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "requests" },
]

[[package]]
name = "requests"
version = "2.6.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "urllib3"
version = "1.26.0"
source = { registry = "https://pypi.org/simple" }
"""


def lock_repo(tmp_path, lock=UV_LOCK):
    return write_repo(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "my-project"\nversion = "0.1.0"\n',
            "uv.lock": lock,
        },
        name="repo",
    )


def test_uv_lock_is_converted_to_pinned_requirements(tmp_path, monkeypatch):
    repo = lock_repo(tmp_path)
    use_tool(monkeypatch, fake_tool(tmp_path))
    calls = mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", "")

    assert result.status == "pass", result.detail
    assert len(calls) == 1
    argv = calls[0]
    assert "uv.lock" not in argv, "uv.lock must never be a positional manifest"
    assert "my-project" not in " ".join(argv)
    assert argv[argv.index("-r") + 1].endswith(".txt")
    assert "--no-deps" in argv
    # A pinned file must be read directly (no throwaway venv/ensurepip step,
    # which aborts on some uv-managed interpreters).
    assert "--disable-pip" in argv
    assert result.lock_sha256 and len(result.lock_sha256) == 64
    assert result.skipped and result.skipped[0]["name"] == "my-project"


def test_lock_pin_file_contains_every_registry_package(tmp_path, monkeypatch):
    repo = lock_repo(tmp_path)
    use_tool(monkeypatch, fake_tool(tmp_path))
    seen = {}

    def fake(cmd):
        path = Path(cmd[cmd.index("-r") + 1])
        seen["text"] = path.read_text()
        return FakeProc(0, json.dumps(report(clean_dependency())))

    mock_run(monkeypatch, fake)
    audit.run_dependency_audit(str(repo), "python", "")
    assert "requests==2.6.0" in seen["text"]
    assert "urllib3==1.26.0" in seen["text"]


def test_uv_lock_vulnerable_pin_fails(tmp_path, monkeypatch):
    repo = lock_repo(tmp_path)
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(
        monkeypatch,
        FakeProc(1, json.dumps(report(vulnerable_dependency()))),
    )

    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "fail"
    assert result.findings[0]["name"] == "requests"


def test_third_party_non_registry_lock_entry_fails_closed(tmp_path, monkeypatch):
    lock = UV_LOCK.rsplit('source = { registry = "https://pypi.org/simple" }', 1)[0] + (
        'source = { git = "https://example.invalid/urllib3" }\n'
    )
    repo = lock_repo(tmp_path, lock=lock)
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status in ("invalid", "unsupported")
    assert "urllib3" in result.detail


def test_pyproject_without_lock_fails_closed_with_actionable_reason(
    tmp_path, monkeypatch
):
    repo = write_repo(
        tmp_path,
        {"pyproject.toml": '[project]\nname = "solo"\ndependencies = ["requests"]\n'},
        name="repo",
    )
    use_tool(monkeypatch, fake_tool(tmp_path))
    calls = mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "unsupported"
    assert "lock" in result.detail.lower()
    assert calls == []


def test_unpinned_requirements_fail_closed(tmp_path, monkeypatch):
    repo = write_repo(tmp_path, {"requirements.txt": "requests\n"}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    calls = mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "unsupported"
    assert "requests" in result.detail
    assert calls == []


def test_pinned_requirements_are_audited_via_file(tmp_path, monkeypatch):
    repo = write_repo(tmp_path, {"requirements.txt": "requests==2.6.0\n"}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    calls = mock_run(
        monkeypatch,
        FakeProc(1, json.dumps(report(vulnerable_dependency()))),
    )

    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "fail"
    argv = calls[0]
    assert "requirements.txt" not in argv
    assert argv[argv.index("-r") + 1].endswith(".txt")


def test_hashed_pins_are_deterministic_and_supported(tmp_path, monkeypatch):
    repo = write_repo(
        tmp_path,
        {
            "requirements.txt": (
                "requests==2.6.0 --hash=sha256:0123456789abcdef\n# comment\n"
            )
        },
        name="repo",
    )
    use_tool(monkeypatch, fake_tool(tmp_path))
    seen = {}

    def fake(cmd):
        seen["text"] = Path(cmd[cmd.index("-r") + 1]).read_text()
        return FakeProc(1, json.dumps(report(vulnerable_dependency())))

    mock_run(monkeypatch, fake)
    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "fail", result.detail
    assert seen["text"].strip() == "requests==2.6.0"


# ---------------------------------------------------------------------------
# 6. environment <-> lock binding
# ---------------------------------------------------------------------------


def write_dist_info(site_packages, name, version):
    dist = site_packages / f"{name.replace('-', '_')}-{version}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    )


def test_environment_drift_from_lock_blocks_before_audit(tmp_path, monkeypatch):
    repo = lock_repo(tmp_path)
    venv = fake_venv(tmp_path)
    site_packages = next((venv / "lib").glob("python*/site-packages"))
    write_dist_info(site_packages, "requests", "2.31.0")
    write_dist_info(site_packages, "my-project", "0.1.0")
    use_tool(monkeypatch, fake_tool(tmp_path))
    calls = mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "invalid"
    assert "uv.lock" in result.detail
    assert "requests" in result.detail
    assert calls == []


def test_environment_matching_lock_is_audited(tmp_path, monkeypatch):
    repo = lock_repo(tmp_path)
    venv = fake_venv(tmp_path)
    site_packages = next((venv / "lib").glob("python*/site-packages"))
    write_dist_info(site_packages, "requests", "2.6.0")
    write_dist_info(site_packages, "urllib3", "1.26.0")
    write_dist_info(site_packages, "my-project", "0.1.0")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(monkeypatch, FakeProc(1, json.dumps(report(vulnerable_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    assert result.status == "fail"
    assert result.lock_sha256


# ---------------------------------------------------------------------------
# 7. CLI contract and provenance fields
# ---------------------------------------------------------------------------


def test_cli_exit_is_zero_only_for_pass_or_no_manifest(tmp_path, monkeypatch, capsys):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    mock_run(monkeypatch, FakeProc(1, "", ""))

    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["result"]["status"] == "invalid"

    mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))
    assert audit.main(["--repo", str(repo), "--venv", str(venv)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True


def test_result_records_command_counts_and_tool_version(tmp_path, monkeypatch):
    venv = fake_venv(tmp_path)
    repo = write_repo(tmp_path, {}, name="repo")
    tool = fake_tool(tmp_path)
    # A dist-info beside the tool records its version without executing it.
    (tmp_path / "engine" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    dist = (
        tmp_path
        / "engine"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "pip_audit-2.10.1.dist-info"
    )
    dist.mkdir()
    (dist / "METADATA").write_text("Name: pip-audit\nVersion: 2.10.1\n")
    use_tool(monkeypatch, tool)
    mock_run(
        monkeypatch,
        FakeProc(
            0, json.dumps(report(clean_dependency(), skipped_dependency("localpkg")))
        ),
    )

    result = audit.run_dependency_audit(str(repo), "python", str(venv))
    # localpkg is not the repo's declared project -> blocked, but facts recorded
    assert result.audited == 1
    assert result.command and result.command[0] == str(tool)
    assert result.tool_version == "2.10.1"
    assert result.source == "environment"
    assert result.report_sha256
    payload = result.to_dict()
    assert payload["vulnerability_service"] == "pypi"
    assert payload["audited"] == 1
    assert payload["skipped"]


def test_no_manifest_repo_is_inapplicable_not_a_scan(tmp_path, monkeypatch):
    repo = write_repo(tmp_path, {"README.md": "docs\n"}, name="repo")
    use_tool(monkeypatch, fake_tool(tmp_path))
    calls = mock_run(monkeypatch, FakeProc(0, json.dumps(report(clean_dependency()))))

    result = audit.run_dependency_audit(str(repo), "python", "")
    assert result.status == "no_manifest"
    assert calls == []
    assert audit.main(["--repo", str(repo)]) == 0
