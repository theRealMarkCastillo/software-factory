"""R2-A reviewer sandbox: configuration, fail-closed gates and transcript audit.

The live attestation (real Docker container, real Hermes tool stack) only runs
with ``FACTORY_LIVE_SANDBOX=1``; it is executed explicitly for acceptance
evidence. Everything else here is deterministic and offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from factory_orch_fixtures import CARD

from factory_orchestration.adapters.hermes import (
    EXPECTED_REVIEW_TOOLS,
    HermesReviewer,
    HermesWorker,
    NoReadonlyEnforcement,
    audit_review_transcript,
    child_environment,
)
from factory_orchestration.adapters.reviewer_sandbox import (
    CANDIDATE_MOUNT,
    EVIDENCE_MOUNT,
    PROBE_MOUNT,
    SandboxedReadonlyAttestor,
    SandboxSpec,
    _assert_owned_root,
    prepare_sandbox,
    sandbox_config_yaml,
)
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.state import Attempt

IMAGE = "python@sha256:" + "a" * 64


def _spec(tmp_path: Path) -> SandboxSpec:
    candidate = tmp_path / "candidate"
    evidence = tmp_path / "evidence"
    probe = tmp_path / "probe"
    for directory in (candidate, evidence, probe):
        directory.mkdir(parents=True, exist_ok=True)
    (candidate / "README.md").write_text("# candidate\n")
    (evidence / "evidence-manifest.json").write_text("{}")
    (probe / "probe.txt").write_text("factory review probe\n")
    return SandboxSpec(
        name="factory-reviewer",
        root=tmp_path / "sandbox",
        candidate=candidate,
        evidence=evidence,
        image=IMAGE,
        probe=probe,
    )


def test_config_pins_every_sandbox_control(tmp_path):
    text = sandbox_config_yaml(
        _spec(tmp_path),
        base_url="http://127.0.0.1:9/v1",
        model="fake-model",
        api_max_retries=0,
    )
    for expected in (
        "backend: docker",
        "container_persistent: false",
        "docker_persist_across_processes: false",
        "docker_network: false",
        "docker_mount_cwd_to_workspace: false",
        "docker_forward_env: []",
        "api_max_retries: 0",
        '"--read-only"',
        '"--user"',
        '"10001:10001"',
        '"--cap-drop"',
    ):
        assert expected in text, expected
    assert f"{CANDIDATE_MOUNT}:ro" in text
    assert f"{EVIDENCE_MOUNT}:ro" in text
    assert f"{PROBE_MOUNT}:ro" in text
    # No host home, no docker socket, no implicit cwd mount.
    assert "/Users/" not in text or str(tmp_path) in text
    assert "docker.sock" not in text


def test_prepare_refuses_a_real_profile_home(tmp_path):
    real = Path.home() / ".hermes" / "profiles" / "factory-qa"
    with pytest.raises(OrchestrationError) as excinfo:
        _assert_owned_root(real)
    assert excinfo.value.reason == "capability_unenforced"
    with pytest.raises(OrchestrationError):
        _assert_owned_root(Path.home() / ".hermes" / "profiles")


def test_prepare_refuses_a_foreign_home_with_a_config(tmp_path):
    spec = _spec(tmp_path)
    spec.home.mkdir(parents=True)
    (spec.home / "config.yaml").write_text("model: {}\n")
    with pytest.raises(OrchestrationError) as excinfo:
        prepare_sandbox(spec, base_url="http://127.0.0.1:9/v1", model="m", api_key="k")
    assert "no factory sandbox marker" in excinfo.value.detail


def test_prepare_is_idempotent_for_an_owned_home(tmp_path):
    spec = _spec(tmp_path)
    first = prepare_sandbox(
        spec, base_url="http://127.0.0.1:9/v1", model="m", api_key="k"
    )
    second = prepare_sandbox(
        spec, base_url="http://127.0.0.1:9/v1", model="m", api_key="k"
    )
    assert first["config_digest"] == second["config_digest"]
    assert second["adopted_existing_home"] is False
    assert spec.marker.is_file()
    assert spec.config_path().is_file()


def test_provider_credentials_are_written_to_the_profile_env_not_the_child_env(
    tmp_path,
):
    spec = _spec(tmp_path)
    prepare_sandbox(
        spec,
        base_url="https://api.deepseek.com/v1",
        model="deepseek-flash",
        provider="deepseek",
        api_key=None,
        env_values={"DEEPSEEK_API_KEY": "placeholder-for-test"},
    )
    env_file = spec.home / ".env"
    assert "DEEPSEEK_API_KEY=placeholder-for-test" in env_file.read_text()
    env = child_environment(home=spec.home, base={})
    assert "DEEPSEEK_API_KEY" not in env


def test_attestor_without_a_provider_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HERMES_REPO", str(tmp_path / "missing-repo"))
    attestor = SandboxedReadonlyAttestor(
        root=tmp_path / "sandbox", executable="hermes", image=IMAGE
    )
    spec = _spec(tmp_path)
    receipt = attestor.attest(
        reviewer="factory-reviewer",
        candidate=str(spec.candidate),
        evidence=str(spec.evidence),
    )
    assert receipt.enforced is False
    assert receipt.reason == "capability_unenforced"
    assert "fake provider" in receipt.detail


def test_missing_candidate_or_evidence_fails_closed(tmp_path):
    attestor = SandboxedReadonlyAttestor(
        root=tmp_path / "sandbox", executable="hermes", image=IMAGE
    )
    receipt = attestor.attest(
        reviewer="factory-reviewer",
        candidate=str(tmp_path / "absent"),
        evidence=str(tmp_path / "also-absent"),
    )
    assert receipt.enforced is False
    assert "not a directory" in receipt.detail


def test_live_reviewer_still_refuses_without_an_attestor(tmp_path):
    reviewer = HermesReviewer(
        HermesWorker(artifact_root=tmp_path), NoReadonlyEnforcement()
    )
    with pytest.raises(OrchestrationError) as excinfo:
        reviewer.submit(
            operation_key="k",
            attempt=Attempt(task_id=CARD, run_id=7, profile="factory-runner"),
            reviewer="factory-reviewer",
            workspace=str(tmp_path),
            candidate_sha="a" * 40,
            base_sha="b" * 40,
            spec_digest="sha256:" + "c" * 64,
            evidence_digest="sha256:" + "d" * 64,
            artifacts=str(tmp_path),
            timeout_s=60,
        )
    assert excinfo.value.reason == "capability_unenforced"
    assert "blocked, not passed" in excinfo.value.detail


def _write_transcript(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_audit_accepts_only_the_measured_surface_and_review_root(tmp_path):
    transcript = tmp_path / "stream.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "type": "tool_use",
                "name": "read_file",
                "input": {"path": "/evidence/candidate/x"},
            },
            {
                "type": "tool_use",
                "name": "search_files",
                "input": {"path": "/evidence"},
            },
            {"type": "tool_use", "name": "search_files", "input": {"path": "/"}},
            {"type": "result", "text": 'review:\n{"outcome":"approve","comment":"ok"}'},
        ],
    )
    audit = audit_review_transcript(transcript)
    assert audit["violations"] == []
    assert audit["verdict"] == {"outcome": "approve", "comment": "ok"}
    assert set(audit["tool_uses"]) <= EXPECTED_REVIEW_TOOLS


def test_audit_flags_a_tool_outside_the_surface(tmp_path):
    transcript = tmp_path / "stream.jsonl"
    _write_transcript(
        transcript,
        [
            {"type": "tool_use", "name": "terminal", "input": {"command": "id"}},
            {"type": "result", "text": '{"outcome":"approve"}'},
        ],
    )
    audit = audit_review_transcript(transcript)
    assert any("outside surface" in item for item in audit["violations"])


def test_audit_flags_a_path_outside_the_review_root(tmp_path):
    transcript = tmp_path / "stream.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "type": "tool_use",
                "name": "read_file",
                "input": {"path": "/Users/someone/.hermes/auth.json"},
            },
            {"type": "result", "text": '{"outcome":"approve"}'},
        ],
    )
    audit = audit_review_transcript(transcript)
    assert any("outside review root" in item for item in audit["violations"])


def test_audit_ignores_a_verdict_with_an_unknown_outcome(tmp_path):
    transcript = tmp_path / "stream.jsonl"
    _write_transcript(
        transcript,
        [{"type": "result", "text": '{"outcome":"looks_good","comment":"nope"}'}],
    )
    assert audit_review_transcript(transcript)["verdict"] is None


def test_live_attestation_is_not_reachable_from_the_offline_suite(tmp_path):
    """The suite's offline boundary must block Docker.

    Live attestation is a host-side, explicitly invoked run
    (``scripts/attest_reviewer_sandbox.py``); a nominally mocked QC run must
    never create a container. This test records that separation.
    """

    attestor = SandboxedReadonlyAttestor(
        root=tmp_path / "sandbox", executable="hermes", image=IMAGE
    )
    spec = _spec(tmp_path)
    receipt = attestor.attest(
        reviewer="factory-reviewer",
        candidate=str(spec.candidate),
        evidence=str(spec.evidence),
    )
    assert receipt.enforced is False
    assert receipt.reason == "capability_unenforced"
    assert (
        "offline test boundary" in receipt.detail or "docker" in receipt.detail.lower()
    )
