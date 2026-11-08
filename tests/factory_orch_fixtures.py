"""Shared offline fixtures for the orchestration tests.

Every repository, worktree, evidence directory and state directory is created
under pytest's temporary tree. No Docker, board access or network is used.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from factory.evidence import (
    REQUIRED_ARTIFACTS,
    SIDECAR_FILENAME,
    write_evidence_manifest,
)
from factory_orchestration.adapters.fake import (
    FakeBoard,
    FakeGate,
    FakeReviewer,
    FakeWorker,
)
from factory_orchestration.graph import Deps
from factory_orchestration.state import Attempt, RunAssignment
from factory_orchestration.store import RunStore

CARD = "t_abcd1234"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


def build_repo(root: Path) -> tuple[Path, str, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".env.example").write_text("A=1\n")
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / "tests" / "fixtures" / "case.json").write_text('{"case":1}\n')
    base = commit(repo, "base")
    (repo / "app.py").write_text("x = 2\ny = 3\n")
    candidate = commit(repo, "candidate")
    return repo, base, candidate


def add_worktree(repo: Path, revision: str, path: Path) -> Path:
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(path), revision],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return path


def make_evidence(
    artifacts: Path,
    repo: Path,
    base: str,
    candidate: str,
    **overrides,
) -> Path:
    artifacts.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_ARTIFACTS:
        (artifacts / name).write_text(f"{name}: ran\n")
    params = {
        "entry_point": "docker compose test",
        "command": ["docker", "compose", "up"],
        "mode": "acceptance",
        "source_start": {"sha": candidate, "status_porcelain": ""},
        "source_end": {"sha": candidate, "status_porcelain": ""},
        "base": {"ref": "main", "kind": "commit", "sha": base},
        "runtime": {
            "teardown": {"attempted": True, "ok": True, "detail": ""},
            "safety": {
                "postflight": "clean",
                "protected_changes": [],
                "unexpected_changes": [],
                "collateral_damage": False,
            },
        },
        "artifacts": list(REQUIRED_ARTIFACTS),
        "audit": {"status": "pass", "passed": True},
        "coverage": {
            "status": "pass",
            "passed": True,
            "base": "main",
            "base_sha": base,
            "base_kind": "commit",
        },
        "datamodel": {"status": "skipped", "reason": "no_data_model"},
        "execution": {
            "exit_code": 0,
            "passed": True,
            "stage": "complete",
            "duration_s": 1.0,
        },
        "fixture_paths": ["tests/fixtures/case.json"],
        "gates_run": True,
    }
    params.update(overrides)
    write_evidence_manifest(artifacts, repo, **params)
    return artifacts


def refresh_sidecar(artifacts: Path) -> None:
    manifest = artifacts / "evidence-manifest.json"
    payload = json.loads(manifest.read_text())
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    manifest.write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    (artifacts / SIDECAR_FILENAME).write_text(f"{digest}  evidence-manifest.json\n")


def make_spec(root: Path) -> tuple[Path, str]:
    spec = root / "card-spec.md"
    spec.write_text("# Bounded card\n\nImplement the reviewed change.\n")
    digest = "sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest()
    return spec, digest


def enable_admission(
    state_dir: Path, cards: tuple[str, ...] = (CARD,), *, enabled: bool = True
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "admission.json").write_text(
        json.dumps({"enabled": enabled, "enrolled_cards": list(cards)})
    )


def make_assignment(
    *,
    repo: Path,
    workspace: Path,
    base: str,
    spec: Path,
    digest: str,
    artifacts: Path,
    card: str = CARD,
    **overrides,
) -> RunAssignment:
    params = {
        "board_id": "software-factory",
        "card_id": card,
        "repository": str(repo),
        "workspace_path": str(workspace),
        "immutable_base_sha": base,
        "spec_reference": str(spec),
        "spec_digest": digest,
        "template_id": "bounded-card-v1",
        "worker_profile": "factory-runner",
        "reviewer_profile": "factory-qa",
        "acceptance_argv": ("true",),
        "acceptance_artifacts": str(artifacts),
        "engine_release_reference": "test-release",
        "engine_fingerprint": "sha256:" + "a" * 64,
        "host_policy_digest": "sha256:" + "b" * 64,
        "max_repairs": 2,
    }
    params.update(overrides)
    return RunAssignment(**params)


def make_deps(
    *,
    state_dir: Path,
    root: Path,
    board: FakeBoard,
    worker: FakeWorker,
    gate: FakeGate,
    reviewer: FakeReviewer,
) -> Deps:
    return Deps(
        store=RunStore(state_dir),
        board=board,
        worker=worker,
        gate=gate,
        reviewer=reviewer,
        state_dir=state_dir,
    )


def seed(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    repo, base, candidate = build_repo(root)
    workspace = add_worktree(repo, candidate, root / "workspace")
    artifacts = make_evidence(root / "artifacts", repo, base, candidate)
    spec, digest = make_spec(root)
    state_dir = root / "state"
    enable_admission(state_dir)
    attempt = Attempt(task_id=CARD, run_id=7, profile="factory-runner")
    board = FakeBoard()
    board.set_current(attempt)
    worker = FakeWorker(root=root / "effects", candidate_sha=candidate)
    gate = FakeGate(root=root / "effects", evidence_dir=artifacts)
    reviewer = FakeReviewer(root=root / "effects")
    deps = make_deps(
        state_dir=state_dir,
        root=root,
        board=board,
        worker=worker,
        gate=gate,
        reviewer=reviewer,
    )
    assignment = make_assignment(
        repo=repo,
        workspace=workspace,
        base=base,
        spec=spec,
        digest=digest,
        artifacts=artifacts,
    )
    return {
        "repo": repo,
        "base": base,
        "candidate": candidate,
        "workspace": workspace,
        "artifacts": artifacts,
        "spec": spec,
        "digest": digest,
        "state_dir": state_dir,
        "attempt": attempt,
        "board": board,
        "worker": worker,
        "gate": gate,
        "reviewer": reviewer,
        "deps": deps,
        "assignment": assignment,
    }


def request_count(worker: FakeWorker) -> int:
    directory = worker.root / "worker-requests"
    return len(list(directory.glob("*.json"))) if directory.is_dir() else 0
