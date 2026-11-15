"""Workspace-only admission through drive and the shipped CLI, offline."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from factory_orch_fixtures import git, make_deps, request_count, seed

from factory_orchestration.adapters.fake import (
    FakeBoard,
    FakeGate,
    FakeReviewer,
    FakeWorker,
)
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import thread_id
from factory_orchestration.runner import current_state, drive, resume
from factory_orchestration.state import Attempt

ROOT = Path(__file__).resolve().parents[1]
BASE = "ae3646215396af57016ebab57855cf89f249cc3e"
INVALID = (
    "repository-copy",
    "external",
    "traversal",
    "symlink-escape",
    "missing",
    "directory",
    "untracked",
    "base-symlink-mode",
    "base-tree-mode",
    "base-digest",
    "readable-digest",
    "inside-link-untracked",
    "absent-workspace",
)
VALID = ("workspace", "inside-link", "executable-blob")


def configure(seeded, kind):
    assignment = seeded["assignment"]
    workspace = seeded["workspace"]
    spec = seeded["spec"]
    outside = seeded["repo"] / "card-spec.md"
    if kind == "repository-copy":
        assert outside.read_bytes() == spec.read_bytes()
        spec = outside
    elif kind in {"external", "traversal", "symlink-escape"}:
        external = workspace.parent / "external.md"
        external.write_bytes(spec.read_bytes())
        spec = external
        if kind == "traversal":
            spec = workspace / ".." / external.name
        elif kind == "symlink-escape":
            spec = workspace / "escape.md"
            spec.symlink_to(external)
    elif kind == "missing":
        spec = workspace / "missing.md"
    elif kind == "directory":
        spec = workspace
    elif kind == "untracked":
        spec = workspace / "untracked.md"
        spec.write_bytes(seeded["spec"].read_bytes())
    elif kind in {"base-symlink-mode", "base-tree-mode", "executable-blob"}:
        # Build a real immutable tree entry without changing the candidate tree.
        mode = {
            "base-symlink-mode": "120000",
            "base-tree-mode": "040000",
            "executable-blob": "100755",
        }[kind]
        obj = git(
            seeded["repo"], "rev-parse", assignment.immutable_base_sha + ":card-spec.md"
        )
        obj_type = "blob"
        if kind == "base-tree-mode":
            obj = git(
                seeded["repo"], "rev-parse", assignment.immutable_base_sha + "^{tree}"
            )
            obj_type = "tree"
        tree = subprocess.run(
            ["git", "mktree"],
            cwd=seeded["repo"],
            input=f"{mode} {obj_type} {obj}\tcard-spec.md\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assignment = replace(assignment, immutable_base_sha=tree)
    elif kind == "base-digest":
        spec.write_text("# Changed spec\n")
        assignment = replace(
            assignment,
            spec_digest="sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest(),
        )
    elif kind == "readable-digest":
        spec.write_text("# Changed spec\n")
    elif kind in {"inside-link", "inside-link-untracked"}:
        target = spec
        if kind == "inside-link-untracked":
            target = workspace / "untracked-target.md"
            target.write_bytes(spec.read_bytes())
        spec = workspace / "alias.md"
        spec.symlink_to(target.name)
    elif kind == "absent-workspace":
        assignment = replace(
            assignment, workspace_path=str(workspace.parent / "not-created")
        )
        spec = outside

    elif kind != "workspace":
        raise AssertionError(kind)
    seeded["assignment"] = replace(assignment, spec_reference=str(spec))


def assert_no_admission(seeded):
    store = seeded["deps"].store
    assert store.run(thread_id(seeded["assignment"])) is None
    for table in ("runs", "operations", "events", "approvals"):
        assert (
            store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        )
    assert not store.checkpoint_path.exists()
    assert not (seeded["state_dir"] / "coordinator.lock").exists()
    assert request_count(seeded["worker"]) == 0
    assert seeded["gate"].submissions == []
    assert seeded["reviewer"].submissions == []
    assert not seeded["worker"].root.exists()
    if Path(seeded["assignment"].workspace_path).name == "not-created":
        assert not Path(seeded["assignment"].workspace_path).exists()


@pytest.mark.parametrize("kind", INVALID)
def test_drive_refuses_before_board_or_effects(tmp_path, monkeypatch, kind):
    seeded = seed(tmp_path)
    configure(seeded, kind)
    original_assignment = seeded["assignment"]
    views = []
    original_view = seeded["board"].view

    def view(attempt):
        views.append(attempt)
        return original_view(attempt)

    monkeypatch.setattr(seeded["board"], "view", view)
    with pytest.raises(OrchestrationError) as exc:
        drive(seeded["deps"], original_assignment, seeded["attempt"])
    assert exc.value.reason == "invalid_assignment"
    assert views == []
    assert seeded["assignment"] == original_assignment
    assert_no_admission(seeded)


@pytest.mark.parametrize("kind", VALID)
def test_drive_accepts_workspace_spec(tmp_path, kind):
    seeded = seed(tmp_path)
    configure(seeded, kind)
    # Stop at a genuine worker interrupt: admission must not rewrite the reference.
    seeded["worker"].outcomes = ["pending"]
    result = drive(seeded["deps"], seeded["assignment"], seeded["attempt"])
    assert result.status == "awaiting_worker", result.state
    assert result.state["spec_reference"] == seeded["assignment"].spec_reference
    assert request_count(seeded["worker"]) == 1
    assert seeded["deps"].store.run(result.thread_id) is not None
    assert seeded["deps"].store.checkpoint_path.is_file()


def test_same_root_preserves_existing_isolation_refusal(tmp_path):
    seeded = seed(tmp_path)
    with pytest.raises(OrchestrationError) as exc:
        replace(seeded["assignment"], workspace_path=str(seeded["repo"]))
    assert exc.value.reason == "invalid_assignment"
    assert "must differ" in exc.value.detail
    assert_no_admission(seeded)


@pytest.mark.parametrize("kind", (*INVALID, "same-root"))
def test_actual_cli_refuses_without_board_or_execution(tmp_path, kind):
    seeded = seed(tmp_path)
    if kind != "same-root":
        configure(seeded, kind)
    assignment_file = tmp_path / "assignment.json"
    payload = asdict(seeded["assignment"])
    if kind == "same-root":
        payload["workspace_path"] = payload["repository"]
    assignment_file.write_text(json.dumps(payload))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "unexpected-hermes-call"
    hermes = bin_dir / "hermes"
    hermes.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 99\n")
    hermes.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_orchestration",
            "--state-dir",
            str(seeded["state_dir"]),
            "--launcher",
            str(bin_dir / "no-launcher"),
            "run",
            "--assignment",
            str(assignment_file),
            "--attempt-run-id",
            "7",
            "--attempt-profile",
            "factory-runner",
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    error = json.loads(result.stderr)
    assert error["reason"] == "invalid_assignment"
    expected = (
        "must differ" if kind == "same-root" else "reviewer-visible spec required"
    )
    assert expected in error["detail"]
    assert not marker.exists()
    assert_no_admission(seeded)


LEGACY_CREATE = r"""
import json, sys
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from factory_orch_fixtures import seed
from factory_orchestration import runner
assert Path(runner.__file__).resolve().is_relative_to(Path.cwd())
s = seed(Path(sys.argv[1]))
s["worker"].outcomes = ["pending"]
a = replace(s["assignment"], spec_reference=str(s["repo"] / "card-spec.md"))
r = runner.drive(s["deps"], a, s["attempt"])
assert r.status == "awaiting_worker", r.state
print(json.dumps({"thread": r.thread_id, "candidate": s["candidate"], "awaiting": r.awaiting}))
"""


def test_checkpoint_created_by_actual_base_resumes_without_new_admission(tmp_path):
    base_source = tmp_path / "base-source"
    base_source.mkdir()
    # Materialize real base blobs using read-only local Git commands. Archive is
    # deliberately forbidden by the suite's unchanged offline command guard.
    entries = git(
        ROOT,
        "ls-tree",
        "-r",
        BASE,
        "--",
        "factory",
        "factory_orchestration",
        "tests/factory_orch_fixtures.py",
    )
    for entry in entries.splitlines():
        metadata, name = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        assert kind == "blob" and mode in {"100644", "100755"}
        destination = base_source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            subprocess.run(
                ["git", "cat-file", "blob", oid],
                cwd=ROOT,
                capture_output=True,
                check=True,
            ).stdout
        )
    create_script = base_source / "create_checkpoint.py"
    create_script.write_text(LEGACY_CREATE)
    root = tmp_path / "legacy-run"
    created = subprocess.run(
        [sys.executable, str(create_script), str(root)],
        cwd=base_source,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    legacy = json.loads(created.stdout)
    attempt = Attempt(task_id="t_abcd1234", run_id=7, profile="factory-runner")
    board = FakeBoard()
    board.set_current(attempt)
    worker = FakeWorker(root=root / "effects", candidate_sha=legacy["candidate"])
    gate = FakeGate(root=root / "effects", evidence_dir=root / "artifacts")
    reviewer = FakeReviewer(root=root / "effects")
    deps = make_deps(
        state_dir=root / "state",
        root=root,
        board=board,
        worker=worker,
        gate=gate,
        reviewer=reviewer,
    )
    before = current_state(deps, legacy["thread"])
    assert before["spec_reference"] == str(root / "repo" / "card-spec.md")
    assert not Path(before["spec_reference"]).is_relative_to(
        Path(before["workspace_path"])
    )
    result = resume(
        deps,
        legacy["thread"],
        attempt,
        resolved_hold="legacy worker finished",
        payload={
            "receipt": {
                "operation_key": legacy["awaiting"]["operation_key"],
                "session_id": "offline-session",
                "outcome": "completed",
                "candidate_sha": legacy["candidate"],
                "artifact_references": [],
                "reported_tests": ["pytest -q"],
                "detail": "",
            }
        },
    )
    # Admission is skipped; the unchanged later review boundary still refuses.
    assert result.status == "held", result.state
    assert result.state["last_reason_code"] == "invalid_assignment"
    assert result.state["spec_reference"] == before["spec_reference"]
    assert request_count(worker) == 1
    assert len(gate.submissions) == 1
    assert reviewer.submissions == []
