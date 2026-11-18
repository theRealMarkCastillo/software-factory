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

    def install(report=None, stderr="", returncode=0, raw=None):
        npm.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"payload = {(json.dumps(report) if raw is None else raw)!r}\n"
            "if sys.argv[1] == 'ls':\n"
            "    import json\n"
            "    payload = json.dumps({'dependencies': {name: {'version': '1'} for name in ('a', 'b', 'c')}})\n"
            "sys.stdout.write(payload)\n"
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
        "metadata": {"vulnerabilities": {"total": 0}, "dependencies": {"total": 127}},
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


@pytest.mark.parametrize("raw", ["", "\n \t", "not JSON"])
def test_plain_rejection_preserves_actionable_evidence(
    fake_npm, capsys, monkeypatch, tmp_path, raw
):
    from factory import runner

    diagnostic = "unknown option --omit=dev " + "x" * 1000
    repo = fake_npm(raw=raw, stderr=diagnostic, returncode=2)
    result = audit.run_dependency_audit(str(repo))
    assert result.status == audit.STATUS_INVALID
    assert "npm 7+" in result.detail
    assert "supporting --omit=dev" in result.detail
    assert diagnostic[:400] in result.detail
    assert diagnostic[:401] not in result.detail
    assert len(result.detail) < 500
    assert result.npm_scope == "production"
    assert result.command == [
        "npm",
        "audit",
        "--json",
        "--audit-level",
        "moderate",
        "--omit=dev",
    ]
    assert audit.main(["--repo", str(repo)]) == 1
    cli = json.loads(capsys.readouterr().out)
    assert cli["passed"] is False
    assert cli["result"] == result.to_dict()

    monkeypatch.setattr(runner, "_diff_has_added_python", lambda *args: False)
    run_result = runner.RunResult(card_id="npm-rejection", project_name="npm-rejection")
    assert runner._run_preflight_gates(repo, run_result, {"ref": "main"}) is False
    evidence_path = tmp_path / "runner-evidence.json"
    evidence_path.write_text(json.dumps(run_result.to_dict()))
    stored = json.loads(evidence_path.read_text())["gates"]["audit"]
    assert stored["status"] == audit.STATUS_INVALID
    for key in ("detail", "command", "npm_scope"):
        assert stored[key] == result.to_dict()[key]


@pytest.mark.parametrize("raw", ["\n \t", "not JSON"])
@pytest.mark.parametrize("returncode", [0, 1])
def test_ordinary_malformed_output_still_fails_closed(fake_npm, raw, returncode):
    repo = fake_npm(raw=raw, returncode=returncode)
    result = audit.run_dependency_audit(str(repo))
    assert result.status == audit.STATUS_INVALID
    assert result.detail == "npm audit returned unparseable output"
    assert result.npm_scope == "production"
    assert result.command[-1] == "--omit=dev"


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
