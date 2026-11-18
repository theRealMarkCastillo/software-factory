"""Production-only npm audit scope at the public gate and CLI boundaries."""

import json
import sys

import pytest

from factory import audit


@pytest.fixture
def fake_npm(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}\n")
    monkeypatch.setenv("PATH", str(bin_dir))

    def install(report, stderr="", returncode=0):
        npm.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"print({json.dumps(report)!r})\n"
            f"print({stderr!r}, file=sys.stderr)\n"
            f"sys.exit({returncode})\n"
        )
        npm.chmod(0o755)
        return repo

    return install


def clean_report():
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {},
        "metadata": {"vulnerabilities": {"total": 0}, "dependencies": {"total": 3}},
    }


def test_npm_command_omits_dev_dependencies(fake_npm):
    repo = fake_npm(clean_report())
    result = audit.run_dependency_audit(str(repo))
    assert result.status == audit.STATUS_PASS
    assert result.command == [
        "npm",
        "audit",
        "--json",
        "--audit-level",
        "moderate",
        "--omit=dev",
    ]


def test_npm_scope_is_in_result_and_cli_evidence(fake_npm, capsys):
    repo = fake_npm(clean_report())
    result = audit.run_dependency_audit(str(repo))
    assert result.npm_scope == "production"
    assert result.to_dict()["npm_scope"] == "production"
    assert audit.main(["--repo", str(repo)]) == 0
    evidence = json.loads(capsys.readouterr().out)["result"]
    assert evidence["npm_scope"] == "production"
    assert evidence["command"][-1] == "--omit=dev"


@pytest.mark.parametrize("reject_option", [False, True])
def test_npm_without_omit_support_fails_closed(fake_npm, capsys, reject_option):
    # npm 6 may silently ignore unknown options and emit a clean v1 report.
    report = clean_report()
    del report["auditReportVersion"]
    repo = fake_npm(
        report,
        stderr="unknown option --omit=dev" if reject_option else "",
        returncode=2 if reject_option else 0,
    )
    result = audit.run_dependency_audit(str(repo))
    assert result.status == audit.STATUS_INVALID
    assert "npm 7+" in result.detail
    assert "--omit=dev" in result.detail
    assert result.to_dict()["npm_scope"] == "production"
    assert audit.main(["--repo", str(repo)]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_mixed_project_preserves_npm_scope_in_evidence(fake_npm, monkeypatch):
    repo = fake_npm(clean_report())
    (repo / "requirements.txt").write_text("example==1.0\n")
    # Isolate the unrelated Python auditor; execute the real fake npm process.
    monkeypatch.setattr(
        audit,
        "_run_pip_audit",
        lambda *args: audit.AuditResult(
            audit.STATUS_PASS, "python", tool="pip-audit", audited=1
        ),
    )
    result = audit.run_dependency_audit(str(repo)).to_dict()
    assert result["status"] == audit.STATUS_PASS
    npm_scan = result["scans"][1]
    assert npm_scan["npm_scope"] == "production"
    assert npm_scan["command"][-1] == "--omit=dev"
    assert npm_scan["audited"] == 3
    assert result["scans"][0]["audited"] == 1
    assert result["audited"] == 4
