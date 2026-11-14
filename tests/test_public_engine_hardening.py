"""Public defaults and fail-closed orchestration admission regressions."""

import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from factory_orch_fixtures import seed

from factory_orchestration.adapters import hermes, installed
from factory_orchestration.cli import build_parser
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.runner import drive


def _public_source_leaks(root):
    """Scan current tracked sources, not archived evidence or local files.

    FACTORY_PUBLIC_DENY_NAMES is an optional comma-separated list of private
    host/user names. Resolve the real home at runtime; never persist it here.
    """
    names = [
        name.strip()
        for name in os.environ.get("FACTORY_PUBLIC_DENY_NAMES", "").split(",")
    ]
    private = [str(Path.home()), *(name for name in names if name)]
    personal = re.compile(
        "|".join(re.escape(value) for value in private), re.IGNORECASE
    )
    secrets = re.compile(
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"
        r"|(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]{20,}"
        r"|AKIA[A-Z0-9]{16}"
    )
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            "factory",
            "factory_orchestration",
            "scripts",
            "tests",
            "README.md",
            "docs/architecture.md",
            "docs/langgraph-hybrid/operator-runbook.md",
            "docs/langgraph-hybrid/reviewer-sandbox.md",
            ":(exclude)tests/fixtures/kanban-show.json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    leaks = []
    for name in filter(None, result.stdout.split("\0")):
        path = root / name
        if not path.is_file():
            continue
        for line_number, line in enumerate(
            path.read_text(errors="replace").splitlines(), 1
        ):
            if personal.search(line) or secrets.search(line):
                # Report locations only: do not echo leaked credentials into logs.
                leaks.append(f"{name}:{line_number}")
    return leaks


def test_public_tracked_sources_have_no_personal_paths():
    assert not _public_source_leaks(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize(
    "text,leaks",
    [
        ("/Users/<user>/repo and /home/<user>/repo", False),
        ("real-home", True),
        ("deny-listed", True),
        ("-----BEGIN " + "PRIVATE KEY-----", True),
    ],
)
def test_public_leak_scan_is_personal_not_platform_wide(
    tmp_path, monkeypatch, text, leaks
):
    monkeypatch.setenv(
        "FACTORY_PUBLIC_DENY_NAMES", "synthetic-private-host,synthetic-private-user"
    )
    if text == "real-home":
        text = str(Path.home() / "repo")
    elif text == "deny-listed":
        text = "host: synthetic-private-host"
    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "example.txt").write_text(text)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "factory/example.txt"], check=True
    )
    assert bool(_public_source_leaks(tmp_path)) is leaks


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
    expected.mkdir(parents=True)
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
    operations = seeded["deps"].store.operations(result.thread_id)
    review = next(op for op in operations if op["kind"] == "review")
    assert review["state"] == "failed"
    receipt = json.loads(review["receipt_json"])
    assert receipt["status"] == "not-run"
    assert receipt["reason"] == "review_credentials_unavailable"
    assert "outcome" not in receipt


def test_valid_review_key_reaches_the_sandbox_home_and_nowhere_else(
    tmp_path, monkeypatch
):
    """Hermes resolves provider credentials from the profile home's ``.env``
    (the host gateway multiplexes profiles and never unions process env), so
    the selected review key must be in the disposable sandbox home's ``.env``
    (0600) -- that file only, holding only that key."""
    from factory_orchestration.adapters.base import AttestationReceipt
    from factory_orchestration.adapters.reviewer_sandbox import SandboxSpec

    canary = "test-only-review-key-7e837194f5d249fe"
    other = "test-only-unselected-key-1b9c2f0e44a7"
    monkeypatch.setattr(
        hermes,
        "read_profile_provider_env",
        lambda profile: {
            "OPENAI_API_KEY": canary,
            "ANTHROPIC_API_KEY": other,
        },
    )
    candidate = tmp_path / "candidate"
    evidence = tmp_path / "evidence"
    candidate.mkdir()
    evidence.mkdir()
    spec = SandboxSpec(
        name="factory-reviewer",
        root=tmp_path / "sandbox",
        candidate=candidate,
        evidence=evidence,
        image="python:3.12-slim@sha256:" + "0" * 64,
        probe=tmp_path / "probe",
    )
    attestor = Mock()
    attestor.attest.return_value = AttestationReceipt(
        enforced=True, reason="ok", home=str(spec.home)
    )
    attestor.spec_for.return_value = spec
    launch = Mock(return_value=SimpleNamespace(pid=12345))
    monkeypatch.setattr(hermes.subprocess, "Popen", launch)
    reviewer = hermes.HermesReviewer(
        hermes.HermesWorker(artifact_root=tmp_path / "artifacts"),
        attestor,
        review_provider="custom",
        review_model="review-model",
        review_base_url="http://127.0.0.1:1/v1",
    )
    from factory_orchestration.state import Attempt

    _, verdict = reviewer.submit(
        operation_key="review-valid-key",
        attempt=Attempt(task_id="t_deadbeef", run_id=7, profile="factory-runner"),
        reviewer="factory-qa",
        workspace=str(candidate),
        candidate_sha="a" * 40,
        base_sha="b" * 40,
        spec_digest="sha256:" + "c" * 64,
        evidence_digest="sha256:" + "d" * 64,
        artifacts=str(evidence),
        timeout_s=60,
    )
    assert verdict is None
    launch.assert_called_once()
    assert launch.call_args.kwargs["env"]["OPENAI_API_KEY"] == canary
    attestor.verify_terminal.assert_called_once_with(spec)
    env_file = spec.home / ".env"
    assert env_file.read_text().splitlines() == [f"OPENAI_API_KEY={canary}"]
    assert env_file.stat().st_mode & 0o777 == 0o600
    holders = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and canary.encode() in path.read_bytes()
    ]
    assert holders == [env_file]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert other.encode() not in path.read_bytes(), str(path)
