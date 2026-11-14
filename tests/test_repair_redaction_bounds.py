"""Additional bounds checks for redaction's local I/O boundary."""

import json
import subprocess
from pathlib import Path

import pytest

from factory_orchestration.adapters.gates import InstalledLauncherGate
from factory_orchestration.graph import _gate_log_tail


def test_large_log_retains_useful_redacted_tail(tmp_path, monkeypatch):
    secret = "synthetic-" + "tail-credential"
    monkeypatch.setenv("FIXTURE_AUTH", secret)
    log = tmp_path / "acceptance.log"
    log.write_text(
        "x" * (3 * 1024 * 1024)
        + "\nFAILED tests/test_api.py::test_y[a b] - "
        + secret
        + "\n"
    )
    tail = _gate_log_tail(str(tmp_path), str(log))
    assert "FAILED tests/test_api.py::test_y[a b]" in tail
    assert "[REDACTED:FIXTURE_AUTH]" in tail
    assert secret not in tail
    assert tail.startswith("[truncated]")
    # The truncation marker is framing, not part of the first pytest node id.
    import hashlib

    from factory_orchestration.graph import _failure_signature

    expected = hashlib.sha256(b" |  | tests/test_api.py::test_y[a b]").hexdigest()
    assert _failure_signature({"gate_log_excerpt": tail}) == expected


@pytest.mark.parametrize("exit_code", [1, 2])
def test_installed_verifier_redacts_before_its_existing_detail_cap(
    tmp_path, monkeypatch, exit_code
):
    secret = "fixture-" + "s" * 6000
    monkeypatch.setenv("FIXTURE_AUTH", secret)
    monkeypatch.setattr(
        "factory_orchestration.evidence_guard.manifest_guard", lambda root: (True, "ok")
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            [], exit_code, json.dumps({"detail": secret}), secret
        ),
    )
    result = InstalledLauncherGate(Path("/unused"), tmp_path).verify(
        artifacts=str(tmp_path),
        repository=str(tmp_path),
        candidate_sha="a" * 40,
        base_sha="b" * 40,
    )
    assert result.detail == "[REDACTED:FIXTURE_AUTH]"
