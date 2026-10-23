"""Unit tests for factory.runner's artifact retention GC. Deterministic, no Docker."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from factory.policy import PolicyViolation
from factory.runner import run_artifact_retention


def _make_runs(root: Path, n: int):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        d = root / f"factory-run-{i:03d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "STATUS.json").write_text("{}")
        # stagger mtimes so ordering is deterministic
        ts = time.time() + i
        import os

        os.utime(d, (ts, ts))
    return root


def test_keeps_newest_n_and_deletes_rest(tmp_path):
    root = _make_runs(tmp_path / "artifacts", 5)
    result = run_artifact_retention(str(root), keep=2)
    assert len(result["deleted"]) == 3
    assert len(result["kept"]) == 2
    # newest two survive (highest index = newest by our stagger)
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["factory-run-003", "factory-run-004"]


def test_nothing_to_delete_when_under_limit(tmp_path):
    root = _make_runs(tmp_path / "artifacts", 3)
    result = run_artifact_retention(str(root), keep=10)
    assert result["deleted"] == []
    assert len(result["kept"]) == 3


def test_refuses_to_delete_outside_root(tmp_path):
    # A symlink pointing outside root must never be followed/deleted.
    root = _make_runs(tmp_path / "artifacts", 1)
    outside = tmp_path / "precious"
    outside.mkdir()
    (root / "evil-link").symlink_to(outside, target_is_directory=True)
    # symlinks are skipped entirely (not counted as deletable), so this
    # should return cleanly and leave the outside dir intact.
    result = run_artifact_retention(str(root), keep=0)
    assert outside.exists()
    assert "evil-link" not in result["deleted"] and "evil-link" not in result["kept"]


def test_refuses_unsafe_root(tmp_path):
    with pytest.raises(PolicyViolation):
        run_artifact_retention(str(Path.home()), keep=1)


def test_missing_root_returns_gracefully(tmp_path):
    result = run_artifact_retention(str(tmp_path / "nope"), keep=5)
    assert result["reason"] == "no artifact root yet"
    assert result["deleted"] == []


def test_pending_human_danger_markers_never_gc(tmp_path):
    """The durable HITL fail-fast marker dir must survive even aggressive GC."""
    root = _make_runs(tmp_path / "artifacts", 5)
    pending = root / "pending-human"
    pending.mkdir()
    (pending / "factory-t-xxx-livejobs.danger").write_text("SAFETY INCIDENT\n")
    # keep=0 -> everything old would normally be deleted
    result = run_artifact_retention(str(root), keep=0)
    assert pending.exists(), "pending-human dir must never be GC'd"
    assert (pending / "factory-t-xxx-livejobs.danger").exists()
    assert "pending-human" not in result["deleted"]


def test_delete_failure_is_reported(tmp_path, monkeypatch):
    root = _make_runs(tmp_path / "artifacts", 2)
    monkeypatch.setattr("factory.runner.shutil.rmtree", lambda *args, **kwargs: None)
    with pytest.raises(PolicyViolation, match="could not delete"):
        run_artifact_retention(str(root), keep=1)
