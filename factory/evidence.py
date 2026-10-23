"""Revision-bound evidence manifests for factory acceptance runs.

A green command is evidence only for the exact revision, environment, and
entry point it exercised. This module records those inputs beside run logs so
reviewers can distinguish assembled evidence from an undocumented local pass.

Two readers live here and they are deliberately different:

``assert_assembled_evidence``
    The historical (schema v1) reader. It keeps its prior accept/reject
    semantics for the flat dicts older artifacts contain, expressed over the
    shared gate policy. It certifies nothing about schema identity, mode,
    source cleanliness, teardown, safety, or artifact hashes.

``check_evidence`` / ``verify_evidence``
    The schema v2 checker and verifier. ``check_evidence`` is the single
    code-owned acceptance policy: schema identity, acceptance mode, clean and
    unchanged source, completed execution, clean teardown and postflight, the
    gate policy table, engine identity, and hashed artifacts. ``verify_evidence``
    adds the read-only repository comparison (candidate/base objects, ancestry,
    fixture re-hashing from the candidate tree, gate-inapplicability
    substantiation). It writes no receipt and mutates no file or ref.

Trust boundary (stated, not hidden):

* Integrity is not same-UID attestation. An actor who can write the artifacts
  directory can produce a self-consistent manifest, sidecar, and logs. Closing
  that needs a separate trust authority (signing key held outside the UID); no
  such claim is made here.
* Clean start/end snapshots do not prove no transient edit was reverted, nor do
  they bind ignored files, mutable container images, dependencies, or the
  inherited environment.
* The engine fingerprint binds evidence to the engine build that produced it;
  it does not prove that build was the installed release.
* Raw Git run outside the sanctioned commands is not intercepted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_NAME = "factory-evidence"
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({2})
MANIFEST_FILENAME = "evidence-manifest.json"
SIDECAR_FILENAME = "evidence-manifest.sha256"
MANIFEST_MAX_BYTES = 1024 * 1024
SIDECAR_MAX_BYTES = 4096
REQUIRED_ARTIFACTS = ("up.stdout.log", "up.stderr.log", "compose.log")
MODES = ("acceptance", "diagnostic")
ENGINE_NAME = "hermes-software-factory"
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Gate acceptance policy: ONE code-owned table, consumed by the runner's
#: self-check and by the verifier. Never read from a manifest, never overridable
#: from a CLI. ``required`` fields must match exactly; ``inapplicable`` maps a
#: permitted "inapplicable" signature to the justification the verifier must
#: substantiate against candidate/base content before accepting it.
GATE_POLICY: dict[str, dict] = {
    "audit": {
        "required": {"status": "pass"},
        "inapplicable": {
            "no_manifest": "no supported dependency manifest in the subject"
        },
    },
    "coverage": {
        "required": {"status": "pass", "passed": True},
        "inapplicable": {
            "skipped:no_added_python": "diff vs base adds no python lines"
        },
    },
    "datamodel": {
        "required": {"status": "pass"},
        "inapplicable": {"skipped:no_data_model": "subject has no data-model.yml"},
    },
}

#: Closed reason-code set. Gate and artifact reasons carry a ``:<gate>`` /
#: ``:<name>`` suffix on the same family prefix, e.g.
#: ``gate_unavailable:audit`` or ``artifact_hash_mismatch:compose.log``.
REASON_CODES = frozenset(
    {
        "ok",
        # inputs
        "invalid_argument",
        "artifacts_dir_invalid",
        "repo_invalid",
        # manifest bytes
        "manifest_missing",
        "manifest_symlink",
        "sidecar_symlink",
        "malformed_manifest",
        "manifest_digest_missing",
        "manifest_digest_mismatch",
        # schema
        "legacy_schema_unsupported",
        "unsupported_schema_name",
        "unsupported_schema_version",
        # acceptance semantics
        "mode_not_acceptance",
        "gates_disabled",
        "source_dirty",
        "source_changed_during_run",
        "source_sha_mismatch",
        "base_mismatch",
        "execution_incomplete",
        "teardown_failed",
        "safety_incident",
        "collateral_change",
        # gates
        "gate_missing",
        "gate_failed",
        "gate_unavailable",
        "gate_not_applicable_unpermitted",
        "gate_inapplicable_unsubstantiated",
        # engine
        "engine_mismatch",
        # repository objects
        "unknown_object",
        "not_a_commit",
        "not_fast_forward",
        # fixtures
        "fixture_untracked",
        "fixture_missing",
        "fixture_hash_mismatch",
        # artifacts
        "artifact_path_escape",
        "duplicate_artifact",
        "artifact_missing",
        "artifact_size_mismatch",
        "artifact_hash_mismatch",
    }
)

#: Read-only Git is hardened with command-scoped options only; no persistent
#: configuration is ever written. ``core.hooksPath=/dev/null`` neutralizes
#: repository hooks, ``core.fsmonitor=false`` disables fsmonitor execution, and
#: ``--no-optional-locks`` keeps status probes from refreshing the index.
GIT_SAFETY_ARGUMENTS = (
    "--no-optional-locks",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
)

#: Root filenames the audit gate treats as dependency manifests.
DEPENDENCY_MANIFESTS = (
    "requirements.txt",
    "Pipfile.lock",
    "poetry.lock",
    "pyproject.toml",
    "package-lock.json",
    "package.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)

DATA_MODEL_FILENAME = "data-model.yml"

_STATUS_FAILED = frozenset({"fail", "error"})
_STATUS_UNAVAILABLE = frozenset({"unavailable", "unsupported", "disabled"})


class EvidenceError(RuntimeError):
    """Raised when a required evidence input cannot be recorded."""


class _Malformed(Exception):
    """Internal: a manifest field failed structural validation."""


# ---------------------------------------------------------------------------
# hashing and git plumbing
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_argv(args: Iterable[str]) -> list[str]:
    return ["git", *GIT_SAFETY_ARGUMENTS, *args]


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run a read-only git command; never raises on a non-policy condition."""
    try:
        return subprocess.run(
            _git_argv(args),
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - host without git
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired:  # pragma: no cover - pathological
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="git timed out")


def _git_bytes(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _git_argv(args),
            cwd=str(repo),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - host without git
        return subprocess.CompletedProcess(
            args, 1, stdout=b"", stderr=str(exc).encode()
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - pathological
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"git timed out")


def _run_git(repo: Path, args: list[str]) -> str:
    proc = _git(repo, args)
    if proc.returncode != 0:
        raise EvidenceError((proc.stderr or "git command failed").strip())
    return proc.stdout.strip()


def capture_source(repo_path: str | Path) -> dict:
    """Capture a ``{sha, status_porcelain}`` source snapshot, or an empty one.

    The producer calls this before gates and again after execution so evidence
    binds the exact revision that was measured and proves it did not move.
    A repository that cannot be read yields empty values and can never certify.
    """
    repo = Path(repo_path).expanduser().resolve()
    try:
        sha = _run_git(repo, ["rev-parse", "HEAD"])
        status = _run_git(repo, ["status", "--short"])
    except EvidenceError:
        return {"sha": "", "status_porcelain": ""}
    return {"sha": sha, "status_porcelain": status}


def resolve_base_info(repo_path: str | Path, ref: str) -> dict:
    """Resolve the diff base to an immutable commit SHA before gates run.

    Returns ``{"ref", "kind", "sha"}``. ``kind`` is ``"commit"`` when the ref
    resolves, otherwise ``"unresolved"`` with an empty SHA -- a run that cannot
    name its measured base cannot certify.
    """
    repo = Path(repo_path).expanduser().resolve()
    proc = _git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    if not FULL_SHA_RE.match(sha):
        return {"ref": ref, "kind": "unresolved", "sha": ""}
    return {"ref": ref, "kind": "commit", "sha": sha}


# ---------------------------------------------------------------------------
# engine identity
# ---------------------------------------------------------------------------


def engine_package_dir() -> Path:
    """Directory holding the engine modules whose hashes define the fingerprint."""
    return Path(__file__).resolve().parent


def _engine_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib always present on 3.11+
        return "0.0.0"
    try:
        return version(ENGINE_NAME)
    except PackageNotFoundError:
        pass
    pyproject = engine_package_dir().parent / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return "0.0.0"
    return str(data.get("project", {}).get("version", "0.0.0"))


def engine_identity() -> dict:
    """Return the engine fingerprint the verifier recomputes exactly.

    Canonical text: ``name|version|python_major.minor|`` followed by one
    newline-joined ``relpath:sha256`` line per ``factory/*.py`` file, sorted by
    path. Fingerprint = ``sha256:`` over those UTF-8 bytes.
    """
    package = engine_package_dir()
    version = _engine_version()
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    lines = [f"{path.name}:{_sha256(path)}" for path in sorted(package.glob("*.py"))]
    canonical = "\n".join([f"{ENGINE_NAME}|{version}|{python}|", *lines])
    fingerprint = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "name": ENGINE_NAME,
        "version": version,
        "python": python,
        "fingerprint": fingerprint,
    }


# ---------------------------------------------------------------------------
# producer
# ---------------------------------------------------------------------------


def _environment_snapshot(repo: Path, env_file: str) -> dict:
    path = repo / env_file
    if not path.is_file():
        return {"path": str(path), "present": False}
    return {"path": str(path), "present": True, "sha256": _sha256(path)}


def _fixture_records(repo: Path, fixture_paths: Iterable[str]) -> list[dict]:
    records = []
    for relative in fixture_paths:
        path = (repo / relative).resolve()
        if not path.is_relative_to(repo):
            raise EvidenceError(
                f"fixture escapes repository root: {relative} -> {path}"
            )
        if not path.is_file():
            raise EvidenceError(f"fixture does not exist: {path}")
        tracked = bool(_git(repo, ["ls-files", "--", relative]).stdout.strip())
        records.append({"path": relative, "sha256": _sha256(path), "tracked": tracked})
    return records


def artifact_records(
    artifacts_dir: str | Path, names: Iterable[str] = REQUIRED_ARTIFACTS
) -> list[dict]:
    """Hash declared artifacts, rejecting names that are not bare basenames."""
    directory = Path(artifacts_dir).expanduser().resolve()
    if not directory.is_dir():
        raise EvidenceError(f"artifacts directory does not exist: {directory}")
    records = []
    for name in names:
        if (
            not isinstance(name, str)
            or name in (".", "..")
            or not ARTIFACT_NAME_RE.match(name)
        ):
            raise EvidenceError(f"artifact name must be a bare basename: {name!r}")
        path = directory / name
        if path.is_symlink():
            raise EvidenceError(f"artifact is a symlink: {name}")
        resolved = path.resolve()
        if not resolved.is_relative_to(directory):
            raise EvidenceError(f"artifact escapes artifacts directory: {name}")
        if not resolved.is_file():
            raise EvidenceError(f"artifact missing: {name}")
        records.append(
            {
                "name": name,
                "sha256": _sha256(resolved),
                "bytes": resolved.stat().st_size,
            }
        )
    return records


def _snapshot(value: dict | None, fallback: dict, label: str) -> dict:
    if value is None:
        return dict(fallback)
    if not isinstance(value, dict):
        raise EvidenceError(f"source {label} must be a mapping")
    sha = value.get("sha")
    status = value.get("status_porcelain", "")
    if not isinstance(sha, str) or not isinstance(status, str):
        raise EvidenceError(f"source {label} needs a string sha and status_porcelain")
    return {"sha": sha, "status_porcelain": status}


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
    mode: str | None = None,
    source_start: dict | None = None,
    source_end: dict | None = None,
    runtime: dict | None = None,
    base: dict | None = None,
    artifacts: Iterable[str] | None = None,
    card_id: str | None = None,
    project_name: str = "",
) -> Path:
    """Write a schema-v2 manifest plus its ``sha256sum`` sidecar.

    Legacy top-level keys keep their prior names and meanings so external
    readers continue to work; ``git_revision`` is an alias for ``source.sha``.

    ``mode`` defaults to ``acceptance`` when gates run and ``diagnostic`` when
    they do not. An explicit acceptance mode with gates disabled is refused:
    the contradiction must not be recordable.

    The function deliberately records hashes and a git diff rather than values
    from dotenv files, so secrets are never copied into the artifact.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise EvidenceError(f"repository does not exist: {repo}")
    if mode is None:
        mode = "acceptance" if gates_run else "diagnostic"
    if mode not in MODES:
        raise EvidenceError(f"mode must be one of {MODES}: {mode!r}")
    if mode == "acceptance" and not gates_run:
        raise EvidenceError(
            "acceptance mode requires gates; a run with gates disabled is diagnostic"
        )
    output_dir = Path(artifacts_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    current = capture_source(repo)
    start = _snapshot(source_start, current, "start")
    end = _snapshot(source_end, current, "end")
    clean = bool(
        start["sha"]
        and start["sha"] == end["sha"]
        and start["status_porcelain"] == ""
        and end["status_porcelain"] == ""
    )
    declared = (
        list(artifacts)
        if artifacts is not None
        else [name for name in REQUIRED_ARTIFACTS if (output_dir / name).is_file()]
    )
    manifest = {
        # schema v2
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
        "mode": mode,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "card_id": card_id
        if card_id is not None
        else os.environ.get("HERMES_KANBAN_TASK", ""),
        "project_name": project_name,
        "source": {"sha": start["sha"], "clean": clean, "start": start, "end": end},
        "base": dict(base or {}),
        "engine": engine_identity(),
        "runtime": dict(runtime or {}),
        "artifacts": artifact_records(output_dir, declared) if declared else [],
        # legacy reader keys (same names, same meanings)
        "git_revision": start["sha"],
        "git_status_porcelain": start["status_porcelain"],
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
        "fixtures": _fixture_records(repo, fixture_paths),
        "execution": execution or {},
        "hermes_factory_card": card_id
        if card_id is not None
        else os.environ.get("HERMES_KANBAN_TASK", ""),
    }
    path = output_dir / MANIFEST_FILENAME
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    path.write_text(payload)
    sidecar = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    (output_dir / SIDECAR_FILENAME).write_text(f"{sidecar}  {MANIFEST_FILENAME}\n")
    return path


# ---------------------------------------------------------------------------
# shared gate policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateClaim:
    """An inapplicable gate result that the verifier must substantiate."""

    gate: str
    key: str
    justification: str


def evaluate_gate_policy(gates: dict) -> tuple[list[str], list[GateClaim]]:
    """Apply the code-owned gate table; return (violations, inapplicable claims).

    Disabled, unavailable, unsupported, erroring, failing, missing, or
    unpermitted-skip gates are violations. A permitted inapplicable result is
    returned as a claim: it is necessary but not sufficient, because the
    verifier must prove it from candidate/base content.
    """
    violations: list[str] = []
    claims: list[GateClaim] = []
    for gate, policy in GATE_POLICY.items():
        value = gates.get(gate) if isinstance(gates, dict) else None
        if not isinstance(value, dict):
            violations.append(f"gate_missing:{gate}")
            continue
        status = value.get("status")
        if not isinstance(status, str) or not status:
            violations.append(f"gate_missing:{gate}")
            continue
        if status in _STATUS_FAILED:
            violations.append(f"gate_failed:{gate}")
            continue
        if status in _STATUS_UNAVAILABLE:
            violations.append(f"gate_unavailable:{gate}")
            continue
        if status == policy["required"]["status"]:
            if value.get("passed") is False:
                violations.append(f"gate_failed:{gate}")
                continue
            for name, expected in policy["required"].items():
                if name == "status":
                    continue
                if value.get(name) is not expected:
                    violations.append(f"gate_failed:{gate}")
                    break
            continue
        if value.get("passed") is False:
            violations.append(f"gate_failed:{gate}")
            continue
        key = status if status != "skipped" else f"skipped:{value.get('reason')}"
        if key in policy["inapplicable"]:
            claims.append(GateClaim(gate, key, policy["inapplicable"][key]))
            continue
        violations.append(f"gate_not_applicable_unpermitted:{gate}")
    return violations, claims


def gate_policy_violations(gates: dict) -> list[str]:
    """Reason codes for gate results that cannot certify (claims excluded)."""
    return evaluate_gate_policy(gates)[0]


# ---------------------------------------------------------------------------
# legacy (v1) reader -- kept compatible, never the verifier
# ---------------------------------------------------------------------------


def assert_assembled_evidence(manifest: dict) -> None:
    """Reject manifests that cannot certify an assembled-system claim.

    This is the *legacy* reader with its historical accept/reject semantics,
    re-expressed over ``GATE_POLICY``. It exists so existing artifacts and
    readers keep working. It does NOT check schema identity, acceptance mode,
    source binding, teardown, safety, engine identity, or artifact hashes, so
    it must never be used as the evidence-gate verifier (``check_evidence`` /
    ``verify_evidence`` in this module).
    """
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
    violations = gate_policy_violations(
        {gate: manifest.get(gate) or {} for gate in GATE_POLICY}
    )
    if violations:
        raise EvidenceError(
            "assembled evidence requires successful gates or an explicit "
            f"permitted skip ({violations[0]})"
        )


# ---------------------------------------------------------------------------
# schema v2 checker
# ---------------------------------------------------------------------------


@dataclass
class EvidenceCheck:
    ok: bool
    reason: str = "ok"
    detail: str = ""
    checks: list[dict] = field(default_factory=list)
    gate_claims: list[GateClaim] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "checks": list(self.checks),
        }


def _require(condition, message: str) -> None:
    if not condition:
        raise _Malformed(message)


def _is_sha(value) -> bool:
    return isinstance(value, str) and bool(FULL_SHA_RE.match(value))


def _is_int(value) -> bool:
    return type(value) is int  # bool is not an integer here, deliberately


def _is_number(value) -> bool:
    return _is_int(value) or (isinstance(value, float) and value == value)


def _is_str(value) -> bool:
    return isinstance(value, str)


def _check_snapshot(value, label: str) -> None:
    _require(isinstance(value, dict), f"source.{label} must be a mapping")
    _require(_is_sha(value.get("sha")), f"source.{label}.sha must be a 40-hex commit")
    _require(
        _is_str(value.get("status_porcelain")),
        f"source.{label}.status_porcelain must be a string",
    )


def _check_fixture_path(path, index: int) -> None:
    _require(_is_str(path), f"fixtures[{index}].path must be a string")
    _require(bool(path), f"fixtures[{index}].path is empty")
    _require(not path.startswith(("/", "-")), f"fixtures[{index}].path is absolute")
    _require("\\" not in path, f"fixtures[{index}].path uses a backslash")
    parts = Path(path).parts
    _require(".." not in parts, f"fixtures[{index}].path escapes the repository")


def _check_structure(manifest: dict) -> None:
    """Types and required fields only; acceptance semantics come later."""
    mode = manifest.get("mode")
    _require(mode in MODES, f"mode must be one of {MODES}: {mode!r}")
    _require(type(manifest.get("gates_run")) is bool, "gates_run must be a boolean")

    entry_point = manifest.get("entry_point")
    _require(
        _is_str(entry_point) and bool(entry_point.strip()), "entry_point is required"
    )
    command = manifest.get("command")
    if _is_str(command):
        _require(bool(command.strip()), "command is empty")
    else:
        _require(
            isinstance(command, list)
            and bool(command)
            and all(_is_str(part) and part for part in command),
            "command must be a non-empty string or list of strings",
        )

    source = manifest.get("source")
    _require(isinstance(source, dict), "source must be a mapping")
    _require(_is_sha(source.get("sha")), "source.sha must be a 40-hex commit")
    _require(type(source.get("clean")) is bool, "source.clean must be a boolean")
    _check_snapshot(source.get("start"), "start")
    _check_snapshot(source.get("end"), "end")
    _require(
        _is_sha(manifest.get("git_revision")),
        "git_revision must be a 40-hex commit (legacy alias of source.sha)",
    )
    _require(
        manifest["git_revision"] == source["sha"],
        "git_revision must equal source.sha",
    )
    _require(
        _is_str(manifest.get("git_status_porcelain")),
        "git_status_porcelain must be a string",
    )

    base = manifest.get("base")
    _require(isinstance(base, dict), "base must be a mapping")
    _require(_is_str(base.get("ref")) and bool(base.get("ref")), "base.ref is required")
    _require(_is_str(base.get("kind")), "base.kind must be a string")
    _require(_is_sha(base.get("sha")), "base.sha must be a 40-hex commit")

    coverage = manifest.get("coverage")
    _require(isinstance(coverage, dict), "coverage must be a mapping")
    _require(
        _is_sha(coverage.get("base_sha")), "coverage.base_sha must be a 40-hex commit"
    )

    execution = manifest.get("execution")
    _require(isinstance(execution, dict), "execution must be a mapping")
    _require(
        _is_int(execution.get("exit_code")), "execution.exit_code must be an integer"
    )
    _require(
        type(execution.get("passed")) is bool, "execution.passed must be a boolean"
    )
    _require(_is_str(execution.get("stage")), "execution.stage must be a string")
    if "duration_s" in execution:
        _require(
            _is_number(execution["duration_s"]), "execution.duration_s must be a number"
        )

    runtime = manifest.get("runtime")
    _require(isinstance(runtime, dict), "runtime must be a mapping")
    teardown = runtime.get("teardown")
    _require(isinstance(teardown, dict), "runtime.teardown must be a mapping")
    _require(
        type(teardown.get("attempted")) is bool,
        "runtime.teardown.attempted must be a boolean",
    )
    _require(type(teardown.get("ok")) is bool, "runtime.teardown.ok must be a boolean")
    _require(
        _is_str(teardown.get("detail", "")), "runtime.teardown.detail must be a string"
    )
    safety = runtime.get("safety")
    _require(isinstance(safety, dict), "runtime.safety must be a mapping")
    _require(
        _is_str(safety.get("postflight")), "runtime.safety.postflight must be a string"
    )
    for key in ("protected_changes", "unexpected_changes"):
        value = safety.get(key)
        _require(isinstance(value, list), f"runtime.safety.{key} must be a list")
        _require(
            all(_is_str(item) for item in value),
            f"runtime.safety.{key} must contain strings",
        )
    _require(
        type(safety.get("collateral_damage")) is bool,
        "runtime.safety.collateral_damage must be a boolean",
    )

    engine = manifest.get("engine")
    _require(isinstance(engine, dict), "engine must be a mapping")
    _require(_is_str(engine.get("name")), "engine.name must be a string")
    _require(_is_str(engine.get("version")), "engine.version must be a string")
    _require(_is_str(engine.get("python")), "engine.python must be a string")
    _require(
        _is_str(engine.get("fingerprint"))
        and bool(FINGERPRINT_RE.match(engine["fingerprint"])),
        "engine.fingerprint must be sha256:<64 hex>",
    )

    artifacts = manifest.get("artifacts")
    _require(isinstance(artifacts, list), "artifacts must be a list")
    for index, record in enumerate(artifacts):
        _require(isinstance(record, dict), f"artifacts[{index}] must be a mapping")
        _require(
            _is_str(record.get("name")), f"artifacts[{index}].name must be a string"
        )
        _require(
            _is_str(record.get("sha256"))
            and bool(re.fullmatch(r"[0-9a-f]{64}", record["sha256"])),
            f"artifacts[{index}].sha256 must be 64 hex characters",
        )
        _require(
            _is_int(record.get("bytes")) and record["bytes"] >= 0,
            f"artifacts[{index}].bytes must be a non-negative integer",
        )

    fixtures = manifest.get("fixtures")
    _require(isinstance(fixtures, list), "fixtures must be a list")
    for index, record in enumerate(fixtures):
        _require(isinstance(record, dict), f"fixtures[{index}] must be a mapping")
        _check_fixture_path(record.get("path"), index)
        _require(
            _is_str(record.get("sha256"))
            and bool(re.fullmatch(r"[0-9a-f]{64}", record["sha256"])),
            f"fixtures[{index}].sha256 must be 64 hex characters",
        )
        _require(
            type(record.get("tracked")) is bool,
            f"fixtures[{index}].tracked must be a boolean",
        )

    environment = manifest.get("environment")
    _require(isinstance(environment, dict), "environment must be a mapping")
    example = environment.get("example_env")
    _require(isinstance(example, dict), "environment.example_env must be a mapping")
    _require(
        example.get("present") is True,
        "environment.example_env.present must be true (documented environment)",
    )


def _check_engine(manifest: dict) -> tuple[bool, str]:
    actual = engine_identity()
    recorded = manifest["engine"]
    if recorded.get("name") != actual["name"] or (
        recorded.get("fingerprint") != actual["fingerprint"]
    ):
        return False, (
            "evidence was produced by a different engine build "
            f"(recorded {recorded.get('name')} {recorded.get('version')} "
            f"{recorded.get('fingerprint')}; this engine is {actual['name']} "
            f"{actual['version']} {actual['fingerprint']}). "
            "Re-run acceptance with this engine; old evidence cannot be certified."
        )
    return True, ""


def _check_artifacts(manifest: dict, artifacts_dir: Path) -> tuple[bool, str, str]:
    artifacts = manifest["artifacts"]
    if not artifacts:
        return False, "artifact_missing", "manifest declares no artifacts"
    seen: set[str] = set()
    for record in artifacts:
        name = record["name"]
        if name in (".", "..") or not ARTIFACT_NAME_RE.match(name):
            return (
                False,
                f"artifact_path_escape:{name}",
                f"artifact name {name!r} is not a bare basename",
            )
        if name in seen:
            return False, f"duplicate_artifact:{name}", f"{name} declared twice"
        seen.add(name)
    for required in REQUIRED_ARTIFACTS:
        if required not in seen:
            return (
                False,
                f"artifact_missing:{required}",
                f"required artifact {required} is not declared",
            )
    if not artifacts_dir.is_dir():
        return False, "artifacts_dir_invalid", f"not a directory: {artifacts_dir}"
    for record in artifacts:
        name = record["name"]
        path = artifacts_dir / name
        if path.is_symlink():
            return (
                False,
                f"artifact_path_escape:{name}",
                f"artifact {name} is a symlink",
            )
        resolved = path.resolve()
        if not resolved.is_relative_to(artifacts_dir):
            return (
                False,
                f"artifact_path_escape:{name}",
                f"artifact {name} resolves outside the artifacts directory",
            )
        if not resolved.is_file():
            return (
                False,
                f"artifact_missing:{name}",
                f"artifact {name} does not exist",
            )
        size = resolved.stat().st_size
        if size != record["bytes"]:
            return (
                False,
                f"artifact_size_mismatch:{name}",
                f"{name} is {size} bytes, manifest records {record['bytes']}",
            )
        digest = _sha256(resolved)
        if digest != record["sha256"]:
            return (
                False,
                f"artifact_hash_mismatch:{name}",
                f"{name} sha256 {digest} != {record['sha256']} recorded in manifest",
            )
    return True, "", ""


def check_evidence(manifest: dict, *, artifacts_dir: str | Path) -> EvidenceCheck:
    """Schema-v2 acceptance checks that need no repository comparison.

    This is the runner's self-check and the verifier's core. Candidate/base
    equality, repository objects, ancestry, fixture tree hashes, and
    gate-inapplicability substantiation belong to ``verify_evidence``.
    """
    checks: list[dict] = []

    def reject(name: str, reason: str, detail: str) -> EvidenceCheck:
        checks.append({"name": name, "ok": False, "reason": reason})
        return EvidenceCheck(False, reason, detail, checks)

    def accept(name: str) -> None:
        checks.append({"name": name, "ok": True})

    if not isinstance(manifest, dict):
        return reject(
            "manifest", "malformed_manifest", "manifest must be a JSON object"
        )

    schema = manifest.get("schema")
    if schema is None:
        return reject(
            "schema",
            "legacy_schema_unsupported",
            "manifest has no schema block; legacy (v1) evidence cannot be certified",
        )
    if not isinstance(schema, dict):
        return reject("schema", "malformed_manifest", "schema must be an object")
    if schema.get("name") != SCHEMA_NAME:
        return reject(
            "schema",
            "unsupported_schema_name",
            f"schema.name must be {SCHEMA_NAME!r}: {schema.get('name')!r}",
        )
    version = schema.get("version")
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        return reject(
            "schema",
            "unsupported_schema_version",
            f"schema.version {version!r} is not in {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
        )
    accept("schema")

    try:
        _check_structure(manifest)
    except _Malformed as exc:
        return reject("structure", "malformed_manifest", str(exc))
    accept("structure")

    if manifest["mode"] != "acceptance":
        return reject(
            "mode",
            "mode_not_acceptance",
            f"mode is {manifest['mode']!r}; diagnostic evidence can never certify",
        )
    accept("mode")

    if manifest["gates_run"] is not True:
        return reject(
            "gates_enabled",
            "gates_disabled",
            "gates_run is false but mode is acceptance (contradiction)",
        )
    accept("gates_enabled")

    source = manifest["source"]
    start, end = source["start"], source["end"]
    if start["status_porcelain"] != "":
        return reject(
            "source",
            "source_dirty",
            f"source was not clean at start: {start['status_porcelain']!r}",
        )
    if (
        end["sha"] != start["sha"]
        or end["status_porcelain"] != start["status_porcelain"]
    ):
        return reject(
            "source",
            "source_changed_during_run",
            f"source moved {start['sha']} -> {end['sha']} / "
            f"{start['status_porcelain']!r} -> {end['status_porcelain']!r}",
        )
    if source["clean"] is not True:
        return reject(
            "source",
            "source_dirty",
            "manifest marks the source unclean while both snapshots look clean",
        )
    accept("source")

    execution = manifest["execution"]
    if (
        execution["passed"] is not True
        or execution["exit_code"] != 0
        or execution["stage"] != "complete"
    ):
        return reject(
            "execution",
            "execution_incomplete",
            f"execution was {execution['stage']!r} with exit {execution['exit_code']} "
            f"(passed={execution['passed']})",
        )
    accept("execution")

    runtime = manifest["runtime"]
    teardown = runtime["teardown"]
    if teardown["attempted"] is not True or teardown["ok"] is not True:
        return reject(
            "teardown",
            "teardown_failed",
            f"teardown attempted={teardown['attempted']} ok={teardown['ok']}: "
            f"{teardown.get('detail', '')}",
        )
    safety = runtime["safety"]
    if safety["postflight"] != "clean" or safety["protected_changes"]:
        return reject(
            "safety",
            "safety_incident",
            f"postflight {safety['postflight']!r} with protected changes "
            f"{safety['protected_changes']}",
        )
    if safety["unexpected_changes"] or safety["collateral_damage"] is not False:
        return reject(
            "safety",
            "collateral_change",
            f"unexpected changes {safety['unexpected_changes']} "
            f"(collateral_damage={safety['collateral_damage']})",
        )
    accept("runtime")

    violations, claims = evaluate_gate_policy(
        {gate: manifest.get(gate) or {} for gate in GATE_POLICY}
    )
    if violations:
        return reject("gates", violations[0], f"gate policy rejected: {violations[0]}")
    accept("gates")

    engine_ok, engine_detail = _check_engine(manifest)
    if not engine_ok:
        return reject("engine", "engine_mismatch", engine_detail)
    accept("engine")

    artifacts_ok, artifacts_reason, artifacts_detail = _check_artifacts(
        manifest, Path(artifacts_dir).expanduser().resolve()
    )
    if not artifacts_ok:
        return reject("artifacts", artifacts_reason, artifacts_detail)
    accept("artifacts")

    return EvidenceCheck(True, "ok", "", checks, claims)


# ---------------------------------------------------------------------------
# verifier
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    ok: bool
    reason: str = "ok"
    detail: str = ""
    candidate: str = ""
    base: str = ""
    checks: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "candidate": self.candidate,
            "base": self.base,
            "checks": list(self.checks),
            "evidence": dict(self.evidence),
        }


def _evidence_summary(manifest: dict) -> dict:
    engine = manifest.get("engine") or {}
    schema = manifest.get("schema") or {}
    return {
        "schema_version": schema.get("version"),
        "mode": manifest.get("mode"),
        "engine": {
            "version": engine.get("version"),
            "fingerprint": engine.get("fingerprint"),
        },
    }


def _candidate_tree_paths(repo: Path, candidate: str) -> tuple[set[str], str]:
    proc = _git(repo, ["ls-tree", "-r", "--name-only", "-z", candidate])
    if proc.returncode != 0:
        return set(), (proc.stderr or "git ls-tree failed").strip()
    return {name for name in proc.stdout.split("\0") if name}, ""


def _blob_sha256(repo: Path, revision: str, path: str) -> str | None:
    proc = _git_bytes(repo, ["cat-file", "blob", f"{revision}:{path}"])
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def _added_python_lines(
    repo: Path, base: str, candidate: str
) -> tuple[int | None, str]:
    proc = _git(
        repo,
        [
            "diff",
            "--unified=0",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            base,
            candidate,
            "--",
            "*.py",
        ],
    )
    if proc.returncode != 0:
        return None, (proc.stderr or "git diff failed").strip()
    added = 0
    for line in proc.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
    return added, ""


def _substantiate_claim(
    claim: GateClaim, repo: Path, base: str, candidate: str, tree: set[str]
) -> str:
    """Return why an inapplicable gate claim is unsupported, or ''."""
    if claim.gate == "audit" and claim.key == "no_manifest":
        present = [name for name in DEPENDENCY_MANIFESTS if name in tree]
        if present:
            return (
                "audit claims no dependency manifest, but the candidate tree "
                f"contains {present}"
            )
        return ""
    if claim.gate == "coverage" and claim.key == "skipped:no_added_python":
        added, error = _added_python_lines(repo, base, candidate)
        if added is None:
            return f"could not compute the base..candidate python diff: {error}"
        if added:
            return (
                "coverage claims no added python lines, but the base..candidate "
                f"diff adds {added}"
            )
        return ""
    if claim.gate == "datamodel" and claim.key == "skipped:no_data_model":
        if DATA_MODEL_FILENAME in tree:
            return (
                "datamodel claims no data model, but the candidate tree contains "
                f"{DATA_MODEL_FILENAME}"
            )
        return ""
    return f"no substantiation rule for {claim.gate}:{claim.key}"


class _StrictManifestError(ValueError):
    """Manifest bytes are not one unambiguous JSON object (internal signal)."""


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    """``json`` object hook: refuse a repeated key inside ONE object.

    ``json`` keeps the *last* value of a repeated member, so a manifest reading
    ``{"mode": "diagnostic", "mode": "acceptance"}`` certifies as acceptance
    while another conforming reader may keep ``diagnostic``. Refusing every
    repeat at every depth -- same or conflicting value, either order -- removes
    the ambiguity instead of silently choosing a winner.
    """
    members: dict = {}
    for key, value in pairs:
        if key in members:
            raise _StrictManifestError(f"manifest repeats the object key {key[:80]!r}")
        members[key] = value
    return members


def _parse_manifest_bytes(raw: bytes) -> object:
    """Decode manifest bytes strictly, or raise :class:`_StrictManifestError`.

    Bounded on purpose: a sub-1 MiB document can still nest past the JSON
    decoder's recursion limit, which is malformed input rather than a crash.
    The caller turns every failure here into ``malformed_manifest`` so the CLI
    keeps its ``--json`` / closed-reason-code contract and no traceback escapes.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _StrictManifestError(f"manifest is not UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_unique_json_object)
    except _StrictManifestError:
        raise
    except json.JSONDecodeError as exc:
        raise _StrictManifestError(f"manifest is not JSON: {exc}") from exc
    except RecursionError as exc:
        raise _StrictManifestError(
            "manifest nests deeper than the JSON decoder limit"
        ) from exc


def verify_evidence(
    artifacts_dir: str | Path,
    repo_path: str | Path,
    candidate: str,
    base: str,
) -> VerificationResult:
    """Read-only, offline verification of one evidence directory.

    Returns a ``VerificationResult``; it never raises for a rejection, never
    writes a file, never mutates a ref, and never creates a receipt. Promotion
    calls this in-process so there is no artifact to replay.
    """
    checks: list[dict] = []

    def reject(name: str, reason: str, detail: str) -> VerificationResult:
        checks.append({"name": name, "ok": False, "reason": reason})
        return VerificationResult(False, reason, detail, candidate, base, checks, {})

    def accept(name: str) -> None:
        checks.append({"name": name, "ok": True})

    if not _is_sha(candidate) or not _is_sha(base):
        return reject(
            "arguments",
            "invalid_argument",
            "candidate and base must be full 40-hex commit SHAs",
        )

    directory = Path(artifacts_dir).expanduser().resolve()
    if not directory.is_dir():
        return reject(
            "artifacts_dir",
            "artifacts_dir_invalid",
            f"artifacts directory does not exist: {directory}",
        )
    accept("artifacts_dir")

    manifest_path = directory / MANIFEST_FILENAME
    if manifest_path.is_symlink():
        return reject("manifest", "manifest_symlink", f"{manifest_path} is a symlink")
    if not manifest_path.is_file():
        return reject("manifest", "manifest_missing", f"no {MANIFEST_FILENAME}")
    if manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
        return reject(
            "manifest",
            "malformed_manifest",
            f"manifest exceeds {MANIFEST_MAX_BYTES} bytes",
        )
    raw = manifest_path.read_bytes()
    try:
        manifest = _parse_manifest_bytes(raw)
    except _StrictManifestError as exc:
        return reject("manifest", "malformed_manifest", str(exc))
    if not isinstance(manifest, dict):
        return reject(
            "manifest", "malformed_manifest", "manifest must be a JSON object"
        )
    accept("manifest")

    # Schema identity is inspected BEFORE the sidecar requirement so real
    # legacy evidence reports legacy_schema_unsupported rather than a missing
    # digest it can never have.
    schema = manifest.get("schema")
    if schema is None:
        return reject(
            "schema",
            "legacy_schema_unsupported",
            "manifest has no schema block; legacy (v1) evidence cannot be certified",
        )
    if not isinstance(schema, dict):
        return reject("schema", "malformed_manifest", "schema must be an object")
    if schema.get("name") != SCHEMA_NAME:
        return reject(
            "schema",
            "unsupported_schema_name",
            f"schema.name must be {SCHEMA_NAME!r}: {schema.get('name')!r}",
        )
    version = schema.get("version")
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        return reject(
            "schema",
            "unsupported_schema_version",
            f"schema.version {version!r} is not supported",
        )
    accept("schema")

    sidecar_path = directory / SIDECAR_FILENAME
    if sidecar_path.is_symlink():
        return reject("sidecar", "sidecar_symlink", f"{sidecar_path} is a symlink")
    if not sidecar_path.is_file():
        return reject(
            "sidecar",
            "manifest_digest_missing",
            f"no {SIDECAR_FILENAME} beside the manifest",
        )
    if sidecar_path.stat().st_size > SIDECAR_MAX_BYTES:
        return reject(
            "sidecar", "manifest_digest_mismatch", "sidecar is implausibly large"
        )
    parts = sidecar_path.read_text(errors="replace").split()
    expected = hashlib.sha256(raw).hexdigest()
    if (
        len(parts) != 2
        or parts[1] != MANIFEST_FILENAME
        or not re.fullmatch(r"[0-9a-f]{64}", parts[0])
        or parts[0] != expected
    ):
        return reject(
            "sidecar",
            "manifest_digest_mismatch",
            "sidecar digest does not match the manifest bytes",
        )
    accept("sidecar")

    core = check_evidence(manifest, artifacts_dir=directory)
    checks.extend(core.checks)
    if not core.ok:
        return VerificationResult(
            False,
            core.reason,
            core.detail,
            candidate,
            base,
            checks,
            _evidence_summary(manifest),
        )

    if manifest["source"]["sha"] != candidate:
        return reject(
            "candidate",
            "source_sha_mismatch",
            f"evidence was produced for {manifest['source']['sha']}, not {candidate}",
        )
    accept("candidate_sha")

    if manifest["base"]["sha"] != base or manifest["coverage"]["base_sha"] != base:
        return reject(
            "base",
            "base_mismatch",
            f"manifest base {manifest['base']['sha']} / coverage base "
            f"{manifest['coverage']['base_sha']} != expected {base}",
        )
    accept("base_sha")

    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        return reject("repo", "repo_invalid", f"repository does not exist: {repo}")
    probe = _git(repo, ["rev-parse", "--git-dir"])
    if probe.returncode != 0:
        return reject(
            "repo",
            "repo_invalid",
            f"not a git repository: {repo} ({(probe.stderr or '').strip()})",
        )
    accept("repo")

    for name, sha in (("base", base), ("candidate", candidate)):
        proc = _git(repo, ["cat-file", "-t", sha])
        if proc.returncode != 0:
            return reject(
                "objects",
                "unknown_object",
                f"{name} {sha} is not present in {repo}",
            )
        if proc.stdout.strip() != "commit":
            return reject(
                "objects",
                "not_a_commit",
                f"{name} {sha} is a {proc.stdout.strip()}, not a commit",
            )
    accept("objects")

    ancestry = _git(repo, ["merge-base", "--is-ancestor", base, candidate])
    if ancestry.returncode == 1:
        return reject(
            "ancestry",
            "not_fast_forward",
            f"{base} is not an ancestor of {candidate}",
        )
    if ancestry.returncode != 0:
        return reject(
            "ancestry",
            "unknown_object",
            f"could not compare ancestry: {(ancestry.stderr or '').strip()}",
        )
    accept("ancestry")

    tree, tree_error = _candidate_tree_paths(repo, candidate)
    if tree_error:
        return reject(
            "objects", "unknown_object", f"could not read candidate tree: {tree_error}"
        )
    for claim in core.gate_claims:
        problem = _substantiate_claim(claim, repo, base, candidate, tree)
        if problem:
            return reject(
                "gates",
                f"gate_inapplicable_unsubstantiated:{claim.gate}",
                problem,
            )
    if core.gate_claims:
        accept("gate_substantiation")

    for record in manifest["fixtures"]:
        if record["tracked"] is not True:
            return reject(
                "fixtures",
                "fixture_untracked",
                f"fixture {record['path']} is not tracked; acceptance requires "
                "fixtures bound to the candidate commit",
            )
        digest = _blob_sha256(repo, candidate, record["path"])
        if digest is None:
            return reject(
                "fixtures",
                "fixture_missing",
                f"fixture {record['path']} is not in candidate {candidate}",
            )
        if digest != record["sha256"]:
            return reject(
                "fixtures",
                "fixture_hash_mismatch",
                f"fixture {record['path']} sha256 {digest} != {record['sha256']} "
                "recorded in manifest",
            )
    accept("fixtures")

    return VerificationResult(
        True, "ok", "", candidate, base, checks, _evidence_summary(manifest)
    )
