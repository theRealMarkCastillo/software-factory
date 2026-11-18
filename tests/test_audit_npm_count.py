"""Production counts come from npm's omitted lock tree, not audit metadata."""

import json
import subprocess

from factory import audit


def test_production_count_deduplicates_lock_tree(tmp_path, monkeypatch):
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
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] == str(tmp_path)
        payload = report if command[1] == "audit" else tree
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    result = audit.run_dependency_audit(str(tmp_path)).to_dict()
    assert result["status"] == audit.STATUS_PASS
    assert result["audited"] == 4
    assert result["lock_total"] == 127
    assert commands[1] == [
        "npm",
        "ls",
        "--omit=dev",
        "--all",
        "--json",
        "--package-lock-only",
    ]
