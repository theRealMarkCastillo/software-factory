"""Happy path, independent review, repair budget and provenance (T2/T6/T7)."""

from __future__ import annotations

import json
import threading

from factory_orch_fixtures import seed

from factory_orchestration.graph import _implementation_prompt, thread_id
from factory_orchestration.runner import drive, inspect_run, resume


def _drive(seeded):
    return drive(seeded["deps"], seeded["assignment"], seeded["attempt"])


def test_implementation_prompt_does_not_hand_the_worker_the_board():
    prompt = _implementation_prompt(
        {
            "card_id": "t_secret",
            "workspace_path": "/tmp/ws",
            "immutable_base_sha": "a" * 40,
            "spec_reference": "/tmp/spec.md",
            "spec_digest": "sha256:" + "b" * 64,
            "max_repairs": 2,
        },
        0,
    )
    assert "t_secret" not in prompt
    assert "Do not run `hermes kanban`" in prompt
    assert "the orchestrator owns" in prompt


def test_happy_path_returns_a_proposal_with_full_provenance(tmp_path):
    seeded = seed(tmp_path)
    result = _drive(seeded)

    assert result.status == "ready_for_authorized_completion", result.state
    state = result.state
    assert state["candidate_sha"] == seeded["candidate"]
    assert state["immutable_base_sha"] == seeded["base"]
    assert state["evidence_reference"] == str(seeded["artifacts"])
    assert state["evidence_digest"].startswith("sha256:")
    assert state["approval_reference"].startswith("sha256:")
    assert state["engine_release_reference"] == "test-release"
    assert state["graph_version"] == "factory-card/v1"

    view = inspect_run(seeded["deps"], thread_id(seeded["assignment"]))
    assert view["checkpoint"]["present"] is True
    assert view["checkpoint"]["bytes"] > 0
    assert view["next_action"]["action"] == "return_to_lifecycle_owner"
    assert view["hermes_attempt"]["run_id"] == 7
    assert {(op["stage"], op["state"]) for op in view["operations"]} == {
        ("request_implementation", "confirmed"),
        ("run_acceptance", "confirmed"),
        ("request_independent_review", "confirmed"),
    }
    assert view["approval"]["candidate_sha"] == seeded["candidate"]
    assert view["approval"]["reviewer"] == "factory-qa"


def test_only_independent_review_can_release_completion(tmp_path):
    seeded = seed(tmp_path)
    seeded["reviewer"].outcome = "approve"
    result = _drive(seeded)
    assert result.status == "ready_for_authorized_completion"
    approval = seeded["deps"].store.approval(result.state["approval_reference"])
    assert approval["reviewer"] != result.state["worker_profile"]


def test_repeated_review_changes_stop_before_the_repair_budget(tmp_path):
    seeded = seed(tmp_path)
    seeded["reviewer"].outcome = "changes_required"

    result = _drive(seeded)

    assert result.status == "held", result.state
    assert result.state["last_reason_code"] == "not_converging"
    assert result.state["repair_count"] == 1
    assert len(seeded["worker"].submitted) == 2
    assert seeded["deps"].store.repair_count(thread_id(seeded["assignment"])) == 1
    assert (
        seeded["deps"].store.run(thread_id(seeded["assignment"]))["repair_count"] == 1
    )
    # No approval exists, so no completion was proposed.
    assert result.state.get("approval_reference", "") == ""


def test_restart_cannot_reset_the_repair_budget(tmp_path):
    seeded = seed(tmp_path)
    seeded["reviewer"].outcome = "changes_required"
    first = _drive(seeded)
    assert first.status == "held"
    submissions = len(seeded["worker"].submitted)

    second = _drive(seeded)

    assert second.status == "held"
    assert second.state["last_reason_code"] == "not_converging"
    assert len(seeded["worker"].submitted) == submissions == 2
    assert seeded["deps"].store.repair_count(thread_id(seeded["assignment"])) == 1


def test_inconclusive_review_holds_without_repair(tmp_path):
    seeded = seed(tmp_path)
    seeded["reviewer"].outcome = "inconclusive"

    result = _drive(seeded)

    assert result.status == "held"
    assert result.state["last_reason_code"] == "review_inconclusive"
    assert len(seeded["worker"].submitted) == 1
    assert result.state.get("approval_reference", "") == ""


def _pending_review_payload(awaiting, *, reviewer, candidate, base, spec, evidence):
    return {
        "review": {
            "operation_key": awaiting["operation_key"],
            "reviewer": reviewer,
            "outcome": "approve",
            "candidate_sha": candidate,
            "base_sha": base,
            "spec_digest": spec,
            "evidence_digest": evidence,
            "graph_version": "factory-card/v1",
            "comment": "forged",
        }
    }


def test_implementer_cannot_approve_its_own_candidate(tmp_path):
    seeded = seed(tmp_path)
    seeded["reviewer"].outcome = "pending"
    paused = _drive(seeded)
    assert paused.status == "awaiting_review"
    state = paused.state
    payload = _pending_review_payload(
        paused.awaiting,
        reviewer=state["worker_profile"],
        candidate=state["candidate_sha"],
        base=state["immutable_base_sha"],
        spec=state["spec_digest"],
        evidence=state["evidence_digest"],
    )

    result = resume(
        seeded["deps"],
        thread_id(seeded["assignment"]),
        seeded["attempt"],
        payload=payload,
        resolved_hold="awaiting independent review",
    )

    assert result.status == "held"
    assert result.state["last_reason_code"] == "invalid_assignment"
    assert seeded["deps"].store.run(thread_id(seeded["assignment"]))["status"] == "held"


def test_review_bound_to_a_different_candidate_is_rejected(tmp_path):
    seeded = seed(tmp_path)
    seeded["reviewer"].outcome = "pending"
    paused = _drive(seeded)
    state = paused.state
    payload = _pending_review_payload(
        paused.awaiting,
        reviewer="factory-qa",
        candidate="f" * 40,
        base=state["immutable_base_sha"],
        spec=state["spec_digest"],
        evidence=state["evidence_digest"],
    )

    result = resume(
        seeded["deps"],
        thread_id(seeded["assignment"]),
        seeded["attempt"],
        payload=payload,
        resolved_hold="awaiting independent review",
    )

    assert result.status == "held"
    assert result.state["last_reason_code"] == "review_inconclusive"
    assert seeded["deps"].store.run(thread_id(seeded["assignment"]))["status"] == "held"


def test_worker_wait_is_persisted_and_resumes_without_resubmission(tmp_path):
    seeded = seed(tmp_path)
    seeded["worker"].outcomes = ["pending"]
    paused = _drive(seeded)

    assert paused.status == "awaiting_worker"
    assert paused.awaiting["kind"] == "await_worker"
    assert len(seeded["worker"].submitted) == 1
    view = inspect_run(seeded["deps"], thread_id(seeded["assignment"]))
    assert view["next_action"]["action"] == "resolve_pending_effect"

    key = paused.awaiting["operation_key"]
    receipt = {
        "operation_key": key,
        "session_id": "session-1",
        "outcome": "completed",
        "candidate_sha": seeded["candidate"],
        "artifact_references": [],
        "reported_tests": ["pytest -q"],
        "detail": "",
    }
    result = resume(
        seeded["deps"],
        thread_id(seeded["assignment"]),
        seeded["attempt"],
        payload={"receipt": receipt},
        resolved_hold="worker request awaited",
    )

    assert result.status == "ready_for_authorized_completion", result.state
    assert len(seeded["worker"].submitted) == 1


def test_drive_does_not_leave_threads_or_scheduler_behind(tmp_path):
    seeded = seed(tmp_path)
    _drive(seeded)
    assert threading.active_count() == 1
    exported = seeded["deps"].store.export(
        thread_id(seeded["assignment"]),
        inspect_run(seeded["deps"], thread_id(seeded["assignment"]))["state"],
    )
    json.dumps(exported)
    assert exported["export_version"] == 1
