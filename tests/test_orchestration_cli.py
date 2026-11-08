"""Operator CLI: inspect, export, admission refusal and metrics (T2/T9/T12)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from factory_orch_fixtures import seed

from factory_orchestration.graph import thread_id
from factory_orchestration.runner import drive, inspect_run

ROOT = Path(__file__).resolve().parents[1]


def _cli(state_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_orchestration",
            "--state-dir",
            str(state_dir),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_inspect_and_export_expose_provenance(tmp_path):
    seeded = seed(tmp_path)
    drive(seeded["deps"], seeded["assignment"], seeded["attempt"])

    inspected = _cli(seeded["state_dir"], "inspect", "t_abcd1234")
    assert inspected.returncode == 0, inspected.stderr
    payload = json.loads(inspected.stdout)
    assert payload["state"]["status"] == "ready_for_authorized_completion"
    assert payload["hermes_attempt"]["run_id"] == 7
    assert payload["checkpoint"]["present"] is True
    assert payload["metrics"]["observed"] is True
    assert payload["metrics"]["baseline"]["available"] is False
    assert payload["next_action"]["action"] == "return_to_lifecycle_owner"

    output = tmp_path / "export.json"
    exported = _cli(
        seeded["state_dir"], "export", "t_abcd1234", "--output", str(output)
    )
    assert exported.returncode == 0, exported.stderr
    bundle = json.loads(output.read_text())
    assert bundle["export_version"] == 1
    assert bundle["run"]["card_id"] == "t_abcd1234"
    assert bundle["approval"]["outcome"] == "approve"


def test_admission_is_opt_in_and_explicit(tmp_path):
    seeded = seed(tmp_path)
    (seeded["state_dir"] / "admission.json").unlink()
    result = _cli(
        seeded["state_dir"], "admission", "--enable", "--enroll", "t_abcd1234"
    )
    assert result.returncode == 0, result.stderr
    policy = json.loads((seeded["state_dir"] / "admission.json").read_text())
    assert policy == {"enabled": True, "enrolled_cards": ["t_abcd1234"]}

    disabled = _cli(seeded["state_dir"], "admission", "--disable")
    assert disabled.returncode == 0
    policy = json.loads((seeded["state_dir"] / "admission.json").read_text())
    assert policy["enabled"] is False


def test_run_refuses_an_unenrolled_card_before_any_effect(tmp_path):
    seeded = seed(tmp_path)
    (seeded["state_dir"] / "admission.json").write_text(
        json.dumps({"enabled": True, "enrolled_cards": []})
    )
    assignment = seeded["assignment"]
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(
        json.dumps(
            {
                "board_id": assignment.board_id,
                "card_id": assignment.card_id,
                "repository": assignment.repository,
                "workspace_path": assignment.workspace_path,
                "immutable_base_sha": assignment.immutable_base_sha,
                "spec_reference": assignment.spec_reference,
                "spec_digest": assignment.spec_digest,
                "template_id": assignment.template_id,
                "worker_profile": assignment.worker_profile,
                "reviewer_profile": assignment.reviewer_profile,
                "acceptance_argv": list(assignment.acceptance_argv),
                "acceptance_artifacts": assignment.acceptance_artifacts,
                "engine_release_reference": assignment.engine_release_reference,
                "engine_fingerprint": assignment.engine_fingerprint,
                "host_policy_digest": assignment.host_policy_digest,
                "max_repairs": assignment.max_repairs,
            }
        )
    )
    result = _cli(
        seeded["state_dir"],
        "run",
        "--assignment",
        str(assignment_path),
        "--attempt-run-id",
        "7",
        "--attempt-profile",
        "factory-runner",
    )
    assert result.returncode == 1
    refusal = json.loads(result.stderr)
    assert refusal["reason"] == "invalid_assignment"
    assert not (seeded["worker"].root / "worker-requests").exists()


def test_metrics_report_observed_values_without_a_baseline(tmp_path):
    seeded = seed(tmp_path)
    seeded["reviewer"].outcome = "inconclusive"
    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    view = inspect_run(seeded["deps"], thread_id(seeded["assignment"]))
    metrics = view["metrics"]
    assert result.status == "held"
    assert metrics["interventions"] >= 1
    assert metrics["diagnosis"]["last_reason_code"] == "review_inconclusive"
    assert metrics["baseline"]["available"] is False
    assert "no historical" in metrics["baseline"]["reason"]
