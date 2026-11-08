"""Real verifier negatives through the gate adapter (T5).

Each case builds a schema-v2 evidence directory with the engine's own producer,
mutates exactly one thing, and asserts the *engine's* reason code. A diagnostic
or tampered manifest can never qualify, and a green worker flag is not a gate.
"""

from __future__ import annotations

import hashlib

from factory_orch_fixtures import make_evidence, seed

from factory.evidence import MANIFEST_FILENAME, SIDECAR_FILENAME
from factory_orchestration.adapters.fake import FakeGate
from factory_orchestration.graph import thread_id
from factory_orchestration.runner import drive, inspect_run


def _verify(seeded, artifacts, candidate=None):
    gate = FakeGate(root=seeded["worker"].root, evidence_dir=artifacts)
    return gate.verify(
        artifacts=str(artifacts),
        repository=str(seeded["repo"]),
        candidate_sha=candidate or seeded["candidate"],
        base_sha=seeded["base"],
    )


def _evidence(seeded, tmp_path, **overrides):
    return make_evidence(
        tmp_path / "tampered",
        seeded["repo"],
        seeded["base"],
        seeded["candidate"],
        **overrides,
    )


def test_diagnostic_evidence_cannot_qualify(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(seeded, tmp_path, mode="diagnostic", gates_run=False)
    outcome = _verify(seeded, artifacts)
    assert outcome.certified is False
    assert outcome.reason == "mode_not_acceptance"


def test_incomplete_teardown_cannot_qualify(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(
        seeded,
        tmp_path,
        runtime={
            "teardown": {"attempted": True, "ok": False, "detail": "leaked volume"},
            "safety": {
                "postflight": "clean",
                "protected_changes": [],
                "unexpected_changes": [],
                "collateral_damage": False,
            },
        },
    )
    outcome = _verify(seeded, artifacts)
    assert outcome.certified is False
    assert outcome.reason == "teardown_failed"


def test_safety_drift_cannot_qualify(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(
        seeded,
        tmp_path,
        runtime={
            "teardown": {"attempted": True, "ok": True, "detail": ""},
            "safety": {
                "postflight": "clean",
                "protected_changes": ["eidolon-v2"],
                "unexpected_changes": [],
                "collateral_damage": False,
            },
        },
    )
    outcome = _verify(seeded, artifacts)
    assert outcome.certified is False
    assert outcome.reason == "safety_incident"


def test_wrong_candidate_cannot_qualify(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(seeded, tmp_path)
    outcome = _verify(seeded, artifacts, candidate="f" * 40)
    assert outcome.certified is False
    assert outcome.reason == "source_sha_mismatch"


def test_tampered_log_cannot_qualify(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(seeded, tmp_path)
    (artifacts / "compose.log").write_text("compose.log: altered\n")
    outcome = _verify(seeded, artifacts)
    assert outcome.certified is False
    assert outcome.reason.startswith("artifact_")
    assert outcome.reason.endswith(":compose.log")


def test_missing_log_cannot_qualify(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(seeded, tmp_path)
    (artifacts / "compose.log").unlink()
    outcome = _verify(seeded, artifacts)
    assert outcome.certified is False
    assert outcome.reason.startswith("artifact_")


def test_duplicate_json_keys_are_rejected_before_interpretation(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(seeded, tmp_path)
    manifest = artifacts / MANIFEST_FILENAME
    text = manifest.read_text()
    duplicated = text.replace(
        '"mode": "acceptance",',
        '"mode": "diagnostic",\n  "mode": "acceptance",',
        1,
    )
    assert duplicated != text
    manifest.write_text(duplicated)
    digest = hashlib.sha256(duplicated.encode()).hexdigest()
    (artifacts / SIDECAR_FILENAME).write_text(f"{digest}  {MANIFEST_FILENAME}\n")

    outcome = _verify(seeded, artifacts)

    assert outcome.certified is False
    assert outcome.reason.startswith("duplicate_json_keys")


def test_tampered_graph_holds_and_never_proposes_completion(tmp_path):
    seeded = seed(tmp_path)
    artifacts = _evidence(seeded, tmp_path, mode="diagnostic", gates_run=False)
    seeded["gate"].evidence_dir = artifacts

    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])

    assert result.status == "held"
    assert result.state["last_reason_code"] == "evidence_rejected"
    assert result.state["last_reason_detail"].startswith("mode_not_acceptance")
    assert result.state.get("approval_reference", "") == ""
    view = inspect_run(seeded["deps"], thread_id(seeded["assignment"]))
    assert view["next_action"]["action"] == "operator_decision"
