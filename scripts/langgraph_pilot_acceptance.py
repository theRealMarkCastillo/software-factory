#!/usr/bin/env python3
"""Reproduce the installed-acceptance evidence for the orchestration pilot.

This script is **not** part of the offline test suite. It creates a disposable
Git repository under a scratch directory, runs the installed factory launcher
(one busybox service, no host ports, no protected projects), verifies the
resulting evidence with the installed verifier, then drives one graph run whose
acceptance goes through the installed launcher. The worker and reviewer in the
graph run are deterministic fixtures, so no model call or board mutation occurs.

Usage::

    uv run --extra orchestration python scripts/langgraph_pilot_acceptance.py \
        --run --scratch /path/to/disposable/scratch

Without ``--run`` it only prints what it would do. The caller is responsible for
confirming Docker is appropriate on the host and that the scratch path is
disposable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

LAUNCHER = Path("~/git/software-factory-ops/bin/factory")
CARD = "t_1a2b3c4d"
BASE_CARD = "t_lgaccept"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def build_repo(repo: Path) -> tuple[str, str]:
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".env.example").write_text("ACCEPTANCE_MARKER=1\n")
    (repo / "README.md").write_text("Disposable installed-acceptance fixture.\n")
    (repo / "compose.yml").write_text(
        "services:\n"
        "  test:\n"
        "    image: busybox:latest\n"
        '    command: ["sh", "-c", "echo acceptance-ok"]\n'
    )
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "base",
    )
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "CHANGE.md").write_text("candidate marker\n")
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "candidate",
    )
    return base, git(repo, "rev-parse", "HEAD")


def verify(artifacts: Path, repo: Path, base: str, candidate: str) -> dict:
    completed = subprocess.run(
        [
            str(LAUNCHER),
            "verify",
            "--artifacts-dir",
            str(artifacts),
            "--repo",
            str(repo),
            "--candidate",
            candidate,
            "--base",
            base,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    payload: dict = {"exit_code": completed.returncode}
    if completed.stdout.strip().startswith("{"):
        payload["result"] = json.loads(completed.stdout)
    else:
        payload["stdout"] = completed.stdout[-2000:]
        payload["stderr"] = completed.stderr[-2000:]
    return payload


def installed_acceptance(scratch: Path, repo: Path, artifacts: Path) -> dict:
    base, candidate = build_repo(repo)
    run = subprocess.run(
        [
            str(LAUNCHER),
            "run",
            "--card",
            BASE_CARD,
            "--repo",
            str(repo),
            "--compose",
            "compose.yml",
            "--test-service",
            "test",
            "--artifacts-root",
            str(artifacts),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    manifest = next(artifacts.glob("*/evidence-manifest.json"), None)
    result = {"base": base, "candidate": candidate, "run_exit": run.returncode}
    if manifest is not None:
        result["evidence_reference"] = str(manifest.parent)
        result["verify"] = verify(manifest.parent, repo, base, candidate)
    return result


def graph_pilot(scratch: Path, repo: Path, base: str, candidate: str) -> dict:
    from factory_orchestration.adapters.fake import FakeBoard, FakeReviewer, FakeWorker
    from factory_orchestration.adapters.gates import InstalledLauncherGate
    from factory_orchestration.graph import Deps, thread_id
    from factory_orchestration.runner import drive, inspect_run
    from factory_orchestration.state import Attempt, RunAssignment
    from factory_orchestration.store import RunStore

    pilot = scratch / "pilot"
    if pilot.exists():
        shutil.rmtree(pilot)
    pilot.mkdir(parents=True)
    git(repo, "worktree", "prune")
    workspace = pilot / "workspace"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(workspace), candidate],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    spec = pilot / "spec.md"
    spec.write_text("# Pilot card\n\nRun installed acceptance on a disposable repo.\n")
    digest = "sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest()
    artifacts = pilot / "artifacts"
    state = pilot / "state"
    state.mkdir()
    (state / "admission.json").write_text(
        json.dumps({"enabled": True, "enrolled_cards": [CARD]})
    )
    attempt = Attempt(task_id=CARD, run_id=101, profile="factory-runner")
    board = FakeBoard()
    board.set_current(attempt)
    deps = Deps(
        store=RunStore(state),
        board=board,
        worker=FakeWorker(root=pilot / "effects", candidate_sha=candidate),
        gate=InstalledLauncherGate(LAUNCHER, pilot / "gate-receipts"),
        reviewer=FakeReviewer(root=pilot / "effects", outcome="approve"),
        state_dir=state,
        artifact_dir=artifacts,
        gate_timeout_s=300,
    )
    assignment = RunAssignment(
        board_id="software-factory",
        card_id=CARD,
        repository=str(repo),
        workspace_path=str(workspace),
        immutable_base_sha=base,
        spec_reference=str(spec),
        spec_digest=digest,
        template_id="bounded-card-v1",
        worker_profile="factory-runner",
        reviewer_profile="factory-qa",
        acceptance_argv=(
            str(LAUNCHER),
            "run",
            "--card",
            CARD,
            "--repo",
            str(repo),
            "--compose",
            "compose.yml",
            "--test-service",
            "test",
            "--artifacts-root",
            str(artifacts),
        ),
        acceptance_artifacts=str(artifacts),
        engine_release_reference="installed:" + str(LAUNCHER),
        engine_fingerprint="sha256:" + "a" * 64,
        host_policy_digest="sha256:" + "b" * 64,
    )
    result = drive(deps, assignment, attempt)
    view = inspect_run(deps, thread_id(assignment))
    return {
        "status": result.status,
        "stage": result.stage,
        "base": base,
        "candidate": candidate,
        "evidence_reference": result.state.get("evidence_reference", ""),
        "evidence_digest": result.state.get("evidence_digest", ""),
        "approval_reference": result.state.get("approval_reference", ""),
        "metrics": view["metrics"],
        "operations": [
            {key: op[key] for key in ("stage", "kind", "state", "repair_index")}
            for op in view["operations"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="actually execute")
    parser.add_argument(
        "--scratch",
        default="~/.hermes/profiles/factory-runner/cache/scratch/"
        "lg-installed-acceptance",
        help="disposable scratch directory",
    )
    args = parser.parse_args()
    scratch = Path(args.scratch).expanduser().resolve()
    if not args.run:
        print(
            f"would build a disposable repo and run {LAUNCHER} run/verify in {scratch}"
        )
        return 0
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    direct = installed_acceptance(scratch, scratch / "repo", scratch / "artifacts")
    pilot = graph_pilot(scratch, scratch / "repo", direct["base"], direct["candidate"])
    summary = {"installed_acceptance": direct, "graph_pilot": pilot}
    print(json.dumps(summary, indent=2)[:6000])
    (scratch / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    ok = (
        direct.get("run_exit") == 0
        and direct.get("verify", {}).get("exit_code") == 0
        and pilot.get("status") == "ready_for_authorized_completion"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
