"""Offline matrix for the schema-v2 evidence verifier (``factory.verify``).

Every test builds a temporary git repository and a temporary artifacts
directory; ``tests/conftest.py`` blocks Docker and network subprocesses, so a
future edit cannot silently reach the host from this file.
"""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import verify as verify_cli
from factory.evidence import (
    MANIFEST_FILENAME,
    REQUIRED_ARTIFACTS,
    SIDECAR_FILENAME,
    verify_evidence,
    write_evidence_manifest,
)

LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "legacy-pilot-manifest-v1.json"
OTHER_SHA = "c" * 40


# ---------------------------------------------------------------------------
# temporary repositories and evidence directories
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


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


def _advance(repo: Path, message: str) -> str:
    """Commit a real change (git refuses an empty commit)."""
    marker = repo / "HISTORY.md"
    previous = marker.read_text() if marker.is_file() else ""
    marker.write_text(previous + f"- {message}\n")
    return _commit(repo, message)


DEFAULT_BASE_FILES = {
    ".env.example": "REQUIRE_TLS=false\n",
    "app.py": "x = 1\n",
    "tests/fixtures/legacy.json": '{"case":"real"}\n',
}
DEFAULT_CANDIDATE_FILES = {
    "app.py": "x = 2\ny = 3\n",
    "README.md": "candidate change\n",
}


def _repo(
    tmp_path: Path,
    candidate_files: dict[str, str] | None = None,
    base_files: dict[str, str] | None = None,
) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write_tree(repo, base_files or DEFAULT_BASE_FILES)
    base = _commit(repo, "base")
    _write_tree(repo, DEFAULT_CANDIDATE_FILES)
    if candidate_files:
        _write_tree(repo, candidate_files)
    candidate = _commit(repo, "candidate")
    return repo, base, candidate


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


def _passing_gates(base: str) -> dict:
    return {
        "audit": {"status": "pass", "passed": True},
        "coverage": {
            "status": "pass",
            "passed": True,
            "base": "main",
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
    gates = _passing_gates(base)
    params = {
        "entry_point": "docker compose test service",
        "command": ["docker", "compose", "up", "--build"],
        "mode": "acceptance",
        "source_start": {"sha": candidate, "status_porcelain": ""},
        "source_end": {"sha": candidate, "status_porcelain": ""},
        "base": {"ref": "main", "kind": "commit", "sha": base},
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


def _ctx(tmp_path, repo, base, candidate, artifacts) -> SimpleNamespace:
    return SimpleNamespace(
        tmp_path=tmp_path,
        repo=repo,
        base=base,
        candidate=candidate,
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# valid control
# ---------------------------------------------------------------------------


def test_control_evidence_is_certified(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    result = verify_evidence(artifacts, repo, candidate, base)

    assert result.ok is True, (result.reason, result.detail)
    assert result.reason == "ok"
    assert result.candidate == candidate
    assert result.base == base
    assert result.evidence["schema_version"] == 2
    assert result.evidence["mode"] == "acceptance"
    assert all(check["ok"] for check in result.checks)
    names = {check["name"] for check in result.checks}
    assert {
        "schema",
        "sidecar",
        "source",
        "gates",
        "engine",
        "artifacts",
        "ancestry",
    } <= names


def test_verifier_is_read_only_and_creates_no_receipt(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    def tree(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before_artifacts = tree(artifacts)
    before_refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    before_head = _git(repo, "rev-parse", "HEAD")
    before_status = _git(repo, "status", "--porcelain")

    assert verify_evidence(artifacts, repo, candidate, base).ok is True
    assert verify_evidence(artifacts, repo, candidate, base).ok is True

    assert tree(artifacts) == before_artifacts
    assert (
        _git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == before_refs
    )
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "status", "--porcelain") == before_status


# ---------------------------------------------------------------------------
# schema identity and manifest bytes
# ---------------------------------------------------------------------------


def test_legacy_pilot_manifest_reports_schema_before_missing_sidecar(tmp_path):
    """The real Task Ledger pilot manifest (v1) must fail as legacy, not digest."""
    repo, base, candidate = _repo(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    payload = LEGACY_FIXTURE.read_bytes()
    (artifacts / MANIFEST_FILENAME).write_bytes(payload)
    before = hashlib.sha256(payload).hexdigest()

    result = verify_evidence(artifacts, repo, candidate, base)

    assert result.ok is False
    assert result.reason == "legacy_schema_unsupported"
    assert not (artifacts / SIDECAR_FILENAME).exists()
    assert (
        hashlib.sha256((artifacts / MANIFEST_FILENAME).read_bytes()).hexdigest()
        == before
    )


def test_future_schema_version_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    _rewrite(artifacts, lambda m: m["schema"].update(version=3))

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "unsupported_schema_version"


def test_unknown_schema_name_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    _rewrite(artifacts, lambda m: m["schema"].update(name="something-else"))

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "unsupported_schema_name"


def test_malformed_json_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    payload = b'{"schema": '
    (artifacts / MANIFEST_FILENAME).write_bytes(payload)
    (artifacts / SIDECAR_FILENAME).write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {MANIFEST_FILENAME}\n"
    )

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "malformed_manifest"


def test_missing_sidecar_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    (artifacts / SIDECAR_FILENAME).unlink()

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "manifest_digest_missing"


def test_sidecar_digest_mismatch_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    (artifacts / SIDECAR_FILENAME).write_text(f"{'0' * 64}  {MANIFEST_FILENAME}\n")

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "manifest_digest_mismatch"


def test_boolean_is_not_accepted_where_an_integer_is_required(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    def mutate(manifest):
        manifest["execution"]["exit_code"] = True
        manifest["runtime"]["safety"]["collateral_damage"] = 0

    _rewrite(artifacts, mutate)
    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "malformed_manifest"


def test_oversized_manifest_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    _rewrite(artifacts, lambda m: m.update(padding="x" * (1024 * 1024 + 10)))

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "malformed_manifest"


def test_manifest_symlink_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    real = tmp_path / "real-manifest.json"
    real.write_bytes((artifacts / MANIFEST_FILENAME).read_bytes())
    (artifacts / MANIFEST_FILENAME).unlink()
    (artifacts / MANIFEST_FILENAME).symlink_to(real)

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "manifest_symlink"


# ---------------------------------------------------------------------------
# one-at-a-time semantic mutations (C3/C4)
# ---------------------------------------------------------------------------


def _mutate_source_sha(manifest, ctx):
    manifest["source"]["sha"] = OTHER_SHA
    manifest["git_revision"] = OTHER_SHA
    manifest["source"]["start"]["sha"] = OTHER_SHA
    manifest["source"]["end"]["sha"] = OTHER_SHA


def _mutate_source_changed(manifest, ctx):
    manifest["source"]["end"]["sha"] = OTHER_SHA


def _mutate_source_dirty(manifest, ctx):
    manifest["source"]["clean"] = False
    manifest["source"]["start"]["status_porcelain"] = " M app.py"


def _mutate_log_byte(manifest, ctx):
    target = ctx.artifacts / "compose.log"
    original = target.read_text()
    assert len(original) > 2
    replacement = "X" if original[-2] != "X" else "Y"
    target.write_text(original[:-2] + replacement + "\n")


def _mutate_log_truncated(manifest, ctx):
    (ctx.artifacts / "compose.log").write_text("")


def _mutate_artifact_removed(manifest, ctx):
    (ctx.artifacts / "compose.log").unlink()


def _mutate_artifact_undeclared(manifest, ctx):
    manifest["artifacts"] = [
        record for record in manifest["artifacts"] if record["name"] != "compose.log"
    ]


def _mutate_artifact_duplicate(manifest, ctx):
    manifest["artifacts"].append(dict(manifest["artifacts"][0]))


def _mutate_artifact_escape(manifest, ctx):
    manifest["artifacts"][0]["name"] = "../evil.log"


def _mutate_artifact_symlink(manifest, ctx):
    target = ctx.artifacts / "compose.log"
    outside = ctx.tmp_path / "outside.log"
    outside.write_text("secret\n")
    target.unlink()
    target.symlink_to(outside)
    record = next(r for r in manifest["artifacts"] if r["name"] == "compose.log")
    record.update(
        sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
        bytes=outside.stat().st_size,
    )


def _mutate_audit_unavailable(manifest, ctx):
    manifest["audit"] = {"status": "unavailable", "passed": False}


def _mutate_coverage_failed(manifest, ctx):
    manifest["coverage"]["passed"] = False


def _mutate_coverage_base_sha(manifest, ctx):
    manifest["coverage"]["base_sha"] = OTHER_SHA


def _mutate_base_sha(manifest, ctx):
    manifest["base"]["sha"] = OTHER_SHA


def _mutate_teardown(manifest, ctx):
    manifest["runtime"]["teardown"]["ok"] = False


def _mutate_teardown_skipped(manifest, ctx):
    manifest["runtime"]["teardown"]["attempted"] = False


def _mutate_safety(manifest, ctx):
    manifest["runtime"]["safety"]["postflight"] = "SAFETY_INCIDENT"
    manifest["runtime"]["safety"]["protected_changes"] = ["eidolon-v2"]


def _mutate_collateral(manifest, ctx):
    manifest["runtime"]["safety"]["unexpected_changes"] = ["other-project/db"]


def _mutate_execution(manifest, ctx):
    manifest["execution"].update(
        passed=False, exit_code=1, stage="complete_with_errors"
    )


def _mutate_engine(manifest, ctx):
    manifest["engine"]["fingerprint"] = "sha256:" + "0" * 64


def _mutate_diagnostic(manifest, ctx):
    manifest["mode"] = "diagnostic"
    manifest["gates_run"] = False


def _mutate_gates_disabled(manifest, ctx):
    manifest["gates_run"] = False


def _mutate_unpermitted_skip(manifest, ctx):
    manifest["coverage"] = {
        "status": "skipped",
        "reason": "disabled",
        "base_sha": ctx.base,
    }


def _mutate_fixture_hash(manifest, ctx):
    manifest["fixtures"][0]["sha256"] = "0" * 64


def _mutate_fixture_untracked(manifest, ctx):
    manifest["fixtures"][0]["tracked"] = False


def _mutate_fixture_missing(manifest, ctx):
    manifest["fixtures"][0]["path"] = "tests/fixtures/absent.json"


def _mutate_fixture_escape(manifest, ctx):
    manifest["fixtures"][0]["path"] = "../outside.json"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(_mutate_source_sha, "source_sha_mismatch", id="source-sha"),
        pytest.param(
            _mutate_source_changed, "source_changed_during_run", id="start-not-end"
        ),
        pytest.param(_mutate_source_dirty, "source_dirty", id="dirty-start"),
        pytest.param(
            _mutate_log_byte, "artifact_hash_mismatch:compose.log", id="log-byte"
        ),
        pytest.param(
            _mutate_log_truncated,
            "artifact_size_mismatch:compose.log",
            id="log-truncated",
        ),
        pytest.param(
            _mutate_artifact_removed, "artifact_missing:compose.log", id="log-deleted"
        ),
        pytest.param(
            _mutate_artifact_undeclared,
            "artifact_missing:compose.log",
            id="log-undeclared",
        ),
        pytest.param(
            _mutate_artifact_duplicate,
            "duplicate_artifact:up.stdout.log",
            id="log-duplicated",
        ),
        pytest.param(
            _mutate_artifact_escape,
            "artifact_path_escape:../evil.log",
            id="artifact-escape",
        ),
        pytest.param(
            _mutate_artifact_symlink,
            "artifact_path_escape:compose.log",
            id="artifact-symlink",
        ),
        pytest.param(
            _mutate_audit_unavailable, "gate_unavailable:audit", id="gate-unavailable"
        ),
        pytest.param(_mutate_coverage_failed, "gate_failed:coverage", id="gate-failed"),
        pytest.param(_mutate_coverage_base_sha, "base_mismatch", id="coverage-base"),
        pytest.param(_mutate_base_sha, "base_mismatch", id="base-sha"),
        pytest.param(_mutate_teardown, "teardown_failed", id="teardown-failed"),
        pytest.param(
            _mutate_teardown_skipped, "teardown_failed", id="teardown-skipped"
        ),
        pytest.param(_mutate_safety, "safety_incident", id="safety-incident"),
        pytest.param(_mutate_collateral, "collateral_change", id="collateral"),
        pytest.param(_mutate_execution, "execution_incomplete", id="execution"),
        pytest.param(_mutate_engine, "engine_mismatch", id="engine"),
        pytest.param(_mutate_diagnostic, "mode_not_acceptance", id="diagnostic"),
        pytest.param(_mutate_gates_disabled, "gates_disabled", id="gates-disabled"),
        pytest.param(
            _mutate_unpermitted_skip,
            "gate_not_applicable_unpermitted:coverage",
            id="unpermitted-skip",
        ),
        pytest.param(_mutate_fixture_hash, "fixture_hash_mismatch", id="fixture-hash"),
        pytest.param(
            _mutate_fixture_untracked, "fixture_untracked", id="fixture-untracked"
        ),
        pytest.param(_mutate_fixture_missing, "fixture_missing", id="fixture-missing"),
        pytest.param(_mutate_fixture_escape, "malformed_manifest", id="fixture-escape"),
    ],
)
def test_one_at_a_time_mutation_is_rejected(tmp_path, mutate, reason):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    ctx = _ctx(tmp_path, repo, base, candidate, artifacts)
    _rewrite(artifacts, lambda manifest: mutate(manifest, ctx))

    result = verify_evidence(artifacts, repo, candidate, base)

    assert result.ok is False
    assert result.reason == reason, result.detail


# ---------------------------------------------------------------------------
# repository comparison
# ---------------------------------------------------------------------------


def test_stale_candidate_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    newer = _advance(repo, "candidate moved on")

    result = verify_evidence(artifacts, repo, newer, base)
    assert result.reason == "source_sha_mismatch"

    # ... and the base the manifest recorded is the only acceptable one.
    second = _advance(repo, "base moved on")
    result = verify_evidence(artifacts, repo, candidate, second)
    assert result.reason == "base_mismatch"


def test_unknown_candidate_object_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    def point_at_unknown(manifest):
        manifest["source"]["sha"] = OTHER_SHA
        manifest["git_revision"] = OTHER_SHA
        manifest["source"]["start"]["sha"] = OTHER_SHA
        manifest["source"]["end"]["sha"] = OTHER_SHA

    _rewrite(artifacts, point_at_unknown)
    result = verify_evidence(artifacts, repo, OTHER_SHA, base)
    assert result.reason == "unknown_object"


def test_tree_object_is_not_a_commit(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    tree_sha = _git(repo, "rev-parse", f"{candidate}^{{tree}}")

    def point_at_tree(manifest):
        manifest["source"]["sha"] = tree_sha
        manifest["git_revision"] = tree_sha
        manifest["source"]["start"]["sha"] = tree_sha
        manifest["source"]["end"]["sha"] = tree_sha

    _rewrite(artifacts, point_at_tree)
    result = verify_evidence(artifacts, repo, tree_sha, base)
    assert result.reason == "not_a_commit"


def test_non_ancestor_base_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    later = _advance(repo, "later work")

    # Claim the newer commit is the base of the older candidate: reversed ancestry.
    _rewrite(artifacts, lambda m: m["base"].update(sha=later))
    _rewrite(artifacts, lambda m: m["coverage"].update(base_sha=later))

    result = verify_evidence(artifacts, repo, candidate, later)
    assert result.reason == "not_fast_forward"


def test_non_repository_path_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    result = verify_evidence(artifacts, not_a_repo, candidate, base)
    assert result.reason == "repo_invalid"


def test_invalid_argument_shapes_are_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    result = verify_evidence(artifacts, repo, candidate[:7], base)
    assert result.reason == "invalid_argument"


def test_missing_artifacts_dir_is_rejected(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    result = verify_evidence(tmp_path / "absent", repo, candidate, base)
    assert result.reason == "artifacts_dir_invalid"


# ---------------------------------------------------------------------------
# read-only Git is hardened with command-scoped options only
# ---------------------------------------------------------------------------


def test_git_reads_disable_external_textconv(tmp_path):
    """A textconv driver configured in the repo must not run during verification."""
    repo, base, candidate = _repo(
        tmp_path, candidate_files={".gitattributes": "*.py diff=sentinel\n"}
    )
    artifacts = _evidence(
        tmp_path,
        repo,
        base,
        candidate,
        coverage={
            "status": "skipped",
            "reason": "no_added_python",
            "base": "main",
            "base_sha": base,
        },
    )
    sentinel = tmp_path / "textconv-ran"
    script = tmp_path / "textconv.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\ncat\n")
    script.chmod(0o755)
    _git(repo, "config", "diff.sentinel.textconv", str(script))

    # Control: a plain `git diff` really does invoke the driver here, so the
    # assertion below is not vacuous.
    subprocess.run(
        ["git", "diff", base, candidate, "--", "app.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert sentinel.exists(), "control diff did not invoke the textconv driver"
    sentinel.unlink()

    verify_evidence(artifacts, repo, candidate, base)

    assert not sentinel.exists(), "verifier invoked an external textconv driver"


def test_git_reads_use_command_scoped_hardening(tmp_path, monkeypatch):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(
        tmp_path,
        repo,
        base,
        candidate,
        coverage={
            "status": "skipped",
            "reason": "no_added_python",
            "base": "main",
            "base_sha": base,
        },
    )
    recorded: list[list[str]] = []
    real_run = subprocess.run
    config_before = _git(repo, "config", "--local", "--list")

    def spy(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]) == "git":
            recorded.append([str(part) for part in argv])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    verify_evidence(artifacts, repo, candidate, base)

    assert recorded, "verification ran no git commands"
    for argv in recorded:
        assert "core.hooksPath=/dev/null" in argv
        assert "core.fsmonitor=false" in argv
        assert "--no-optional-locks" in argv
    diffs = [argv for argv in recorded if "diff" in argv]
    assert diffs, "the coverage claim must have triggered a diff"
    for argv in diffs:
        assert "--no-textconv" in argv
        assert "--no-ext-diff" in argv
    # No persistent configuration was written by the verifier.
    assert _git(repo, "config", "--local", "--list") == config_before


# ---------------------------------------------------------------------------
# gate inapplicability must be substantiated from candidate/base content
# ---------------------------------------------------------------------------


def test_coverage_skip_is_unsubstantiated_when_python_was_added(tmp_path):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(
        tmp_path,
        repo,
        base,
        candidate,
        coverage={
            "status": "skipped",
            "reason": "no_added_python",
            "base": "main",
            "base_sha": base,
        },
    )

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "gate_inapplicable_unsubstantiated:coverage"


def test_datamodel_skip_is_unsubstantiated_when_the_candidate_has_one(tmp_path):
    repo, base, candidate = _repo(
        tmp_path, candidate_files={"data-model.yml": "entities: {}\n"}
    )
    artifacts = _evidence(tmp_path, repo, base, candidate)

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "gate_inapplicable_unsubstantiated:datamodel"


def test_audit_no_manifest_is_unsubstantiated_when_a_manifest_exists(tmp_path):
    repo, base, candidate = _repo(
        tmp_path, candidate_files={"requirements.txt": "requests==2.0.0\n"}
    )
    artifacts = _evidence(
        tmp_path, repo, base, candidate, audit={"status": "no_manifest"}
    )

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.reason == "gate_inapplicable_unsubstantiated:audit"


def test_substantiated_inapplicable_gates_certify(tmp_path):
    """A doc-only diff really does justify the documented skips."""
    repo, base, candidate = _repo(
        tmp_path,
        candidate_files={"app.py": "x = 1\n", "README.md": "candidate change\n"},
    )
    artifacts = _evidence(
        tmp_path,
        repo,
        base,
        candidate,
        audit={"status": "no_manifest"},
        coverage={
            "status": "skipped",
            "reason": "no_added_python",
            "base": "main",
            "base_sha": base,
        },
        datamodel={"status": "skipped", "reason": "no_data_model"},
    )

    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.ok is True, (result.reason, result.detail)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*args: str) -> int:
    return verify_cli.main(list(args))


def test_cli_exit_zero_on_certified_with_json(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    rc = _cli(
        "--artifacts-dir",
        str(artifacts),
        "--repo",
        str(repo),
        "--candidate",
        candidate,
        "--base",
        base,
        "--json",
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["reason"] == "ok"
    assert payload["evidence"]["schema_version"] == 2


def test_cli_exit_one_on_rejection_with_named_reason(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    _rewrite(artifacts, lambda m: m["audit"].update(status="unavailable"))

    rc = _cli(
        "--artifacts-dir",
        str(artifacts),
        "--repo",
        str(repo),
        "--candidate",
        candidate,
        "--base",
        base,
        "--json",
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "gate_unavailable:audit"


def test_cli_human_output_names_the_verdict(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    rc = _cli(
        "--artifacts-dir",
        str(artifacts),
        "--repo",
        str(repo),
        "--candidate",
        candidate,
        "--base",
        base,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "CERTIFIED" in out
    assert candidate in out


def test_cli_exit_two_on_usage_errors(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)

    rc = _cli(
        "--artifacts-dir",
        str(artifacts),
        "--repo",
        str(repo),
        "--candidate",
        "not-a-sha",
        "--base",
        base,
    )
    assert rc == 2
    assert "40-hex" in capsys.readouterr().err

    with pytest.raises(SystemExit) as excinfo:
        _cli("--artifacts-dir", str(artifacts))
    assert excinfo.value.code == 2


def test_cli_legacy_manifest_exits_one_with_legacy_reason(tmp_path, capsys):
    repo, base, candidate = _repo(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / MANIFEST_FILENAME).write_bytes(LEGACY_FIXTURE.read_bytes())

    rc = _cli(
        "--artifacts-dir",
        str(artifacts),
        "--repo",
        str(repo),
        "--candidate",
        candidate,
        "--base",
        base,
        "--json",
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "legacy_schema_unsupported"
