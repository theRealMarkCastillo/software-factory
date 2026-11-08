"""Installed-path acceptance evidence and opt-in/rollback behaviour (T8/T9).

The installed launcher is executed for read-only verification only. A full
`factory run` acceptance needs Docker and an authorized disposable project, so
this increment records that gap explicitly instead of simulating it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from factory_orch_fixtures import make_evidence, seed

from factory_orchestration.adapters.gates import InstalledLauncherGate
from factory_orchestration.adapters.installed import discover, engine_fingerprint
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import thread_id
from factory_orchestration.runner import cancel, drive, inspect_run, next_action

LAUNCHER = "~/software-factory-ops/bin/factory"


def _release():
    try:
        return discover()
    except (FileNotFoundError, OSError) as exc:
        pytest.skip(f"installed release unavailable: {exc}")


def test_installed_release_identity_is_complete():
    release = _release()
    assert release.launcher.is_file()
    assert release.ready and isinstance(release.ready, dict)
    assert release.fingerprint.startswith("sha256:")
    assert len(release.fingerprint) == len("sha256:") + 64
    assert release.ready_sha256.startswith("sha256:")
    assert release.deployment_sha256.startswith("sha256:")
    assert release.host_policy_sha256.startswith("sha256:")
    assert release.reference.startswith("installed:")
    assert release.launcher_help_ok("verify") is True
    assert (release.engine_dir / "factory" / "verify.py").is_file()
    # The recomputation must match the engine's own canonical algorithm.
    from factory.evidence import engine_identity

    local = engine_identity()
    assert local["name"] == "hermes-software-factory"
    assert (
        engine_fingerprint(
            release.engine_dir, version=release.engine_version, python=release.python
        )
        == release.fingerprint
    )


def test_installed_launcher_reports_a_real_verifier_negative(tmp_path):
    release = _release()
    seeded = seed(tmp_path)
    gate = InstalledLauncherGate(release.launcher, tmp_path / "receipts")
    outcome = gate.verify(
        artifacts=str(tmp_path / "missing-artifacts"),
        repository=str(seeded["repo"]),
        candidate_sha=seeded["candidate"],
        base_sha=seeded["base"],
    )
    assert outcome.certified is False
    assert outcome.reason in {"artifacts_dir_invalid", "manifest_missing"}


def test_installed_launcher_rejects_duplicate_keys_without_running_it(tmp_path):
    seeded = seed(tmp_path)
    artifacts = make_evidence(
        tmp_path / "artifacts", seeded["repo"], seeded["base"], seeded["candidate"]
    )
    manifest = artifacts / "evidence-manifest.json"
    text = manifest.read_text().replace(
        '"mode": "acceptance",',
        '"mode": "acceptance",\n  "mode": "diagnostic",',
        1,
    )
    manifest.write_text(text)
    gate = InstalledLauncherGate(Path("/nonexistent/factory"), tmp_path / "receipts")
    outcome = gate.verify(
        artifacts=str(artifacts),
        repository=str(seeded["repo"]),
        candidate_sha=seeded["candidate"],
        base_sha=seeded["base"],
    )
    assert outcome.certified is False
    assert outcome.reason.startswith("duplicate_json_keys")


def test_admission_disabled_refuses_without_touching_the_workspace(tmp_path):
    seeded = seed(tmp_path)
    (seeded["state_dir"] / "admission.json").write_text(
        json.dumps({"enabled": False, "enrolled_cards": ["t_abcd1234"]})
    )
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=seeded["workspace"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    with pytest.raises(OrchestrationError) as excinfo:
        drive(seeded["deps"], seeded["assignment"], seeded["attempt"])

    assert excinfo.value.reason == "admission_disabled"
    assert not (seeded["worker"].root / "worker-requests").exists()
    assert seeded["deps"].store.run(thread_id(seeded["assignment"])) is None
    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=seeded["workspace"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert before == after


def test_disabling_admission_lets_an_active_graph_drain(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    paused = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert paused.status == "awaiting_worker"

    (seeded["state_dir"] / "admission.json").write_text(
        json.dumps({"enabled": False, "enrolled_cards": []})
    )
    again = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert again.status == "awaiting_worker"
    assert len(seeded["worker"].submitted) == 1


def test_rollback_cancels_and_leaves_state_readable(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    thread = thread_id(seeded["assignment"])

    outcome = cancel(seeded["deps"], thread, by="rollback-test")
    assert outcome["status"] == "cancelled"

    view = inspect_run(seeded["deps"], thread)
    assert view["state"]["status"] == "cancelled"
    assert view["checkpoint"]["present"] is True
    assert view["next_action"]["action"] == "none"
    # New non-enrolled cards route through the ordinary refusal, not the graph.
    (seeded["state_dir"] / "admission.json").write_text(
        json.dumps({"enabled": True, "enrolled_cards": []})
    )
    fresh = seed(tmp_path / "fresh")
    (fresh["state_dir"] / "admission.json").write_text(
        json.dumps({"enabled": True, "enrolled_cards": []})
    )
    with pytest.raises(OrchestrationError) as excinfo:
        drive(fresh["deps"], fresh["assignment"], fresh["attempt"])
    assert excinfo.value.reason == "invalid_assignment"
    assert not (fresh["worker"].root / "worker-requests").exists()


def test_next_action_is_a_bounded_proposal(tmp_path):
    seeded = seed(tmp_path)
    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    action = next_action(result.state, {})
    assert action["action"] == "return_to_lifecycle_owner"
    assert "promotion" in action["detail"]
