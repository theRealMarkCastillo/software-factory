"""Schema, vocabulary and engine-independence guarantees (T2/T11 support)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from factory_orch_fixtures import (
    CARD,
    make_assignment,
    seed,
)

from factory_orchestration.errors import REASON_CODES, OrchestrationError
from factory_orchestration.state import (
    GRAPH_VERSION,
    STATE_SCHEMA_VERSION,
    Attempt,
    WorkerReceipt,
    approval_matches,
)

ROOT = Path(__file__).resolve().parents[1]


def _assignment(tmp_path: Path, **overrides):
    seeded = seed(tmp_path)
    return make_assignment(
        repo=seeded["repo"],
        workspace=seeded["workspace"],
        base=seeded["base"],
        spec=seeded["spec"],
        digest=seeded["digest"],
        artifacts=seeded["artifacts"],
        **overrides,
    )


def test_relative_paths_are_refused(tmp_path):
    with pytest.raises(OrchestrationError, match="invalid_assignment"):
        _assignment(tmp_path, workspace_path="workspace")


def test_short_sha_is_refused(tmp_path):
    with pytest.raises(OrchestrationError, match="full 40-hex"):
        _assignment(tmp_path, immutable_base_sha="abc123")


def test_implementer_and_reviewer_must_differ(tmp_path):
    with pytest.raises(OrchestrationError, match="different profiles"):
        _assignment(tmp_path, reviewer_profile="factory-runner")


def test_unsupported_graph_version_fails_closed(tmp_path):
    with pytest.raises(OrchestrationError, match="version_mismatch"):
        _assignment(tmp_path, graph_version="factory-card/v2")


def test_unsupported_state_schema_fails_closed(tmp_path):
    with pytest.raises(OrchestrationError, match="version_mismatch"):
        _assignment(tmp_path, state_schema_version=STATE_SCHEMA_VERSION + 1)


def test_attempt_requires_dispatcher_granted_identity():
    with pytest.raises(OrchestrationError, match="HERMES_KANBAN_TASK"):
        Attempt.from_env({"HERMES_PROFILE": "factory-runner"})
    attempt = Attempt.from_env(
        {
            "HERMES_KANBAN_TASK": CARD,
            "HERMES_KANBAN_RUN_ID": "12",
            "HERMES_PROFILE": "factory-runner",
            "HERMES_KANBAN_CLAIM_LOCK": "host:123",
        }
    )
    assert attempt.run_id == 12
    assert attempt.claim_lock == "host:123"


def test_worker_receipt_requires_a_candidate():
    attempt = Attempt(task_id=CARD, run_id=1, profile="factory-runner")
    with pytest.raises(OrchestrationError, match="candidate"):
        WorkerReceipt(
            operation_key="k",
            attempt=attempt,
            session_id="s",
            outcome="completed",
        )


def test_reason_vocabulary_is_closed():
    assert {
        "stale_attempt",
        "cross_attempt_resume_unsupported",
        "effect_unknown",
        "cancelled",
    } <= REASON_CODES
    with pytest.raises(ValueError):
        OrchestrationError("made_up_reason")


def test_approval_binds_candidate_base_spec_and_evidence():
    approval = {
        "approval_key": "k",
        "reviewer": "factory-qa",
        "outcome": "approve",
        "candidate_sha": "a" * 40,
        "base_sha": "b" * 40,
        "spec_digest": "sha256:" + "c" * 64,
        "evidence_digest": "sha256:" + "d" * 64,
    }
    kwargs = dict(
        candidate_sha=approval["candidate_sha"],
        base_sha=approval["base_sha"],
        spec_digest=approval["spec_digest"],
        evidence_digest=approval["evidence_digest"],
        implementer="factory-runner",
    )
    assert approval_matches(approval, **kwargs) is True
    changed = dict(kwargs, candidate_sha="e" * 40)
    assert approval_matches(approval, **changed) is False
    changed = dict(kwargs, evidence_digest="sha256:" + "f" * 64)
    assert approval_matches(approval, **changed) is False
    self_approved = dict(approval, reviewer="factory-runner")
    assert approval_matches(self_approved, **kwargs) is False


def test_engine_does_not_depend_on_langgraph():
    sources = list((ROOT / "factory").glob("*.py"))
    assert sources
    offenders = [
        path.name for path in sources if "langgraph" in path.read_text().lower()
    ]
    assert offenders == []
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "[project.optional-dependencies]" in pyproject
    assert "orchestration = [" in pyproject
    # The engine must import with LangGraph made unavailable.
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name.startswith('langgraph'):\n"
        "            raise ImportError('blocked by test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import factory.verify, factory.evidence, factory.runner\n"
        "print('ok')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_graph_and_schema_constants_are_versioned():
    assert GRAPH_VERSION == "factory-card/v1"
    assert STATE_SCHEMA_VERSION == 1
