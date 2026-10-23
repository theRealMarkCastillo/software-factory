"""Offline matrix for sanctioned local promotion (``factory-promote``).

Every test builds a disposable temporary git repository and artifacts
directory. ``tests/conftest.py`` denies Docker and network subprocesses at both
``run`` and ``Popen`` entry points (including ``finally`` paths), so a future
edit cannot silently reach the host from this file.

The positive tests exercise real Git: the ref update is a real
``update-ref --no-deref`` compare-and-swap against a temporary repository, and
"zero mutation" is asserted by re-reading refs, the index bytes, the config
bytes and the worktree tree hash.
"""

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from factory import promote as promote_cli
from factory.evidence import (
    MANIFEST_FILENAME,
    REQUIRED_ARTIFACTS,
    SIDECAR_FILENAME,
    write_evidence_manifest,
)

# ---------------------------------------------------------------------------
# disposable git repositories
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit",
        "-qm",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


BASE_FILES = {
    ".env.example": "REQUIRE_TLS=false\n",
    "app.py": "x = 1\n",
    "tests/fixtures/legacy.json": '{"case":"real"}\n',
}
CANDIDATE_FILES = {
    "app.py": "x = 2\ny = 3\n",
    "README.md": "candidate change\n",
}


def _repo(tmp_path: Path, name: str = "repo") -> tuple[Path, str, str]:
    """A repository whose ``main`` holds the candidate and ``release`` the base."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write_tree(repo, BASE_FILES)
    base = _commit(repo, "base")
    _write_tree(repo, CANDIDATE_FILES)
    candidate = _commit(repo, "candidate")
    return repo, base, candidate


def _branch(repo: Path, name: str, at: str) -> None:
    _git(repo, "branch", name, at)


# ---------------------------------------------------------------------------
# schema-v2 evidence written through the real producer
# ---------------------------------------------------------------------------


def _clean_runtime() -> dict:
    return {
        "teardown": {"attempted": True, "ok": True, "detail": ""},
        "safety": {
            "postflight": "clean",
            "protected_changes": [],
            "unexpected_changes": [],
            "collateral_damage": False,
        },
    }


def _gates(base: str) -> dict:
    return {
        "audit": {"status": "pass", "passed": True},
        "coverage": {
            "status": "pass",
            "passed": True,
            "base": "release",
            "base_sha": base,
            "base_kind": "commit",
        },
        "datamodel": {"status": "skipped", "reason": "no_data_model"},
    }


def _evidence(tmp_path: Path, repo: Path, base: str, candidate: str, **overrides):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    for name in REQUIRED_ARTIFACTS:
        (artifacts / name).write_text(f"{name}: ran\n")
    gates = _gates(base)
    params = {
        "entry_point": "docker compose test service",
        "command": ["docker", "compose", "up", "--build"],
        "mode": "acceptance",
        "source_start": {"sha": candidate, "status_porcelain": ""},
        "source_end": {"sha": candidate, "status_porcelain": ""},
        "base": {"ref": "release", "kind": "commit", "sha": base},
        "runtime": _clean_runtime(),
        "artifacts": list(REQUIRED_ARTIFACTS),
        "audit": gates["audit"],
        "coverage": gates["coverage"],
        "datamodel": gates["datamodel"],
        "execution": {
            "exit_code": 0,
            "passed": True,
            "stage": "complete",
            "duration_s": 1.5,
        },
        "fixture_paths": ["tests/fixtures/legacy.json"],
        "gates_run": True,
    }
    params.update(overrides)
    write_evidence_manifest(artifacts, repo, **params)
    return artifacts


def _rewrite(artifacts: Path, mutate) -> dict:
    """Apply a semantic mutation and refresh the sidecar (not just the digest)."""
    path = artifacts / MANIFEST_FILENAME
    manifest = json.loads(path.read_text())
    mutate(manifest)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    path.write_text(payload)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    (artifacts / SIDECAR_FILENAME).write_text(f"{digest}  {MANIFEST_FILENAME}\n")
    return manifest


# ---------------------------------------------------------------------------
# observational snapshots
# ---------------------------------------------------------------------------


def _refs(repo: Path) -> dict[str, str]:
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1) for line in out.splitlines())


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return "<absent>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _worktree_hash(repo: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(repo.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        digest.update(str(path.relative_to(repo)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _snapshot(repo: Path) -> dict:
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = repo / common
    return {
        "refs": _refs(repo),
        "head": _git(repo, "rev-parse", "HEAD"),
        "status": _git(repo, "status", "--porcelain"),
        "index": _file_hash(common / "index"),
        "config": _file_hash(common / "config"),
        "worktree": _worktree_hash(repo),
        "lock": (common / "factory-promote.lock").exists(),
    }


def _run(*args: str) -> int:
    return promote_cli.main(list(args))


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _promote(
    repo: Path, artifacts: Path, base: str, candidate: str, *extra: str
) -> int:
    return _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(artifacts),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        "refs/heads/release",
        "--json",
        *extra,
    )


# ---------------------------------------------------------------------------
# slice 1: one real fast-forward, refusals that touch nothing
# ---------------------------------------------------------------------------


def test_valid_evidence_advances_only_the_named_ref(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 0

    result = _json_out(capsys)
    assert result["promoted"] is True
    assert result["dry_run"] is False
    assert result["target"] == "refs/heads/release"
    assert result["before"] == base
    assert result["after"] == candidate
    assert result["candidate"] == candidate
    assert result["base"] == base
    assert result["reason"] == "ok"
    assert result["verification"]["reason"] == "ok"

    after = _snapshot(repo)
    assert after["refs"]["refs/heads/release"] == candidate
    assert after["refs"]["refs/heads/main"] == before["refs"]["refs/heads/main"]
    changed = {
        name
        for name in set(before["refs"]) | set(after["refs"])
        if before["refs"].get(name) != after["refs"].get(name)
    }
    assert changed == {"refs/heads/release"}
    assert after["index"] == before["index"]
    assert after["config"] == before["config"]
    assert after["worktree"] == before["worktree"]
    assert after["head"] == before["head"]
    assert after["status"] == ""
    assert after["lock"] is False
    assert not list((repo / ".git").glob("**/*.lock"))


def test_dry_run_performs_every_check_and_moves_nothing(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate, "--dry-run") == 0

    result = _json_out(capsys)
    assert result["promoted"] is False
    assert result["dry_run"] is True
    assert result["reason"] == "ok"
    assert result["before"] == base
    assert result["after"] == base
    assert result["verification"]["reason"] == "ok"

    after = _snapshot(repo)
    # The scoped coordination lock is the only temporary side effect; it is gone.
    assert after == before


@pytest.mark.parametrize(
    "target",
    [
        "release",
        "refs/tags/v1",
        "HEAD",
        "refs/heads/../escape",
        "refs/heads/",
        "refs/heads/bad name",
    ],
)
def test_target_must_be_a_wellformed_full_branch_ref(tmp_path, capsys, target):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(artifacts),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        target,
        "--json",
    )

    assert rc == 2
    assert "error" in capsys.readouterr().err
    assert _snapshot(repo) == before


@pytest.mark.parametrize("which", ["candidate", "base"])
def test_shas_must_be_full_40_hex(tmp_path, capsys, which):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    values = {"candidate": candidate, "base": base}
    values[which] = candidate[:12]
    before = _snapshot(repo)

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
        "--candidate",
        values["candidate"],
        "--base",
        values["base"],
        "--target",
        "refs/heads/release",
        "--json",
    )

    assert rc == 2
    assert _snapshot(repo) == before


def test_missing_target_branch_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(artifacts),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        "refs/heads/absent",
        "--json",
    )

    assert rc == 1
    assert _json_out(capsys)["reason"] == "target_missing"
    assert _snapshot(repo) == before


def test_symbolic_target_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    _git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/release")
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(artifacts),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        "refs/heads/alias",
        "--json",
    )

    assert rc == 1
    assert _json_out(capsys)["reason"] == "target_symbolic"
    assert _snapshot(repo) == before


def test_target_moved_from_the_expected_base_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    _git(repo, "update-ref", "refs/heads/release", candidate)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    result = _json_out(capsys)
    assert result["reason"] == "target_moved"
    assert _snapshot(repo) == before


def test_non_ancestor_candidate_is_rejected(tmp_path, capsys):
    repo, base, _candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    _git(repo, "rm", "-rq", "--cached", ".")
    for path in repo.iterdir():
        if path.name != ".git":
            path.unlink() if path.is_file() else None
    _write_tree(repo, {"only.py": "z = 1\n"})
    orphan = _commit(repo, "unrelated root")
    artifacts = _evidence(tmp_path, repo, base, orphan)
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, orphan) == 1

    assert _json_out(capsys)["reason"] == "not_fast_forward"
    assert _snapshot(repo) == before


def test_candidate_must_be_a_branch_tip(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    _write_tree(repo, {"scratch.txt": "dangling\n"})
    dangling = _commit(repo, "dangling candidate")
    _git(repo, "reset", "-q", "--hard", candidate)
    artifacts = _evidence(tmp_path, repo, base, dangling)
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, dangling) == 1

    assert _json_out(capsys)["reason"] == "candidate_not_a_branch_tip"
    assert _snapshot(repo) == before


def test_candidate_must_be_a_commit(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    tree = _git(repo, "rev-parse", f"{candidate}^{{tree}}")
    before = _snapshot(repo)

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
        "--candidate",
        tree,
        "--base",
        base,
        "--target",
        "refs/heads/release",
        "--json",
    )

    assert rc == 1
    assert _json_out(capsys)["reason"] == "not_a_commit"
    assert _snapshot(repo) == before


@pytest.mark.parametrize("kind", ["modified", "untracked"])
def test_dirty_promoting_worktree_is_rejected(tmp_path, capsys, kind):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    if kind == "modified":
        (repo / "app.py").write_text("x = 99\n")
    else:
        (repo / "untracked.txt").write_text("scratch\n")
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    result = _json_out(capsys)
    assert result["reason"] == "worktree_dirty"
    assert "app.py" in result["detail"] or "untracked.txt" in result["detail"]
    assert _snapshot(repo) == before


def test_target_checked_out_in_another_worktree_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "release")
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)
    linked_before = _snapshot(linked)

    assert _promote(repo, artifacts, base, candidate) == 1

    result = _json_out(capsys)
    assert result["reason"] == "target_checked_out"
    assert str(linked) in result["detail"]
    assert _snapshot(repo) == before
    assert _snapshot(linked) == linked_before


def test_target_checked_out_in_the_promoting_worktree_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(artifacts),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        "refs/heads/main",
        "--json",
    )

    assert rc == 1
    assert _json_out(capsys)["reason"] == "target_checked_out"
    assert _snapshot(repo) == before


def test_non_repository_path_is_rejected(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    rc = _run(
        "--repo",
        str(plain),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
        "--candidate",
        "a" * 40,
        "--base",
        "b" * 40,
        "--target",
        "refs/heads/release",
        "--json",
    )

    assert rc == 1
    assert _json_out(capsys)["reason"] == "repo_invalid"


# ---------------------------------------------------------------------------
# slice 2: promotion consumes fresh in-process verification
# ---------------------------------------------------------------------------


def test_missing_evidence_is_rejected_without_mutation(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    empty = tmp_path / "empty-artifacts"
    empty.mkdir()
    before = _snapshot(repo)

    assert _promote(repo, empty, base, candidate) == 1

    result = _json_out(capsys)
    assert result["reason"] == "evidence_rejected:manifest_missing"
    assert result["before"] == base
    assert result["after"] == base
    assert _snapshot(repo) == before


def test_tampered_log_is_rejected_without_mutation(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    log = artifacts / "compose.log"
    payload = bytearray(log.read_bytes())
    payload[0] = ord("X") if payload[0] != ord("X") else ord("Y")
    log.write_bytes(bytes(payload))
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    assert (
        _json_out(capsys)["reason"]
        == "evidence_rejected:artifact_hash_mismatch:compose.log"
    )
    assert _snapshot(repo) == before


def test_diagnostic_evidence_is_rejected_without_mutation(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(
        tmp_path, repo, base, candidate, mode="diagnostic", gates_run=False
    )
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    assert _json_out(capsys)["reason"] == "evidence_rejected:mode_not_acceptance"
    assert _snapshot(repo) == before


def test_evidence_for_another_candidate_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    stale = _evidence(tmp_path, repo, base, base)
    before = _snapshot(repo)

    assert _promote(repo, stale, base, candidate) == 1

    assert _json_out(capsys)["reason"] == "evidence_rejected:source_sha_mismatch"
    assert _snapshot(repo) == before


def test_evidence_with_a_different_base_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    _rewrite(artifacts, lambda manifest: manifest["base"].__setitem__("sha", "d" * 40))
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    assert _json_out(capsys)["reason"] == "evidence_rejected:base_mismatch"
    assert _snapshot(repo) == before


def test_manifest_edit_without_sidecar_refresh_is_rejected(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    manifest_path = artifacts / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text())
    manifest["card_id"] = "forged"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    assert _json_out(capsys)["reason"] == "evidence_rejected:manifest_digest_mismatch"
    assert _snapshot(repo) == before


def test_legacy_manifest_cannot_promote(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    legacy = Path(__file__).parent / "fixtures" / "legacy-pilot-manifest-v1.json"
    (artifacts / MANIFEST_FILENAME).write_text(legacy.read_text())
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    assert _json_out(capsys)["reason"] == "evidence_rejected:legacy_schema_unsupported"
    assert _snapshot(repo) == before


def test_evidence_rewritten_after_the_first_verification_is_rejected(
    tmp_path, capsys, monkeypatch
):
    """Promotion must re-verify immediately before acting, not once and trust it."""
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    before = _snapshot(repo)
    real_verify = promote_cli.verify_evidence
    calls = {"n": 0}

    def tampering_verify(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            log = artifacts / "compose.log"
            payload = bytearray(log.read_bytes())
            payload[0] = ord("X") if payload[0] != ord("X") else ord("Y")
            log.write_bytes(bytes(payload))
        return result

    monkeypatch.setattr(promote_cli, "verify_evidence", tampering_verify)

    assert _promote(repo, artifacts, base, candidate) == 1

    assert calls["n"] == 2, "evidence must be verified again immediately before CAS"
    assert (
        _json_out(capsys)["reason"]
        == "evidence_rejected:artifact_hash_mismatch:compose.log"
    )
    assert _snapshot(repo) == before


def test_worktree_turning_dirty_after_verification_is_rejected(
    tmp_path, capsys, monkeypatch
):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    real_verify = promote_cli.verify_evidence
    calls = {"n": 0}

    def dirtying_verify(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            (repo / "late.txt").write_text("appeared during promotion\n")
        return result

    monkeypatch.setattr(promote_cli, "verify_evidence", dirtying_verify)
    refs_before = _refs(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    result = _json_out(capsys)
    assert result["reason"] == "worktree_dirty"
    assert "late.txt" in result["detail"]
    assert _refs(repo) == refs_before


# ---------------------------------------------------------------------------
# slice 3: scoped coordination lock (never stolen, only own lock released)
# ---------------------------------------------------------------------------


def _common_dir(repo: Path) -> Path:
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    return common if common.is_absolute() else (repo / common)


def test_existing_lock_fails_closed_even_when_it_looks_stale(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    lock = _common_dir(repo) / "factory-promote.lock"
    lock.write_text("pid=1 started_at=1970-01-01T00:00:00Z\n")
    stale = 946684800  # 2000-01-01, decades old
    os.utime(lock, (stale, stale))
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    result = _json_out(capsys)
    assert result["reason"] == "lock_busy"
    assert "factory-promote.lock" in result["detail"]
    assert _snapshot(repo) == before
    assert lock.read_text() == "pid=1 started_at=1970-01-01T00:00:00Z\n"


def test_lock_in_the_common_dir_of_a_linked_worktree_is_honoured(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked), base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    lock = _common_dir(repo) / "factory-promote.lock"
    lock.write_text("pid=999999 started_at=2000-01-01T00:00:00Z\n")
    refs_before = _refs(repo)

    rc = _run(
        "--repo",
        str(linked),
        "--artifacts-dir",
        str(artifacts),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        "refs/heads/release",
        "--json",
    )

    assert rc == 1
    assert _json_out(capsys)["reason"] == "lock_busy"
    assert _refs(repo) == refs_before
    assert lock.exists()


def test_lock_is_released_after_a_successful_promotion(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    lock = _common_dir(repo) / "factory-promote.lock"
    seen = {}

    real_update = promote_cli._update_ref

    def observing_update(*args, **kwargs):
        seen["lock_during_update"] = lock.exists()
        return real_update(*args, **kwargs)

    promote_cli._update_ref = observing_update
    try:
        assert _promote(repo, artifacts, base, candidate) == 0
    finally:
        promote_cli._update_ref = real_update

    assert seen["lock_during_update"] is True, "the CAS must run under our lock"
    assert lock.exists() is False


def test_lock_is_released_after_a_rejected_promotion(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    _git(repo, "update-ref", "refs/heads/release", candidate)
    lock = _common_dir(repo) / "factory-promote.lock"

    assert _promote(repo, artifacts, base, candidate) == 1

    assert _json_out(capsys)["reason"] == "target_moved"
    assert lock.exists() is False


def test_a_lock_created_by_someone_else_during_the_run_is_not_deleted(tmp_path, capsys):
    """Release only the lock this invocation acquired, identified by inode."""
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    lock = _common_dir(repo) / "factory-promote.lock"
    foreign = "pid=4242 started_at=2026-01-01T00:00:00Z\n"
    real_update = promote_cli._update_ref

    def replacing_update(*args, **kwargs):
        lock.unlink()
        lock.write_text(foreign)
        return real_update(*args, **kwargs)

    promote_cli._update_ref = replacing_update
    try:
        assert _promote(repo, artifacts, base, candidate) == 0
    finally:
        promote_cli._update_ref = real_update

    assert lock.read_text() == foreign


# ---------------------------------------------------------------------------
# slice 4: hooks suppressed, real CAS, post-update read-back
# ---------------------------------------------------------------------------


HOOK_NAMES = (
    "reference-transaction",
    "post-receive",
    "pre-push",
    "post-merge",
    "post-checkout",
)


def _install_hooks(repo: Path, sentinel: Path) -> None:
    hooks = _common_dir(repo) / "hooks"
    hooks.mkdir(exist_ok=True)
    script = f"#!/bin/sh\nprintf '%s\\n' \"$0\" >> {sentinel}\nexit 1\n"
    for name in HOOK_NAMES:
        hook = hooks / name
        hook.write_text(script)
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_reference_transaction_hook_cannot_observe_or_veto_promotion(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    sentinel = tmp_path / "hook-fired.txt"
    _install_hooks(repo, sentinel)

    # Characterisation first: the sentinel is genuinely live in this repository.
    # A raw update-ref (no command-scoped hardening) is vetoed and recorded,
    # which proves the passing assertion below is not vacuous.
    raw = subprocess.run(
        ["git", "update-ref", "refs/heads/release", candidate],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert raw.returncode != 0
    assert sentinel.exists()
    observed = sentinel.read_text().splitlines()
    assert len(observed) >= 1
    sentinel.unlink()

    before = _snapshot(repo)
    assert _promote(repo, artifacts, base, candidate) == 0

    result = _json_out(capsys)
    assert result["promoted"] is True
    assert sentinel.exists() is False, "no repository hook may run for promotion"
    after = _snapshot(repo)
    assert after["refs"]["refs/heads/release"] == candidate
    assert after["index"] == before["index"]
    assert after["config"] == before["config"]
    assert after["worktree"] == before["worktree"]
    assert after["status"] == ""

    config = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert config.returncode != 0, "persistent config must not be modified"


def test_concurrent_ref_writer_wins_the_real_compare_and_swap(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    tree = _git(repo, "rev-parse", f"{base}^{{tree}}")
    racer = _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        "concurrent writer",
    )
    before = _snapshot(repo)
    real_update = promote_cli._update_ref

    def racing_update(*args, **kwargs):
        _git(repo, "update-ref", "refs/heads/release", racer)
        return real_update(*args, **kwargs)

    promote_cli._update_ref = racing_update
    try:
        assert _promote(repo, artifacts, base, candidate) == 1
    finally:
        promote_cli._update_ref = real_update

    result = _json_out(capsys)
    assert result["reason"] == "ref_update_failed"
    assert candidate in result["detail"] or "update-ref" in result["detail"]
    after = _snapshot(repo)
    assert after["refs"]["refs/heads/release"] == racer, (
        "the CAS was real git, not a mock"
    )
    assert after["refs"]["refs/heads/main"] == before["refs"]["refs/heads/main"]
    assert after["index"] == before["index"]
    assert after["config"] == before["config"]
    assert after["worktree"] == before["worktree"]


def test_target_is_read_back_after_the_update(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    real_update = promote_cli._update_ref

    def reverting_update(*args, **kwargs):
        proc = real_update(*args, **kwargs)
        _git(repo, "update-ref", "refs/heads/release", base)
        return proc

    promote_cli._update_ref = reverting_update
    try:
        assert _promote(repo, artifacts, base, candidate) == 1
    finally:
        promote_cli._update_ref = real_update

    result = _json_out(capsys)
    assert result["reason"] == "post_update_mismatch"
    assert result["promoted"] is False
    assert result["after"] == base, "after reports the value actually read back"
    assert candidate in result["detail"]
    assert _refs(repo)["refs/heads/release"] == base


def test_promotion_runs_no_remote_or_merge_command(tmp_path, capsys, monkeypatch):
    """No push/fetch/ls-remote/clone may appear on any subprocess path."""
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    real_run = subprocess.run
    seen: list[list[str]] = []

    def recording_run(argv, *args, **kwargs):
        command = list(argv) if not isinstance(argv, str) else [argv]
        seen.append(command)
        banned = {
            "push",
            "fetch",
            "ls-remote",
            "clone",
            "pull",
            "merge",
            "remote",
            "tag",
            "receive-pack",
        }
        if command[:1] == ["git"] and banned & set(command):
            raise AssertionError(f"promotion invoked git {' '.join(command)}")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    try:
        assert _promote(repo, artifacts, base, candidate) == 0
    finally:
        monkeypatch.setattr(subprocess, "run", real_run)

    git_calls = [command for command in seen if command[:1] == ["git"]]
    assert git_calls, "promotion must actually use git"
    assert all("update-ref" not in c or "refs/heads/release" in c for c in git_calls)


# ---------------------------------------------------------------------------
# slice 5: CLI surface has no escape hatches
# ---------------------------------------------------------------------------


BASE_ARGS = [
    "--repo",
    ".",
    "--artifacts-dir",
    ".",
    "--candidate",
    "a" * 40,
    "--base",
    "b" * 40,
    "--target",
    "refs/heads/release",
]


@pytest.mark.parametrize(
    "flag", ["--force", "--skip-gate", "--allow-legacy", "--push", "--tag", "--mode"]
)
def test_no_escape_hatch_flags_are_accepted(flag):
    with pytest.raises(SystemExit) as excinfo:
        promote_cli.build_parser().parse_args([*BASE_ARGS, flag])
    assert excinfo.value.code == 2


def test_human_output_names_the_promotion(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(artifacts),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        "refs/heads/release",
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "PROMOTED refs/heads/release" in out
    assert base in out and candidate in out
    assert "verification: ok" in out


def test_human_output_names_a_rejection(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    empty = tmp_path / "no-evidence"
    empty.mkdir()

    rc = _run(
        "--repo",
        str(repo),
        "--artifacts-dir",
        str(empty),
        "--candidate",
        candidate,
        "--base",
        base,
        "--target",
        "refs/heads/release",
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "REJECTED evidence_rejected:manifest_missing" in out


def test_fsmonitor_program_is_not_executed(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    marker = tmp_path / "fsmonitor-ran.txt"
    fsmonitor = tmp_path / "fsmonitor.sh"
    fsmonitor.write_text(f"#!/bin/sh\nprintf ran >> {marker}\nprintf '\\n'\n")
    fsmonitor.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(fsmonitor))
    before = _snapshot(repo)  # unhardened git here *would* run fsmonitor
    if marker.exists():
        marker.unlink()  # only the promotion run counts

    assert _promote(repo, artifacts, base, candidate) == 0

    assert marker.exists() is False, "promotion must not execute the fsmonitor hook"
    after = _snapshot(repo)
    assert after["config"] == before["config"]
    assert after["index"] == before["index"]
    assert after["refs"]["refs/heads/release"] == candidate


# ---------------------------------------------------------------------------
# strict manifest parsing: promotion consumes a malformed_manifest rejection
# ---------------------------------------------------------------------------
#
# The sidecar is refreshed with the mutated bytes, so the refusal is semantic:
# it comes from the parser, not from a stale digest.

DEEP_MANIFEST_PAYLOAD = b"[" * 200_000 + b"]" * 200_000


def _raw_manifest(artifacts: Path, payload: bytes) -> None:
    (artifacts / MANIFEST_FILENAME).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (artifacts / SIDECAR_FILENAME).write_text(f"{digest}  {MANIFEST_FILENAME}\n")


def _duplicate_mode_payload(artifacts: Path) -> bytes:
    """Insert ``"mode": "diagnostic"`` before ``"mode": "acceptance"``.

    A last-wins reader would keep ``acceptance`` and certify the manifest; the
    strict parser must refuse the ambiguous document outright.
    """
    lines = (artifacts / MANIFEST_FILENAME).read_text().splitlines()
    for index, line in enumerate(lines):
        if line.strip() == '"mode": "acceptance",':
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(index, f'{indent}"mode": "diagnostic",')
            return ("\n".join(lines) + "\n").encode()
    raise AssertionError("no top-level mode member found")


@pytest.mark.parametrize("case", ["duplicate-mode", "deep-array"])
def test_malformed_manifest_is_rejected_without_any_mutation(tmp_path, capsys, case):
    repo, base, candidate = _repo(tmp_path)
    _branch(repo, "release", base)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    payload = (
        _duplicate_mode_payload(artifacts)
        if case == "duplicate-mode"
        else DEEP_MANIFEST_PAYLOAD
    )
    _raw_manifest(artifacts, payload)
    before = _snapshot(repo)

    assert _promote(repo, artifacts, base, candidate) == 1

    result = _json_out(capsys)
    assert result["reason"] == "evidence_rejected:malformed_manifest"
    assert result["verification"]["reason"] == "malformed_manifest"
    assert result["promoted"] is False
    assert result["after"] == base
    assert _snapshot(repo) == before
