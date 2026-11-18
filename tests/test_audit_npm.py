"""npm report parsing regressions; subprocess is the external tool boundary."""

import json
import subprocess

import pytest

from factory import audit


@pytest.fixture
def npm_report():
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "picomatch": {
                "name": "picomatch",
                "severity": "high",
                "isDirect": False,
                "via": [
                    {
                        "source": 1247710,
                        "name": "picomatch",
                        "dependency": "picomatch",
                        "title": "picomatch vulnerable to ReDoS through single-branch repeated extglobs",
                        "url": "https://github.com/advisories/GHSA-fc5q-39xc-vjpf",
                        "severity": "high",
                        "range": "<=4.0.7",
                    }
                ],
                "effects": ["fdir", "tinyglobby", "vite"],
                "range": "*",
                "nodes": ["node_modules/picomatch"],
                "fixAvailable": {
                    "name": "vite",
                    "version": "6.2.7",
                    "isSemVerMajor": True,
                },
            }
        },
        "metadata": {
            "vulnerabilities": {
                "info": 0,
                "low": 0,
                "moderate": 0,
                "high": 4,
                "critical": 0,
                "total": 4,
            },
            "dependencies": {
                "prod": 6,
                "dev": 122,
                "optional": 54,
                "peer": 0,
                "peerOptional": 0,
                "total": 127,
            },
        },
    }


def run_report(tmp_path, monkeypatch, report):
    (tmp_path / "package-lock.json").write_text("{}\n")
    stdout = json.dumps(report)
    returncode = int(report["metadata"]["vulnerabilities"]["total"] > 0)

    def run(command, **kwargs):
        if command[1] == "audit":
            return subprocess.CompletedProcess(command, returncode, stdout, "")
        count = report["metadata"]["dependencies"].get("total", 0)
        tree = {
            "dependencies": {f"pkg-{i}": {"version": "1"} for i in range(min(count, 6))}
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(tree), "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    return audit.run_dependency_audit(str(tmp_path), "typescript")


@pytest.mark.parametrize("dependencies", [127, {"prod": 6, "dev": 122, "total": 127}])
@pytest.mark.parametrize("vulnerable", [False, True])
def test_npm_dependency_count_preserves_verdict(
    tmp_path, monkeypatch, npm_report, dependencies, vulnerable
):
    npm_report["metadata"]["dependencies"] = dependencies
    if not vulnerable:
        npm_report["vulnerabilities"] = {}
        npm_report["metadata"]["vulnerabilities"] = {"total": 0}
    result = run_report(tmp_path, monkeypatch, npm_report)
    assert result.status == (audit.STATUS_FAIL if vulnerable else audit.STATUS_PASS)
    assert result.audited == (6 if isinstance(dependencies, dict) else 127)
    assert result.findings == (
        [npm_report["metadata"]["vulnerabilities"]] if vulnerable else []
    )
    assert result.report_sha256


@pytest.mark.parametrize(
    "dependencies",
    [
        {},
        {"prod": 6},
        {"total": None},
        {"total": "127"},
        {"total": 1.5},
        {"total": True},
        {"total": False},
        {"total": -1},
        True,
        False,
        -1,
        None,
        "127",
        1.5,
        [],
    ],
)
@pytest.mark.parametrize("vulnerable", [False, True])
def test_npm_invalid_dependency_count_fails_closed(
    tmp_path, monkeypatch, npm_report, dependencies, vulnerable
):
    npm_report["metadata"]["dependencies"] = dependencies
    if not vulnerable:
        npm_report["metadata"]["vulnerabilities"] = {"total": 0}
    result = run_report(tmp_path, monkeypatch, npm_report)
    assert result.status == audit.STATUS_INVALID
    assert "npm audit dependency count" in result.detail
    assert "non-negative integer" in result.detail
    assert "unparseable" not in result.detail


@pytest.mark.parametrize("dependencies", [0, {"total": 0}])
def test_npm_zero_audited_dependencies_is_invalid(
    tmp_path, monkeypatch, npm_report, dependencies
):
    npm_report["metadata"]["dependencies"] = dependencies
    npm_report["metadata"]["vulnerabilities"] = {"total": 0}
    result = run_report(tmp_path, monkeypatch, npm_report)
    assert result.status == audit.STATUS_INVALID
    assert result.detail == "npm audit audited zero dependencies"


@pytest.mark.parametrize("dependencies", [0, {"total": 0}])
def test_npm_zero_count_does_not_hide_vulnerabilities(
    tmp_path, monkeypatch, npm_report, dependencies
):
    npm_report["metadata"]["dependencies"] = dependencies
    result = run_report(tmp_path, monkeypatch, npm_report)
    assert result.status == audit.STATUS_FAIL
    assert result.findings == [npm_report["metadata"]["vulnerabilities"]]
