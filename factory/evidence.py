"""Reproducible evidence manifests for factory acceptance runs.

A green command is evidence only for the exact revision, environment, and
entry point it exercised. This module records those inputs beside run logs so
reviewers can distinguish assembled evidence from an undocumented local pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path


class EvidenceError(RuntimeError):
    """Raised when a required evidence input cannot be recorded."""


def _run_git(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise EvidenceError((proc.stderr or "git command failed").strip())
    return proc.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _environment_snapshot(repo: Path, env_file: str) -> dict:
    path = repo / env_file
    if not path.is_file():
        return {"path": str(path), "present": False}
    return {"path": str(path), "present": True, "sha256": _sha256(path)}


def write_evidence_manifest(
    artifacts_dir: str | Path,
    repo_path: str | Path,
    *,
    entry_point: str,
    command: Iterable[str] | str,
    env_file: str = ".env",
    env_example: str = ".env.example",
    audit: dict | None = None,
    coverage: dict | None = None,
    datamodel: dict | None = None,
    gates_run: bool = True,
    fixture_paths: Iterable[str] = (),
    execution: dict | None = None,
) -> Path:
    """Write a manifest describing the inputs and call path of an evidence run.

    The function deliberately records hashes and a git diff rather than values
    from dotenv files, so secrets are never copied into the artifact.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise EvidenceError(f"repository does not exist: {repo}")
    output_dir = Path(artifacts_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    fixtures = []
    for relative in fixture_paths:
        path = (repo / relative).resolve()
        if not path.is_relative_to(repo):
            raise EvidenceError(
                f"fixture escapes repository root: {relative} -> {path}"
            )
        if not path.is_file():
            raise EvidenceError(f"fixture does not exist: {path}")
        fixtures.append({"path": relative, "sha256": _sha256(path)})
    manifest = {
        "git_revision": _run_git(repo, ["rev-parse", "HEAD"]),
        "git_status_porcelain": _run_git(repo, ["status", "--short"]),
        "environment": {
            "runtime_env": _environment_snapshot(repo, env_file),
            "example_env": _environment_snapshot(repo, env_example),
            "runtime_env_diff_recorded": bool(
                (repo / env_file).is_file()
            ),  # legacy key
            "runtime_env_present": bool((repo / env_file).is_file()),
        },
        "entry_point": entry_point,
        "command": command if isinstance(command, str) else list(command),
        "audit": audit or {},
        "coverage": coverage or {},
        "datamodel": datamodel or {},
        "gates_run": gates_run,
        "fixtures": fixtures,
        "execution": execution or {},
        "hermes_factory_card": os.environ.get("HERMES_KANBAN_TASK", ""),
    }
    path = output_dir / "evidence-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def assert_assembled_evidence(manifest: dict) -> None:
    """Reject manifests that cannot certify an assembled-system claim."""
    if not isinstance(manifest, dict):
        raise EvidenceError("assembled evidence must be a mapping")
    for section in ("execution", "audit", "coverage", "datamodel", "environment"):
        if not isinstance(manifest.get(section, {}), dict):
            raise EvidenceError(f"assembled evidence {section} must be a mapping")
    required = ("git_revision", "entry_point", "command")
    missing = [key for key in required if not manifest.get(key)]
    if missing:
        raise EvidenceError(f"assembled evidence missing: {', '.join(missing)}")
    if not manifest.get("environment", {}).get("example_env", {}).get("present"):
        raise EvidenceError("assembled evidence has no documented environment example")
    if not manifest.get("fixtures") and manifest.get("anti_stories"):
        raise EvidenceError("anti-story evidence requires executable fixture records")
    execution = manifest.get("execution") or {}
    if (
        execution.get("passed") is not True
        or type(execution.get("exit_code")) is not int
        or execution.get("exit_code") != 0
        or execution.get("stage") != "complete"
    ):
        raise EvidenceError(
            "assembled evidence requires successful completed execution"
        )
    if manifest.get("gates_run") is not True:
        raise EvidenceError("assembled evidence requires enabled gates")
    audit = manifest.get("audit") or {}
    if (
        audit.get("status") not in ("pass", "no_manifest")
        or audit.get("passed") is False
    ):
        raise EvidenceError(
            "assembled evidence requires a successful or inapplicable audit"
        )
    for name, skip_reason in (
        ("coverage", "no_added_python"),
        ("datamodel", "no_data_model"),
    ):
        gate = manifest.get(name) or {}
        if gate.get("status") == "skipped" and gate.get("reason") == skip_reason:
            continue
        if (
            gate.get("status") != "pass"
            or gate.get("passed") is False
            or (name == "coverage" and gate.get("passed") is not True)
        ):
            raise EvidenceError(
                f"assembled evidence requires successful {name} or an explicit permitted skip"
            )
