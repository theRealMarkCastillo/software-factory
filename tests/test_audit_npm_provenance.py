"""Additive npm provenance without changing existing merge verdicts or fields."""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from factory import audit

BASE = "fab7148997300ad3e1e32a374f9fb144f006b201"


@pytest.fixture
def baseline(monkeypatch):
    source = subprocess.run(
        ["git", "show", f"{BASE}:factory/audit.py"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    module = types.ModuleType("baseline_npm_audit")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "baseline-audit.py", "exec"), module.__dict__)
    return module


def scan(module, language, status="pass"):
    is_npm = language == "typescript"
    return module.AuditResult(
        status,
        language,
        tool="npm audit" if is_npm else "pip-audit",
        source="package-lock" if is_npm else "requirements.txt",
        command=["npm", "audit"] if is_npm else ["pip-audit", "-r", "requirements.txt"],
        audited=3 if is_npm else 2,
        detail="fixture detail",
        skipped=[{"name": "local", "skip_reason": "local project"}],
        findings=[{"name": "bad"}] if status == "fail" else [],
    )


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_single_scan_serialization_byte_identical_to_fab7148(baseline, language):
    # Fixed scan fixtures isolate the additive merge/serialization change from
    # the intentionally different npm invocation and npm_scope contract.
    old = baseline._merge_scans([scan(baseline, language)], language).to_dict()
    new = audit._merge_scans([scan(audit, language)], language).to_dict()
    assert json.dumps(new).encode() == json.dumps(old).encode()
    assert "scans" not in new


@pytest.mark.parametrize(
    "status", ["pass", "fail", "invalid", "unsupported", "no_manifest"]
)
@pytest.mark.parametrize("npm_first", [False, True])
def test_mixed_scans_preserve_provenance_on_every_branch(baseline, status, npm_first):
    results = [scan(audit, "python"), scan(audit, "typescript", status)]
    expected_scans = []
    for result in results:
        entry = {
            key: getattr(result, key)
            for key in ("language", "tool", "source", "status", "audited", "command")
        }
        expected_scans.append(entry)
    old_results = [scan(baseline, "python"), scan(baseline, "typescript", status)]
    if npm_first:
        results.reverse()
        old_results.reverse()
        expected_scans.reverse()
    old = baseline._merge_scans(old_results, "python").to_dict()
    new = audit._merge_scans(results, "python").to_dict()
    assert new.pop("scans") == expected_scans
    assert json.dumps(new).encode() == json.dumps(old).encode()
