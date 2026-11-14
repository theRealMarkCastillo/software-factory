"""R2-A reviewer sandbox: configuration, fail-closed gates and transcript audit.

The live attestation (real Docker container, real Hermes tool stack) only runs
with ``FACTORY_LIVE_SANDBOX=1``; it is executed explicitly for acceptance
evidence. Everything else here is deterministic and offline.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest
from factory_orch_fixtures import CARD

from factory_orchestration.adapters import reviewer_sandbox as rs
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
    expected_volumes,
    prepare_sandbox,
    sandbox_config_yaml,
    volume_map,
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


def _mock_effective_config(monkeypatch, spec, *, volumes=None, image=None, extra=None):
    values = {
        "terminal.backend": "docker",
        "terminal.container_persistent": False,
        "terminal.docker_network": False,
        "terminal.docker_forward_env": [],
        "terminal.docker_mount_cwd_to_workspace": False,
    }

    def fake_run(argv, *, env, timeout, cwd=None):
        key = argv[argv.index("config") + 2]
        return subprocess.CompletedProcess(argv, 0, json.dumps(values[key]), "")

    mounts = volumes or [
        f"{source}:{destination}:{mode}"
        for destination, (source, mode) in expected_volumes(spec).items()
    ]
    payloads = {
        "terminal.docker_volumes": mounts,
        "terminal.docker_extra_args": extra or ["--read-only", "--user", "10001:10001"],
        "terminal.docker_image": image or spec.image,
    }
    monkeypatch.setattr(rs, "_run", fake_run)
    monkeypatch.setattr(rs, "_config_json", lambda exe, key, env: payloads[key])
    return {
        "values": values,
        "volumes": mounts,
        "extra_args": payloads["terminal.docker_extra_args"],
        "image": payloads["terminal.docker_image"],
    }


def test_volume_map_refuses_ambiguous_entries():
    assert volume_map(["/a:/evidence/candidate:ro"]) == {
        "/evidence/candidate": ("/a", "ro")
    }
    for bad in (
        ["/a:/evidence/candidate"],
        ["/a:/evidence/candidate:ro:extra"],
        ["/a:/evidence/candidate:ro", "/b:/evidence/candidate:ro"],
        "not-a-list",
    ):
        with pytest.raises(OrchestrationError):
            volume_map(bad)


def test_effective_config_binds_the_exact_mount_sources(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    attestor = SandboxedReadonlyAttestor(root=spec.root, image=IMAGE)
    observed = _mock_effective_config(monkeypatch, spec)
    assert attestor._read_effective_config(spec, {}) == observed

    # Path substitution between attestation and use: the destination and the
    # read-only flag are unchanged, only the host source moves.
    substituted = [
        f"{str(tmp_path / 'elsewhere')}:{CANDIDATE_MOUNT}:ro",
        f"{str(spec.evidence.absolute())}:{EVIDENCE_MOUNT}:ro",
        f"{str((spec.probe or tmp_path).absolute())}:{PROBE_MOUNT}:ro",
    ]
    _mock_effective_config(monkeypatch, spec, volumes=substituted)
    with pytest.raises(OrchestrationError) as excinfo:
        attestor._read_effective_config(spec, {})
    assert "do not match the code-owned mounts" in excinfo.value.detail

    # An extra mount is also a mismatch, even if it is read-only.
    extra_mount = f"{str(tmp_path / 'extra')}:/evidence/extra:ro"
    _mock_effective_config(
        monkeypatch, spec, volumes=expected_volumes_text(spec) + [extra_mount]
    )
    with pytest.raises(OrchestrationError):
        attestor._read_effective_config(spec, {})


def expected_volumes_text(spec: SandboxSpec) -> list[str]:
    return [
        f"{source}:{destination}:{mode}"
        for destination, (source, mode) in expected_volumes(spec).items()
    ]


def test_verify_terminal_refuses_an_unattested_or_changed_sandbox(
    tmp_path, monkeypatch
):
    spec = _spec(tmp_path)
    effective = _effective_config_for(spec)
    attestor = SandboxedReadonlyAttestor(root=spec.root, image=IMAGE)

    with pytest.raises(OrchestrationError) as excinfo:
        attestor.verify_terminal(spec)
    assert "no executed attestation" in excinfo.value.detail

    attestor._attested = _attested_binding(spec, effective)
    monkeypatch.setattr(
        attestor, "_read_effective_config", lambda s, e: dict(effective)
    )
    attestor.verify_terminal(spec)  # unchanged: passes

    # A tree changed after the executed preflight.
    (spec.candidate / "late.txt").write_text("late change\n")
    with pytest.raises(OrchestrationError) as excinfo:
        attestor.verify_terminal(spec)
    assert "candidate_digest changed" in excinfo.value.detail

    # A different candidate path with the same digests is still refused.
    (spec.candidate / "late.txt").unlink()
    other = tmp_path / "candidate-copy"
    other.mkdir()
    (other / "README.md").write_text("# candidate\n")
    moved = SandboxSpec(
        name=spec.name,
        root=spec.root,
        candidate=other,
        evidence=spec.evidence,
        image=spec.image,
        probe=spec.probe,
    )
    with pytest.raises(OrchestrationError) as excinfo:
        attestor.verify_terminal(moved)
    assert "not the attested" in excinfo.value.detail


def _effective_config_for(spec: SandboxSpec) -> dict:
    return {
        "values": {
            "terminal.backend": "docker",
            "terminal.container_persistent": False,
            "terminal.docker_network": False,
            "terminal.docker_forward_env": [],
            "terminal.docker_mount_cwd_to_workspace": False,
        },
        "volumes": expected_volumes_text(spec),
        "extra_args": ["--read-only", "--user", "10001:10001"],
        "image": spec.image,
    }


def _attested_binding(spec: SandboxSpec, effective: dict) -> dict:
    return {
        "candidate": str(Path(spec.candidate).absolute()),
        "evidence": str(Path(spec.evidence).absolute()),
        "probe": str(Path(spec.probe).absolute()) if spec.probe else "",
        "candidate_digest": spec.candidate_digest,
        "evidence_digest": spec.evidence_digest,
        "image": spec.image,
        "home": str(spec.home),
        "uid": spec.uid,
        "effective": effective,
        "attested_at": 1,
    }


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
            spec_path="/evidence/candidate/spec.md",
            changed_files=(),
            changed_files_truncated=0,
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


def test_audit_allows_container_root_only_for_search_files(tmp_path):
    transcript = tmp_path / "stream.jsonl"
    _write_transcript(
        transcript,
        [
            {"type": "tool_use", "name": "search_files", "input": {"path": "/"}},
            {"type": "tool_use", "name": "read_file", "input": {"path": "/"}},
            {"type": "result", "text": '{"outcome":"approve"}'},
        ],
    )
    violations = audit_review_transcript(transcript)["violations"]
    assert violations == ["path outside review root: /"]


def test_audit_ignores_a_verdict_with_an_unknown_outcome(tmp_path):
    transcript = tmp_path / "stream.jsonl"
    _write_transcript(
        transcript,
        [{"type": "result", "text": '{"outcome":"looks_good","comment":"nope"}'}],
    )
    assert audit_review_transcript(transcript)["verdict"] is None


def test_image_digest_delegates_to_policy_and_fails_closed(monkeypatch):
    """Image pinning is a sanctioned policy call, never a raw Docker call."""

    from factory import policy

    pinned = "python@sha256:" + "c" * 64
    monkeypatch.setattr(policy, "container_image_digest", lambda image: pinned)
    assert rs.image_digest("python:3.12-slim") == pinned

    def refuse(image):
        raise policy.PolicyViolation(f"no digest for {image!r}")

    monkeypatch.setattr(policy, "container_image_digest", refuse)
    with pytest.raises(OrchestrationError) as excinfo:
        rs.image_digest("python:3.12-slim")
    assert excinfo.value.reason == "capability_unenforced"
    assert "no digest" in excinfo.value.detail


def test_image_digest_reports_a_policy_refusal_without_calling_docker(monkeypatch):
    """An invalid reference is refused by policy before any subprocess runs."""

    from factory import policy

    calls = []
    monkeypatch.setattr(
        policy.subprocess, "run", lambda cmd, **kwargs: calls.append(list(cmd))
    )
    with pytest.raises(OrchestrationError) as excinfo:
        rs.image_digest("-rm")
    assert excinfo.value.reason == "capability_unenforced"
    assert calls == []


def test_only_factory_policy_reaches_docker():
    """AGENTS.md: keep Docker calls in ``factory/policy.py``.

    The orchestration package may *name* the Docker backend in generated
    configuration, but no module may build a ``docker`` argv of its own — the
    R2-A image pin used to call ``docker image inspect`` directly.
    """

    root = Path(__file__).resolve().parents[1] / "factory_orchestration"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = ast.unparse(node.func)
            if not func.endswith(
                ("subprocess.run", "subprocess.Popen", "subprocess.check_output")
            ):
                continue
            first = node.args[0]
            if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
                continue
            head = first.elts[0]
            if (
                isinstance(head, ast.Constant)
                and isinstance(head.value, str)
                and head.value.split()[0] in {"docker", "docker-compose"}
            ):
                offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    assert offenders == [], offenders


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
