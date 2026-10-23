"""Additional dependency audit CLI regression tests.

Covers the remaining uncovered branches: gate_summary's non-empty path, the
vulnerable-repo exit path in main(), and the __main__ guard itself.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import audit
from factory.audit import AuditResult, gate_summary


def test_gate_summary_with_results():
    """The non-empty summary path (line 279) formats language:status pairs."""
    s = gate_summary(
        [AuditResult("pass", "python"), AuditResult("unavailable", "typescript")]
    )
    assert s == "dependency audit [python:pass, typescript:unavailable]"


def test_main_clean_repo_outputs_json_and_exits_zero(tmp_path, monkeypatch):
    """A venv with no vulnerable packages: main() -> 0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("requests==2.32.0\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "pip").write_text("x")  # existence marker
    (venv / "lib").mkdir(parents=True)

    def fake_pip_audit(cmd, **kwargs):
        class P:
            returncode = 0
            stdout = json.dumps({"dependencies": []})
            stderr = ""

        return P()

    monkeypatch.setattr(
        audit.shutil,
        "which",
        lambda name: "/usr/bin/pip-audit" if name == "pip-audit" else None,
    )
    monkeypatch.setattr(audit.subprocess, "run", fake_pip_audit)

    rc = audit.main(["--repo", str(repo), "--venv", str(venv)])
    assert rc == 0


def test_main_vulnerable_repo_exits_nonzero(tmp_path, monkeypatch):
    """A venv whose installed packages include a vulnerable one: main() -> nonzero."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("requests==2.6.0\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "pip").write_text("x")  # existence marker
    (venv / "lib").mkdir(parents=True)

    def fake_pip_audit(cmd, **kwargs):
        class P:
            returncode = 1  # pip-audit exits 1 when vulnerabilities are found
            stdout = json.dumps(
                {
                    "dependencies": [
                        {
                            "name": "requests",
                            "version": "2.6.0",
                            "vulns": [
                                {"id": "PYSEC-2015-1", "fix_versions": ["2.6.1"]}
                            ],
                        }
                    ]
                }
            )
            stderr = ""

        return P()

    monkeypatch.setattr(
        audit.shutil,
        "which",
        lambda name: "/usr/bin/pip-audit" if name == "pip-audit" else None,
    )
    monkeypatch.setattr(audit.subprocess, "run", fake_pip_audit)

    rc = audit.main(["--repo", str(repo), "--venv", str(venv)])
    assert rc != 0


def test_dunder_main_guard_exists():
    """The module executes main() under __main__ (the old silent no-op is gone)."""
    src = Path(audit.__file__).read_text()
    assert 'if __name__ == "__main__":' in src
    assert "raise SystemExit(main())" in src
