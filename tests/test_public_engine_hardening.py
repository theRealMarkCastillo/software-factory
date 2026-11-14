"""Public defaults and fail-closed orchestration admission regressions."""

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from factory_orch_fixtures import seed

from factory_orchestration.adapters import hermes, installed
from factory_orchestration.cli import build_parser
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.runner import drive


def test_public_tracked_sources_have_no_personal_paths():
    result = subprocess.run(
        [
            "git",
            "grep",
            "-n",
            "/" + "Users/",
            "--",
            "factory",
            "factory_orchestration",
            "scripts",
            "tests",
            "docs/langgraph-hybrid/operator-runbook.md",
            "docs/langgraph-hybrid/reviewer-sandbox.md",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize("use_env", [True, False])
def test_ops_defaults_are_resolved_after_import(tmp_path, monkeypatch, use_env):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("FACTORY_OPS_ROOT", raising=False)
    expected = tmp_path / "git" / "software-factory-ops"
    if use_env:
        expected = tmp_path / "custom-ops"
        monkeypatch.setenv("FACTORY_OPS_ROOT", str(expected))
    args = build_parser().parse_args(
        ["--state-dir", str(tmp_path), "inspect", "t_deadbeef"]
    )
    assert Path(args.launcher) == expected / "bin" / "factory"
    with pytest.raises(FileNotFoundError) as exc:
        installed.discover()
    assert str(expected) in str(exc.value)


@pytest.mark.parametrize("bad_spec", ["outside", "untracked", "mismatch", "escape"])
def test_spec_admission_refuses_before_any_effect(tmp_path, bad_spec):
    seeded = seed(tmp_path)
    assignment = seeded["assignment"]
    if bad_spec == "mismatch":
        assignment = replace(assignment, spec_digest="sha256:" + "0" * 64)
    else:
        outside = tmp_path / "external.md"
        outside.write_bytes(seeded["spec"].read_bytes())
        spec = outside
        if bad_spec == "untracked":
            spec = seeded["repo"] / "untracked.md"
            spec.write_bytes(outside.read_bytes())
        elif bad_spec == "escape":
            spec = seeded["repo"] / "escape.md"
            spec.symlink_to(outside)
        assignment = replace(assignment, spec_reference=str(spec))
    seeded["deps"].worker = Mock(wraps=seeded["worker"])
    seeded["deps"].gate = Mock(wraps=seeded["gate"])
    seeded["deps"].reviewer = Mock(wraps=seeded["reviewer"])
    with pytest.raises(OrchestrationError) as exc:
        drive(seeded["deps"], assignment, seeded["attempt"])
    assert exc.value.reason == "invalid_assignment"
    for adapter in (
        seeded["deps"].worker,
        seeded["deps"].gate,
        seeded["deps"].reviewer,
    ):
        assert adapter.mock_calls == []


@pytest.mark.parametrize("source", ["repo", "workspace"])
def test_tracked_spec_with_base_blob_digest_is_admitted(tmp_path, source):
    seeded = seed(tmp_path)
    spec = seeded["repo" if source == "repo" else "workspace"] / "card-spec.md"
    assignment = replace(seeded["assignment"], spec_reference=str(spec))
    result = drive(seeded["deps"], assignment, seeded["attempt"])
    assert result.status == "ready_for_authorized_completion"


@pytest.mark.parametrize(
    "provider,key",
    [
        ("openai-codex", "present-but-not-oauth"),
        ("unmapped", "present-but-not-mapped"),
        ("openai", ""),
        ("openai", "   "),
    ],
)
def test_review_credentials_refuse_before_sandbox(tmp_path, monkeypatch, provider, key):
    seeded = seed(tmp_path)
    monkeypatch.setattr(hermes, "read_profile_provider_env", lambda profile: {})
    attestor = Mock()
    attestor.attest.side_effect = OrchestrationError(
        "review_unavailable", "unexpected launch"
    )
    reviewer = hermes.HermesReviewer(
        hermes.HermesWorker(artifact_root=tmp_path / "review-artifacts"),
        attestor,
        review_provider=provider,
        review_model="review-model",
        provider_env={"OPENAI_API_KEY": key},
    )
    seeded["deps"].reviewer = reviewer
    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert result.status == "held"
    assert result.state["last_reason_code"] == "review_credentials_unavailable"
    detail = result.state["last_reason_detail"]
    assert provider in detail
    for flag in ("--review-provider", "--review-model", "--review-base-url"):
        assert flag in detail
    assert not result.state.get("review_results")
    assert result.state["repair_count"] == 0
    assert attestor.mock_calls == []
    assert not (tmp_path / "review-artifacts").exists()
