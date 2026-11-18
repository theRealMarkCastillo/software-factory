"""Production counts come from npm's omitted lock tree, not audit metadata."""

import json
import subprocess

import pytest

from factory import audit


@pytest.mark.parametrize(
    "tree,returncode,error",
    [
        ({"dependencies": {"a": {"version": "1"}}}, 1, None),
        ({"dependencies": {"a": {"version": "1"}}}, 0, OSError("gone")),
        ({}, 0, subprocess.TimeoutExpired("npm ls", 180)),
        ([], 0, None),
        ({"dependencies": []}, 0, None),
        ({"dependencies": {"a": {}}}, 0, None),
        ({"dependencies": {"a": {"version": 1}}}, 0, None),
        ({"dependencies": {"a": {"version": ""}}}, 0, None),
        ({"dependencies": {"a": {"version": "1", "problems": ["missing"]}}}, 0, None),
        ({"dependencies": {"a": {"version": "1", "invalid": True}}}, 0, None),
        ({"error": {"code": "ELSPROBLEMS"}}, 0, None),
        ({}, 0, None),
    ],
)
def test_unverified_production_tree_is_invalid(
    tmp_path, monkeypatch, tree, returncode, error
):
    (tmp_path / "package-lock.json").write_text("{}\n")

    def run(command, **kwargs):
        if command[1] == "audit":
            payload = {
                "auditReportVersion": 2,
                "metadata": {
                    "vulnerabilities": {"total": 0},
                    "dependencies": {"total": 127},
                },
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if error:
            raise error
        return subprocess.CompletedProcess(command, returncode, json.dumps(tree), "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    result = audit.run_dependency_audit(str(tmp_path))
    assert result.status == audit.STATUS_INVALID
    assert "production dependency tree" in result.detail


@pytest.mark.parametrize(
    "empty,vulnerable,count,status",
    [
        (False, False, 4, audit.STATUS_PASS),
        (False, True, 4, audit.STATUS_FAIL),
        (True, False, 0, audit.STATUS_INVALID),
        (True, True, 0, audit.STATUS_FAIL),
    ],
)
def test_production_count_deduplicates_lock_tree(
    tmp_path, monkeypatch, empty, vulnerable, count, status
):
    (tmp_path / "package-lock.json").write_text("{}\n")
    report = {
        "auditReportVersion": 2,
        "metadata": {
            "vulnerabilities": {"total": 0},
            "dependencies": {"prod": 6, "dev": 122, "optional": 54, "total": 127},
        },
    }
    tree = {
        "name": "chronograph",
        "dependencies": {
            "frontend": {
                "version": "0.1.0",
                "dependencies": {
                    "react-dom": {
                        "version": "19.2.0",
                        "dependencies": {
                            "react": {"version": "19.2.0"},
                            "scheduler": {"version": "0.27.0"},
                        },
                    },
                    "react": {"version": "19.2.0"},
                },
            }
        },
    }
    if empty:
        tree = {"dependencies": {}}
    report["metadata"]["vulnerabilities"]["total"] = int(vulnerable)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] == str(tmp_path)
        payload = report if command[1] == "audit" else tree
        code = int(vulnerable) if command[1] == "audit" else 0
        return subprocess.CompletedProcess(command, code, json.dumps(payload), "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    result = audit.run_dependency_audit(str(tmp_path)).to_dict()
    assert result["status"] == status
    assert result["audited"] == count
    assert result["findings"] == ([{"total": 1}] if vulnerable else [])
    assert result["lock_total"] == 127
    assert commands[1] == [
        "npm",
        "ls",
        "--omit=dev",
        "--all",
        "--json",
        "--package-lock-only",
    ]
