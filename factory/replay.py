"""Deterministic, non-promotional replay of one evaluator-owned suite.

``factory-learning replay`` compares an explicit baseline commit with an
explicit candidate commit by running *one* caller-supplied pytest suite twice in
two disposable checkouts. The suite, its expectation contract and the expected
digest are supplied externally; nothing in either checkout can authorize a new
expectation, and nothing here certifies acceptance.

The module is honest about its boundary: it is not a sandbox. Candidate code
runs as the same UID with the host filesystem and network reachable. What it
does provide is pytest *configuration* isolation (explicit config file, no
conftest, no plugin autoload, minimal environment), authoritative structured
pytest phase reports written by an engine-owned plugin outside both checkouts,
and a non-circular suite digest verified before, between and after execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from factory.evidence import FULL_SHA_RE, GIT_SAFETY_ARGUMENTS, parse_strict_json
from factory.learning import OUTCOMES, PROPOSAL_STATUS  # noqa: F401  (re-exported)
from factory.policy import PolicyViolation, assert_safe_repo_root

REPORT_SCHEMA_NAME = "factory-replay-report"
REPORT_SCHEMA_VERSION = 1
REPORT_MAX_BYTES = 2 * 1024 * 1024
REPORT_SIDECAR_FILENAME = "replay-report.sha256"
REPORT_FILENAME = "replay-report.json"

SUITE_EXPECTATIONS_FILENAME = "expectations.json"
SUITE_SCHEMA_NAME = "factory-replay-expectations"
SUITE_SCHEMA_VERSION = 1
SUITE_PYTEST_INI = "pytest.ini"
SUITE_TEST_GLOB = "test_*.py"
SUITE_MAX_FILES = 32
SUITE_FILE_MAX_BYTES = 256 * 1024
SUITE_TOTAL_MAX_BYTES = 1024 * 1024
SUITE_MAX_CONTROLS = 16
SUITE_TIMEOUT_DEFAULT = 300
SUITE_TIMEOUT_MAX = 3600
NODEID_MAX_CHARS = 300

PHASE_REPORT_SCHEMA_NAME = "factory-replay-phase-report"
PHASE_REPORT_SCHEMA_VERSION = 1

TARGET_BASELINE_OUTCOME = "assertion_failure"
TARGET_CANDIDATE_OUTCOME = "pass"
CONTROL_OUTCOME = "pass"

VERDICT_REPRODUCED = "reproduced"
VERDICT_NOT_REPRODUCED = "not_reproduced"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_REJECTED = "rejected"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TEST_FILE_RE = re.compile(r"^test_[A-Za-z0-9_]*\.py$")
_SUITE_FILE_RE = re.compile(
    r"^(pytest\.ini|expectations\.json|test_[A-Za-z0-9_]*\.py)$"
)

_SIDE_NAMES = ("baseline", "candidate")


class _Rejected(Exception):
    """Internal: replay admission or binding failed; nothing was executed."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class ReplayResult:
    """One replay outcome; a rejected input never raises to the caller."""

    verdict: str
    reason: str
    detail: str = ""
    report: dict = field(default_factory=dict)
    report_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == VERDICT_REPRODUCED

    @property
    def exit_code(self) -> int:
        return {
            VERDICT_REPRODUCED: 0,
            VERDICT_NOT_REPRODUCED: 1,
            VERDICT_REJECTED: 2,
            VERDICT_INCONCLUSIVE: 3,
        }[self.verdict]

    def as_dict(self) -> dict:
        if self.report:
            return dict(self.report)
        return {
            "schema": {"name": REPORT_SCHEMA_NAME, "version": REPORT_SCHEMA_VERSION},
            "promotional": False,
            "acceptance_evidence": False,
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
            "report_path": self.report_path,
            "limits": list(_LIMITS),
        }


_LIMITS = (
    "Not a sandbox: candidate code runs as the same UID with host filesystem "
    "and network reachable; pytest configuration isolation is not containment.",
    "Replay is historical reproduction of an observable, not original TDD "
    "provenance and not a claim about root cause.",
    "The report binds bytes to digests; same-UID rewriting is outside the "
    "integrity boundary.",
    "Baseline failure is diagnostic only: this report is never acceptance "
    "evidence and factory-verify/factory-promote reject it.",
)


# ---------------------------------------------------------------------------
# canonical digests
# ---------------------------------------------------------------------------


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(blob: bytes) -> str:
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _suite_files(suite_dir: Path) -> list[Path]:
    return sorted(
        (path for path in suite_dir.iterdir() if path.is_file()),
        key=lambda path: path.name,
    )


def compute_suite_digest(suite_dir: Path | str) -> str:
    """Non-circular digest over the suite's file names and contents.

    ``expectations.json`` contributes its content *without* the ``suite_digest``
    field, so the digest can be stored inside that file without hashing itself.
    Every other file contributes its raw bytes. The result therefore covers the
    expectation content while remaining recomputable from the bytes on disk.
    """
    suite = Path(suite_dir)
    rows = []
    for path in _suite_files(suite):
        if path.name == SUITE_EXPECTATIONS_FILENAME:
            payload = parse_strict_json(path.read_bytes())
            if isinstance(payload, dict):
                payload = {
                    key: value
                    for key, value in payload.items()
                    if key != "suite_digest"
                }
            blob = canonical_json_bytes(payload)
        else:
            blob = path.read_bytes()
        rows.append(
            {"name": path.name, "bytes": len(blob), "sha256": sha256_bytes(blob)}
        )
    return sha256_bytes(canonical_json_bytes(rows))


# ---------------------------------------------------------------------------
# suite binding
# ---------------------------------------------------------------------------


@dataclass
class SuiteSpec:
    directory: Path
    digest: str
    suite_id: str
    target: dict
    controls: list[dict]
    timeout_s: int
    files: list[Path]
    digest_by_name: dict[str, str]


def _bounded_text(value: object, label: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise _Rejected("unsupported_suite", f"{label} must be a non-empty string")
    if len(value) > limit:
        raise _Rejected("unsupported_suite", f"{label} exceeds {limit} chars")
    return value


def _expectation_entry(value: object, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"nodeid", "baseline", "candidate"}:
        raise _Rejected(
            "unsupported_suite",
            f"{label} must hold exactly nodeid, baseline and candidate",
        )
    nodeid = _bounded_text(value["nodeid"], f"{label}.nodeid", NODEID_MAX_CHARS)
    if "::" not in nodeid:
        raise _Rejected("unsupported_suite", f"{label}.nodeid is not a pytest node id")
    outcomes = {}
    for side in _SIDE_NAMES:
        block = value[side]
        if not isinstance(block, dict) or set(block) != {"outcome"}:
            raise _Rejected(
                "unsupported_suite", f"{label}.{side} must hold exactly outcome"
            )
        outcome = block["outcome"]
        if outcome not in OUTCOMES:
            raise _Rejected(
                "unsupported_suite", f"{label}.{side}.outcome is not a known outcome"
            )
        outcomes[side] = {"outcome": outcome}
    return {"nodeid": nodeid, **outcomes}


def _load_expectations(suite: Path, test_names: set[str]) -> dict:
    path = suite / SUITE_EXPECTATIONS_FILENAME
    if path.is_symlink() or not path.is_file():
        raise _Rejected("suite_invalid", "suite has no expectations.json")
    try:
        raw = path.read_bytes()
    except OSError as exc:  # pragma: no cover - unreadable fixture
        raise _Rejected("suite_invalid", f"cannot read expectations.json: {exc}")
    if len(raw) > SUITE_FILE_MAX_BYTES:
        raise _Rejected("suite_invalid", "expectations.json exceeds the suite bound")
    try:
        payload = parse_strict_json(raw)
    except ValueError as exc:
        raise _Rejected("suite_invalid", f"expectations.json is not strict JSON: {exc}")
    if not isinstance(payload, dict):
        raise _Rejected("suite_invalid", "expectations.json must be an object")
    allowed = {
        "schema",
        "suite_id",
        "suite_digest",
        "cases",
        "controls",
        "timeout_s",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise _Rejected(
            "unsupported_suite", f"unknown expectation key(s) {unknown[:5]}"
        )
    missing = sorted(
        {"schema", "suite_id", "suite_digest", "cases", "controls"} - set(payload)
    )
    if missing:
        raise _Rejected(
            "unsupported_suite", f"missing expectation key(s) {missing[:5]}"
        )

    schema = payload["schema"]
    if (
        not isinstance(schema, dict)
        or set(schema) != {"name", "version"}
        or schema["name"] != SUITE_SCHEMA_NAME
        or schema["version"] != SUITE_SCHEMA_VERSION
    ):
        raise _Rejected("unsupported_suite", f"unsupported suite schema {schema!r}")
    suite_id = _bounded_text(payload["suite_id"], "suite_id", 200)
    digest_field = payload["suite_digest"]
    if not isinstance(digest_field, str) or not _DIGEST_RE.match(digest_field):
        raise _Rejected("unsupported_suite", "suite_digest must be sha256:<64 hex>")

    cases = payload["cases"]
    if not isinstance(cases, list) or len(cases) != 1:
        raise _Rejected(
            "unsupported_suite", "v1 replay requires exactly one target case"
        )
    target = _expectation_entry(cases[0], "cases[0]")
    if target["baseline"]["outcome"] != TARGET_BASELINE_OUTCOME:
        raise _Rejected(
            "unsupported_suite",
            "v1 replay requires baseline outcome assertion_failure",
        )
    if target["candidate"]["outcome"] != TARGET_CANDIDATE_OUTCOME:
        raise _Rejected(
            "unsupported_suite", "v1 replay requires candidate outcome pass"
        )

    controls_raw = payload["controls"]
    if (
        not isinstance(controls_raw, list)
        or not controls_raw
        or len(controls_raw) > SUITE_MAX_CONTROLS
    ):
        raise _Rejected(
            "unsupported_suite",
            f"controls must hold 1-{SUITE_MAX_CONTROLS} entries",
        )
    controls = [
        _expectation_entry(entry, f"controls[{index}]")
        for index, entry in enumerate(controls_raw)
    ]
    for control in controls:
        for side in _SIDE_NAMES:
            if control[side]["outcome"] != CONTROL_OUTCOME:
                raise _Rejected(
                    "unsupported_suite",
                    f"control {control['nodeid']} must pass on both revisions",
                )

    nodeids = [target["nodeid"], *(control["nodeid"] for control in controls)]
    if len(set(nodeids)) != len(nodeids):
        raise _Rejected("unsupported_suite", "a nodeid is declared more than once")
    for nodeid in nodeids:
        file_part = nodeid.split("::", 1)[0]
        if file_part not in test_names:
            raise _Rejected(
                "unsupported_suite",
                f"nodeid names a file outside the suite: {file_part!r}",
            )

    timeout_s = payload.get("timeout_s", SUITE_TIMEOUT_DEFAULT)
    if type(timeout_s) is not int or not 1 <= timeout_s <= SUITE_TIMEOUT_MAX:
        raise _Rejected(
            "unsupported_suite",
            f"timeout_s must be 1-{SUITE_TIMEOUT_MAX}",
        )
    return {
        "suite_id": suite_id,
        "suite_digest": digest_field,
        "target": target,
        "controls": controls,
        "timeout_s": timeout_s,
    }


def load_suite(suite_dir: Path | str, expected_digest: str) -> SuiteSpec:
    """Admit the suite and prove it matches the caller's external digest."""
    if not isinstance(expected_digest, str) or not _DIGEST_RE.match(expected_digest):
        raise _Rejected(
            "suite_digest_missing", "an external sha256 suite digest is required"
        )
    raw = Path(suite_dir).expanduser()
    if raw.is_symlink():
        raise _Rejected("suite_invalid", f"suite directory is a symlink: {raw}")
    if not raw.is_dir():
        raise _Rejected("suite_invalid", f"suite directory does not exist: {raw}")
    suite = raw.resolve()

    files = _suite_files(suite)
    if not files:
        raise _Rejected("suite_invalid", "suite is empty")
    if len(files) > SUITE_MAX_FILES:
        raise _Rejected("suite_invalid", f"suite exceeds {SUITE_MAX_FILES} files")
    total = 0
    for path in files:
        if path.is_symlink():
            raise _Rejected("suite_invalid", f"suite contains a symlink: {path.name}")
        if not _SUITE_FILE_RE.match(path.name):
            raise _Rejected(
                "suite_invalid", f"suite contains an unsupported file: {path.name!r}"
            )
        if (
            not path.name == SUITE_PYTEST_INI
            and not _TEST_FILE_RE.match(path.name)
            and path.name != SUITE_EXPECTATIONS_FILENAME
        ):
            raise _Rejected(
                "suite_invalid", f"suite contains an unsupported file: {path.name!r}"
            )
        size = path.stat().st_size
        if size > SUITE_FILE_MAX_BYTES:
            raise _Rejected(
                "suite_invalid", f"suite file exceeds the bound: {path.name}"
            )
        total += size
    if total > SUITE_TOTAL_MAX_BYTES:
        raise _Rejected("suite_invalid", "suite exceeds the total size bound")

    test_names = {path.name for path in files if _TEST_FILE_RE.match(path.name)}
    if not test_names:
        raise _Rejected("suite_invalid", "suite contains no test_*.py file")
    if not any(path.name == SUITE_PYTEST_INI for path in files):
        raise _Rejected("suite_invalid", "suite has no pytest.ini")

    computed = compute_suite_digest(suite)
    expectations = _load_expectations(suite, test_names)
    if expectations["suite_digest"] != computed:
        raise _Rejected(
            "suite_digest_mismatch",
            "expectations.json records a digest its own bytes do not produce",
        )
    if computed != expected_digest:
        raise _Rejected(
            "suite_digest_mismatch",
            f"suite digest {computed} does not match the external {expected_digest}",
        )
    digest_by_name = {path.name: sha256_file(path) for path in files}
    return SuiteSpec(
        directory=suite,
        digest=computed,
        suite_id=expectations["suite_id"],
        target=expectations["target"],
        controls=expectations["controls"],
        timeout_s=expectations["timeout_s"],
        files=files,
        digest_by_name=digest_by_name,
    )


# ---------------------------------------------------------------------------
# path admission
# ---------------------------------------------------------------------------


def _assert_safe_directory(path: Path, label: str) -> Path:
    """Admit an explicit owned directory using the factory's safe-root semantics."""
    from factory.policy import HOME_ROOT_PREFIXES, SENSITIVE_SYSTEM_ROOTS

    if path == Path("/"):
        raise _Rejected(f"{label}_invalid", f"{label} must not be the host root")
    home = Path.home().resolve()
    if path == home:
        raise _Rejected(f"{label}_invalid", f"{label} must not be the home directory")
    for prefix in HOME_ROOT_PREFIXES:
        base = Path(prefix)
        if path != base and path.is_relative_to(base):
            if len(path.relative_to(base).parts) == 1:
                raise _Rejected(
                    f"{label}_invalid",
                    f"{label} must be nested below the home directory",
                )
    for sensitive in SENSITIVE_SYSTEM_ROOTS:
        base = Path(sensitive)
        if path == base or path.is_relative_to(base):
            raise _Rejected(
                f"{label}_invalid", f"{label} is inside sensitive host path {sensitive}"
            )
    return path


def _admit_work_root(
    work_root: str | Path, *, repo: Path, suite: Path, out: Path
) -> Path:
    raw = Path(work_root).expanduser()
    if raw.is_symlink():
        raise _Rejected("work_root_invalid", f"work root is a symlink: {raw}")
    if not raw.is_dir():
        raise _Rejected("work_root_invalid", f"work root does not exist: {raw}")
    root = _assert_safe_directory(raw.resolve(), "work_root")
    for protected, label in ((repo, "repository"), (suite, "suite")):
        if (
            root == protected
            or root.is_relative_to(protected)
            or protected.is_relative_to(root)
        ):
            raise _Rejected(
                "work_root_invalid", f"work root overlaps the {label}: {protected}"
            )
    if out == root or out.is_relative_to(root):
        raise _Rejected("out_invalid", "out must not live inside the work root")
    try:
        entries = list(root.iterdir())
    except OSError as exc:  # pragma: no cover - unreadable root
        raise _Rejected("work_root_invalid", f"cannot read work root: {exc}")
    if entries:
        raise _Rejected(
            "work_root_invalid",
            "work root must be empty; generated checkouts are retained and never "
            "cleaned destructively",
        )
    return root


def _admit_out(out: str | Path, *, repo: Path, suite: Path) -> Path:
    raw = Path(out).expanduser()
    if raw.is_symlink():
        raise _Rejected("out_invalid", f"out is a symlink: {raw}")
    if raw.exists():
        raise _Rejected("out_invalid", f"out already exists: {raw}")
    parent = raw.parent
    if parent.is_symlink() or not parent.is_dir():
        raise _Rejected("out_invalid", f"out parent is not a directory: {parent}")
    target = parent.resolve() / raw.name
    for protected, label in ((repo, "repository"), (suite, "suite")):
        if target == protected or target.is_relative_to(protected):
            raise _Rejected("out_invalid", f"out must not live inside the {label}")
    _assert_safe_directory(parent.resolve(), "out")
    return target


# ---------------------------------------------------------------------------
# repo / revision admission
# ---------------------------------------------------------------------------


def _git_environment() -> dict:
    env = {
        "PATH": os.defpath + os.pathsep + str(Path(sys.executable).parent),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C",
    }
    return env


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    argv = ["git", *GIT_SAFETY_ARGUMENTS, *args]
    try:
        return subprocess.run(
            argv,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=str(exc))


def _admit_repo(repo_arg: str | Path) -> Path:
    raw = Path(repo_arg).expanduser()
    if raw.is_symlink():
        raise _Rejected("repo_invalid", f"repository is a symlink: {raw}")
    if not raw.is_dir():
        raise _Rejected("repo_invalid", f"repository does not exist: {raw}")
    repo = raw.resolve()
    try:
        assert_safe_repo_root(repo)
    except PolicyViolation as exc:
        raise _Rejected("repo_invalid", str(exc))
    top = _run_git(repo, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        raise _Rejected("repo_invalid", f"not a git repository: {repo}")
    if Path(top.stdout.strip()).resolve() != repo:
        raise _Rejected(
            "repo_invalid", f"--repo must be the work-tree root, not {repo}"
        )
    return repo


def _resolve_revision(repo: Path, revision: str, label: str) -> str:
    if not isinstance(revision, str) or not FULL_SHA_RE.match(revision):
        raise _Rejected(
            "invalid_revision", f"{label} must be a full 40-hex lowercase commit SHA"
        )
    exists = _run_git(repo, ["cat-file", "-e", f"{revision}^{{commit}}"])
    if exists.returncode != 0:
        raise _Rejected(
            "unknown_revision",
            f"{label} {revision} is not a commit in the local object database "
            "(no fetch is performed)",
        )
    resolved = _run_git(repo, ["rev-parse", f"{revision}^{{commit}}"])
    if resolved.returncode != 0 or resolved.stdout.strip() != revision:
        raise _Rejected(
            "revision_mismatch",
            f"{label} {revision} did not resolve to itself",
        )
    return revision


# ---------------------------------------------------------------------------
# execution state
# ---------------------------------------------------------------------------


@dataclass
class ProcessOutcome:
    returncode: int
    timed_out: bool
    duration_s: float
    stdout_path: Path
    stderr_path: Path


def _kill_process_group(process: subprocess.Popen, grace_s: float = 5.0) -> None:
    """Kill only the launched process group, then reap the leader."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - already gone
        pass
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:  # pragma: no cover - pathological
        pass


def _write_process_logs(
    process: subprocess.Popen, stdout_path: Path, stderr_path: Path
) -> tuple[bytes, bytes]:
    stdout, stderr = process.communicate()
    stdout_path.write_bytes(stdout or b"")
    stderr_path.write_bytes(stderr or b"")
    return stdout or b"", stderr or b""


def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict,
    timeout_s: int,
    stdout_path: Path,
    stderr_path: Path,
) -> ProcessOutcome:
    """Launch one child in its own session; on timeout kill only that group."""
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        stdout, stderr = process.communicate()
    stdout_path.write_bytes(stdout or b"")
    stderr_path.write_bytes(stderr or b"")
    duration = time.monotonic() - started
    return ProcessOutcome(
        returncode=int(process.returncode),
        timed_out=timed_out,
        duration_s=round(duration, 3),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def _reject(reason: str, detail: str) -> ReplayResult:
    return ReplayResult(VERDICT_REJECTED, reason, detail)


def replay(
    *,
    repo: str | Path,
    baseline: str,
    candidate: str,
    suite_dir: str | Path,
    suite_digest: str,
    work_root: str | Path,
    out: str | Path,
    approve_execution: bool = False,
    timeout_s: int | None = None,
) -> ReplayResult:
    """Run one explicit, approved baseline/candidate replay comparison.

    Admission happens in a fixed order so a refusal never reads a Git object,
    creates a checkout or spawns a process: approval, revision shape, the
    external suite digest, path safety, then the repository and revisions.
    """
    try:
        if not approve_execution:
            raise _Rejected(
                "approval_required",
                "replay executes reviewed local code; pass --approve-execution",
            )
        if not isinstance(baseline, str) or not FULL_SHA_RE.match(baseline):
            raise _Rejected(
                "invalid_revision",
                "baseline must be a full 40-hex lowercase commit SHA",
            )
        if not isinstance(candidate, str) or not FULL_SHA_RE.match(candidate):
            raise _Rejected(
                "invalid_revision",
                "candidate must be a full 40-hex lowercase commit SHA",
            )
        if baseline == candidate:
            raise _Rejected("identical_revisions", "baseline and candidate must differ")
        if timeout_s is not None and (
            type(timeout_s) is not int or not 1 <= timeout_s <= SUITE_TIMEOUT_MAX
        ):
            raise _Rejected(
                "invalid_timeout", f"timeout must be 1-{SUITE_TIMEOUT_MAX} seconds"
            )

        repo_candidate = Path(repo).expanduser()
        if repo_candidate.is_symlink() or not repo_candidate.is_dir():
            raise _Rejected(
                "repo_invalid", f"repository does not exist: {repo_candidate}"
            )
        repo_path = repo_candidate.resolve()

        spec = load_suite(suite_dir, suite_digest)
        if spec.directory == repo_path or spec.directory.is_relative_to(repo_path):
            raise _Rejected(
                "suite_invalid",
                "the evaluator suite must live outside the repository under test",
            )

        out_path = _admit_out(out, repo=repo_path, suite=spec.directory)
        work_path = _admit_work_root(
            work_root, repo=repo_path, suite=spec.directory, out=out_path
        )
        if out_path == work_path or work_path.is_relative_to(out_path):
            raise _Rejected("out_invalid", "out must not contain the work root")

        admitted_repo = _admit_repo(repo_path)
        _resolve_revision(admitted_repo, baseline, "baseline")
        _resolve_revision(admitted_repo, candidate, "candidate")
    except _Rejected as exc:
        return _reject(exc.reason, exc.detail)

    out_path.mkdir(parents=True)
    (out_path / "logs").mkdir()
    return _execute(
        repo=admitted_repo,
        baseline=baseline,
        candidate=candidate,
        spec=spec,
        work_root=work_path,
        out=out_path,
        timeout_s=timeout_s if timeout_s is not None else spec.timeout_s,
    )


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

PLUGIN_FILENAME = "replay_plugin.py"
BOOTSTRAP_FILENAME = "_replay_child.py"

_PREFLIGHT_CODE = None  # the probe is a real file, loaded and guarded
PREFLIGHT_FILENAME = "_replay_preflight.py"
ISOLATION_FILENAME = "_replay_isolation.py"

_STRUCTURAL_REASONS = frozenset(
    {
        "malformed_report",
        "contradictory_exit",
        "collection_error",
        "no_tests",
        "interrupted",
        "missing_node",
        "duplicate_node",
        "unexpected_node",
        "skipped",
        "error_failure",
    }
)


def _engine_directory() -> Path:
    return Path(__file__).resolve().parent


def _plugin_path() -> Path:
    return _engine_directory() / PLUGIN_FILENAME


def _child_environment(checkout: Path, harness: Path) -> dict:
    """A minimal, documented environment: no credentials, no inherited pytest."""
    home = harness / "home"
    temporary = harness / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "PYTHONPATH": str(checkout),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "LC_ALL": "C",
    }


def _pytest_arguments(spec: SuiteSpec, harness: Path) -> list[str]:
    basetemp = harness / "pytest-tmp"
    arguments = [
        "-c",
        SUITE_PYTEST_INI,
        "--noconftest",
        "--rootdir",
        ".",
        "-p",
        "no:cacheprovider",
        "-q",
        "-rA",
        "--no-header",
        "--basetemp",
        str(basetemp),
    ]
    arguments.extend(path.name for path in spec.files if _TEST_FILE_RE.match(path.name))
    return arguments


def _write_log(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", errors="replace")


def _worktree_add(
    repo: Path, checkout: Path, revision: str, log_path: Path
) -> str | None:
    process = _run_git(repo, ["worktree", "add", "--detach", str(checkout), revision])
    _write_log(
        log_path,
        f"$ git worktree add --detach {checkout} {revision}\n"
        f"exit={process.returncode}\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}\n",
    )
    if process.returncode != 0:
        return (process.stderr or process.stdout or "git worktree add failed").strip()
    return None


def _preflight(
    checkout: Path, harness: Path, log_path: Path
) -> tuple[dict | None, str, str]:
    outcome = _run_process(
        [sys.executable, str(_engine_directory() / PREFLIGHT_FILENAME), str(checkout)],
        cwd=checkout,
        env=_child_environment(checkout, harness),
        timeout_s=60,
        stdout_path=log_path,
        stderr_path=log_path.with_suffix(".err.log"),
    )
    if outcome.timed_out or outcome.returncode != 0:
        return (
            None,
            "harness_error",
            f"preflight exited {outcome.returncode} (see {log_path.name})",
        )
    try:
        info = parse_strict_json(log_path.read_bytes())
    except ValueError as exc:
        return None, "harness_error", f"preflight output is not JSON: {exc}"
    if not isinstance(info, dict) or not isinstance(info.get("factory_file"), str):
        return None, "harness_error", "preflight output lacks factory_file"
    module_file = Path(info["factory_file"]).resolve()
    if module_file != checkout / "factory" / "__init__.py":
        return (
            None,
            "harness_error",
            f"import isolation failed: the factory package resolved to {module_file}, "
            f"outside the requested snapshot {checkout}",
        )
    evidence_file = info.get("evidence_file")
    if evidence_file is not None:
        resolved_evidence = Path(evidence_file).resolve()
        if resolved_evidence != checkout / "factory" / "evidence.py":
            return (
                None,
                "harness_error",
                f"import isolation failed: factory.evidence resolved to "
                f"{resolved_evidence}, outside the requested snapshot {checkout}",
            )
    for candidate in info.get("sitecustomize") or []:
        if not candidate:
            continue
        resolved = Path(candidate).resolve()
        if resolved.is_relative_to(checkout):
            return (
                None,
                "harness_error",
                f"the checkout planted an auto-imported {resolved.name} module",
            )
    return info, "", ""


def _report_problem(
    report: object, *, nonce: str, checkout: Path, plugin_sha: str, returncode: int
) -> tuple[str, str] | None:
    if not isinstance(report, dict):
        return ("malformed_report", "phase report is not an object")
    if report.get("schema") != {
        "name": PHASE_REPORT_SCHEMA_NAME,
        "version": PHASE_REPORT_SCHEMA_VERSION,
    }:
        return ("malformed_report", "phase report schema is not the trusted one")
    if report.get("nonce") != nonce:
        return ("malformed_report", "phase report nonce does not match this run")
    try:
        reported_checkout = Path(str(report.get("checkout"))).resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return ("malformed_report", "phase report carries no checkout")
    if reported_checkout != checkout:
        return ("malformed_report", "phase report was written for another checkout")
    plugin = report.get("plugin")
    if not isinstance(plugin, dict) or plugin.get("sha256") != plugin_sha:
        return (
            "malformed_report",
            "phase report plugin is not the trusted engine plugin",
        )
    exitstatus = report.get("exitstatus")
    if type(exitstatus) is not int:
        return ("malformed_report", "phase report carries no exit status")
    if exitstatus != returncode:
        return (
            "contradictory_exit",
            f"phase report exit {exitstatus} contradicts process exit {returncode}",
        )
    if not isinstance(report.get("collected"), list) or not isinstance(
        report.get("nodes"), dict
    ):
        return ("malformed_report", "phase report lacks collected/nodes")
    return None


def _node_outcomes(report: dict) -> dict[str, dict]:
    nodes = report.get("nodes") or {}
    result: dict[str, dict] = {}
    for nodeid, phases in nodes.items():
        if not isinstance(nodeid, str) or not isinstance(phases, dict):
            continue
        result[nodeid] = {
            phase: phases.get(phase) for phase in ("setup", "call", "teardown")
        }
    return result


def _classify_side(
    *, report: dict, returncode: int, expected: list[str], target: str
) -> tuple[str, str, dict[str, str]]:
    """Return ``(status, reason, observed outcomes)`` from pytest's own report."""
    collected = report.get("collected") or []
    nodes = _node_outcomes(report)
    observed: dict[str, str] = {}

    if len(collected) != len(set(collected)):
        return ("duplicate_node", "pytest collected a node id twice", observed)
    if report.get("collection_errors"):
        return (
            "collection_error",
            "collection reported an import/collection failure",
            observed,
        )
    if not collected:
        return ("no_tests", "pytest collected no tests", observed)
    if returncode == 5:
        return ("no_tests", "pytest exited 5 (no tests collected)", observed)
    if returncode in (3, 4):
        return (
            "interrupted",
            f"pytest exited {returncode} (internal error/interrupted)",
            observed,
        )

    unexpected = sorted(set(collected) - set(expected))
    if unexpected:
        return (
            "unexpected_node",
            f"pytest collected unexpected node {unexpected[0]!r}",
            observed,
        )
    missing = sorted(set(expected) - set(collected))
    if missing:
        return (
            "missing_node",
            f"expected node was not collected: {missing[0]!r}",
            observed,
        )
    missing = sorted(set(expected) - set(nodes))
    if missing:
        return ("missing_node", f"no phase report for node {missing[0]!r}", observed)

    for nodeid in expected:
        phases = nodes[nodeid]
        for phase in ("setup", "teardown"):
            entry = phases.get(phase) or {}
            if entry.get("outcome") != "passed":
                return (
                    "error_failure",
                    f"{phase} did not pass for {nodeid!r} "
                    f"(outcome={entry.get('outcome')!r})",
                    observed,
                )
        call = phases.get("call") or {}
        outcome = call.get("outcome")
        if outcome == "skipped":
            return ("skipped", f"{nodeid!r} was skipped or xfailed", observed)
        if outcome == "passed":
            observed[nodeid] = "pass"
        elif outcome == "failed":
            if call.get("exception_is_assertion") is True:
                observed[nodeid] = "assertion_failure"
            else:
                return (
                    "error_failure",
                    f"{nodeid!r} failed with "
                    f"{call.get('exception_type') or 'an unknown exception'}",
                    observed,
                )
        else:  # pragma: no cover - pytest always reports one of the three
            return ("malformed_report", f"unknown call outcome {outcome!r}", observed)

    failing = [nodeid for nodeid, outcome in observed.items() if outcome != "pass"]
    if returncode == 0 and failing:
        return (
            "contradictory_exit",
            "pytest exited 0 while its report shows a failing node",
            observed,
        )
    if returncode == 1 and not failing:
        return (
            "contradictory_exit",
            "pytest exited 1 while its report shows no failing node",
            observed,
        )
    if returncode not in (0, 1):
        return (
            "malformed_report",
            f"unexpected pytest exit status {returncode}",
            observed,
        )
    return ("ok", "", observed)


def _run_side(
    *,
    repo: Path,
    side: str,
    revision: str,
    spec: SuiteSpec,
    work_root: Path,
    out: Path,
    timeout_s: int,
) -> dict:
    logs = out / "logs"
    harness = logs / "harness" / side
    checkout = work_root / side
    record: dict = {
        "requested_sha": revision,
        "resolved_sha": None,
        "worktree": str(checkout),
        "head_before": None,
        "head_after": None,
        "preflight": None,
        "command": None,
        "exit_code": None,
        "duration_s": None,
        "timed_out": False,
        "report": None,
        "stdout": None,
        "stderr": None,
        "status": "pending",
        "reason": "",
        "detail": "",
        "nodes": {},
    }
    error = _worktree_add(repo, checkout, revision, logs / f"{side}.worktree.log")
    if error:
        record.update(status="checkout_failed", reason="checkout_failed", detail=error)
        return record
    head = _run_git(checkout, ["rev-parse", "HEAD"])
    record["head_before"] = head.stdout.strip()
    if head.returncode != 0 or record["head_before"] != revision:
        record.update(
            status="checkout_failed",
            reason="revision_mismatch",
            detail="checkout HEAD does not equal the requested commit",
        )
        return record

    info, reason, detail = _preflight(checkout, harness, logs / f"{side}.preflight.log")
    record["preflight"] = info
    if reason:
        record.update(status=reason, reason=reason, detail=detail)
        return record

    nonce = hashlib.sha256(os.urandom(32)).hexdigest()
    phase_report = logs / f"{side}.phase.json"
    child_spec = logs / f"{side}.spec.json"
    plugin_path = _plugin_path()
    plugin_sha = sha256_file(plugin_path)
    child_spec.write_text(
        json.dumps(
            {
                "checkout": str(checkout),
                "plugin_path": str(plugin_path),
                "report_path": str(phase_report),
                "nonce": nonce,
                "pytest_args": _pytest_arguments(spec, harness),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    command = [
        sys.executable,
        str(_engine_directory() / BOOTSTRAP_FILENAME),
        str(child_spec),
    ]
    record["command"] = command
    outcome = _run_process(
        command,
        cwd=spec.directory,
        env=_child_environment(checkout, harness),
        timeout_s=timeout_s,
        stdout_path=logs / f"{side}.stdout.log",
        stderr_path=logs / f"{side}.stderr.log",
    )
    record.update(
        exit_code=outcome.returncode,
        duration_s=outcome.duration_s,
        timed_out=outcome.timed_out,
        stdout=sha256_file(outcome.stdout_path),
        stderr=sha256_file(outcome.stderr_path),
    )

    after = _run_git(checkout, ["rev-parse", "HEAD"])
    record["head_after"] = after.stdout.strip()
    if after.returncode != 0 or record["head_after"] != revision:
        record.update(
            status="revision_mismatch",
            reason="revision_mismatch",
            detail="checkout HEAD moved during the replay",
        )
        return record
    if outcome.timed_out:
        record.update(
            status="timeout",
            reason="timeout",
            detail=f"execution exceeded {timeout_s}s; its process group was killed",
        )
        return record
    if not phase_report.is_file():
        record.update(
            status="malformed_report",
            reason="malformed_report",
            detail="no phase report was written (the run crashed or was killed)",
        )
        return record
    if phase_report.stat().st_size > REPORT_MAX_BYTES:
        record.update(
            status="malformed_report",
            reason="malformed_report",
            detail="phase report exceeds the size bound",
        )
        return record
    try:
        report = parse_strict_json(phase_report.read_bytes())
    except ValueError as exc:
        record.update(
            status="malformed_report",
            reason="malformed_report",
            detail=f"phase report is not strict JSON: {exc}",
        )
        return record
    problem = _report_problem(
        report,
        nonce=nonce,
        checkout=checkout,
        plugin_sha=plugin_sha,
        returncode=outcome.returncode,
    )
    record["report"] = {
        "path": str(phase_report),
        "sha256": sha256_file(phase_report),
    }
    if problem:
        record.update(status=problem[0], reason=problem[0], detail=problem[1])
        return record
    assert isinstance(report, dict)  # narrowed by _report_problem
    expected = [spec.target["nodeid"], *(c["nodeid"] for c in spec.controls)]
    status, reason, observed = _classify_side(
        report=report,
        returncode=outcome.returncode,
        expected=expected,
        target=spec.target["nodeid"],
    )
    record["nodes"] = _node_outcomes(report)
    record["collected"] = list(report.get("collected") or [])
    record["collection_errors"] = list(report.get("collection_errors") or [])
    record["status"] = status
    record["reason"] = reason
    record["detail"] = reason
    record["observed"] = observed
    return record


def _write_replay_report(out: Path, payload: dict) -> Path:
    path = out / REPORT_FILENAME
    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(blob)
    (out / REPORT_SIDECAR_FILENAME).write_text(
        f"{hashlib.sha256(blob).hexdigest()}  {REPORT_FILENAME}\n"
    )
    return path


def _decide(sides: dict, spec: SuiteSpec) -> tuple[str, str, str]:
    """Verdict from the authoritative per-side reports; never from stdout."""
    for side in _SIDE_NAMES:
        status = sides[side]["status"]
        if status == "ok":
            continue
        if status == "checkout_failed":
            reason = sides[side]["reason"]
            return (
                VERDICT_REJECTED
                if reason == "revision_mismatch"
                else VERDICT_INCONCLUSIVE,
                reason,
                sides[side]["detail"],
            )
        if status == "timeout":
            return (VERDICT_INCONCLUSIVE, "timeout", "a side exceeded its timeout")
        if status in _STRUCTURAL_REASONS:
            return (
                VERDICT_INCONCLUSIVE,
                status,
                f"{side}: {sides[side]['detail']}",
            )
        return (VERDICT_INCONCLUSIVE, status, sides[side]["detail"])

    target = spec.target["nodeid"]
    baseline_target = sides["baseline"]["observed"].get(target)
    candidate_target = sides["candidate"]["observed"].get(target)
    for control in spec.controls:
        nodeid = control["nodeid"]
        for side in _SIDE_NAMES:
            if sides[side]["observed"].get(nodeid) != CONTROL_OUTCOME:
                return (
                    VERDICT_NOT_REPRODUCED,
                    "control_mismatch",
                    f"control {nodeid!r} did not pass on {side}",
                )
    if baseline_target == TARGET_BASELINE_OUTCOME and candidate_target == (
        TARGET_CANDIDATE_OUTCOME
    ):
        return (
            VERDICT_REPRODUCED,
            "assertion_failure_to_pass",
            "the target failed with AssertionError on baseline and passed on candidate",
        )
    if baseline_target == "pass" and candidate_target == "pass":
        return (
            VERDICT_NOT_REPRODUCED,
            "both_pass",
            "both revisions passed the target; a shared pass is not improvement",
        )
    if baseline_target != TARGET_BASELINE_OUTCOME:
        return (
            VERDICT_NOT_REPRODUCED,
            "target_baseline_mismatch",
            f"baseline target outcome was {baseline_target!r}, expected "
            f"{TARGET_BASELINE_OUTCOME!r}",
        )
    return (
        VERDICT_NOT_REPRODUCED,
        "target_candidate_mismatch",
        f"candidate target outcome was {candidate_target!r}, expected "
        f"{TARGET_CANDIDATE_OUTCOME!r}",
    )


def _execute(
    *,
    repo: Path,
    baseline: str,
    candidate: str,
    spec: SuiteSpec,
    work_root: Path,
    out: Path,
    timeout_s: int,
) -> ReplayResult:
    digest_before = compute_suite_digest(spec.directory)
    sources = {
        "repo": str(repo),
        "baseline": {"requested_sha": baseline, "resolved_sha": baseline},
        "candidate": {"requested_sha": candidate, "resolved_sha": candidate},
    }
    if digest_before != spec.digest:
        payload = _base_report(
            repo=repo,
            spec=spec,
            sources=sources,
            suite={
                "suite_id": spec.suite_id,
                "digest": spec.digest,
                "expected_digest": spec.digest,
                "digest_before": digest_before,
                "digest_after_baseline": None,
                "digest_after_candidate": None,
                "digest_after": digest_before,
                "files": [],
            },
            sides={},
            work_root=work_root,
            out=out,
            timeout_s=timeout_s,
            verdict=VERDICT_REJECTED,
            reason="suite_modified",
            detail="the suite changed before execution",
            checks=[],
        )
        path = _write_replay_report(out, payload)
        return ReplayResult(
            VERDICT_REJECTED, "suite_modified", payload["detail"], payload, str(path)
        )

    sides: dict[str, dict] = {}
    digests: dict[str, str | None] = {
        "digest_before": digest_before,
        "digest_after_baseline": None,
        "digest_after_candidate": None,
    }
    post_rejection: tuple[str, str] | None = None
    for side, revision in (("baseline", baseline), ("candidate", candidate)):
        sides[side] = _run_side(
            repo=repo,
            side=side,
            revision=revision,
            spec=spec,
            work_root=work_root,
            out=out,
            timeout_s=timeout_s,
        )
        digests[f"digest_after_{side}"] = compute_suite_digest(spec.directory)
        if digests[f"digest_after_{side}"] != spec.digest:
            post_rejection = (
                "suite_modified",
                f"the suite changed during the {side} execution",
            )
            break

    digest_after = compute_suite_digest(spec.directory)
    if post_rejection is None and digest_after != spec.digest:
        post_rejection = ("suite_modified", "the suite changed after execution")
    if post_rejection is None:
        for side, revision in (("baseline", baseline), ("candidate", candidate)):
            check = _run_git(repo, ["rev-parse", f"{revision}^{{commit}}"])
            if check.returncode != 0 or check.stdout.strip() != revision:
                post_rejection = (
                    "revision_mismatch",
                    f"requested revision {revision} no longer resolves to itself",
                )

    checks = _checks(sides=sides, spec=spec, digests=digests, digest_after=digest_after)
    if post_rejection is not None:
        verdict, reason = VERDICT_REJECTED, post_rejection[0]
        detail = post_rejection[1]
        sides.setdefault("candidate", {"status": "skipped_after_rejection"})
    else:
        verdict, reason, detail = _decide(sides, spec)

    payload = _base_report(
        repo=repo,
        spec=spec,
        sources=sources,
        suite={
            "suite_id": spec.suite_id,
            "digest": spec.digest,
            "expected_digest": spec.digest,
            "digest_before": digests["digest_before"],
            "digest_after_baseline": digests["digest_after_baseline"],
            "digest_after_candidate": digests["digest_after_candidate"],
            "digest_after": digest_after,
            "files": [
                {"name": path.name, "sha256": sha256_file(path)} for path in spec.files
            ],
        },
        sides=sides,
        work_root=work_root,
        out=out,
        timeout_s=timeout_s,
        verdict=verdict,
        reason=reason,
        detail=detail,
        checks=checks,
    )
    path = _write_replay_report(out, payload)
    return ReplayResult(verdict, reason, detail, payload, str(path))


def _checks(
    *, sides: dict, spec: SuiteSpec, digests: dict, digest_after: str
) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, reason: str = "") -> None:
        checks.append(
            {"name": name, "ok": ok, **({"reason": reason} if reason else {})}
        )

    add("suite_digest_expected", digests["digest_before"] == spec.digest)
    add(
        "suite_digest_after_baseline",
        digests["digest_after_baseline"] == spec.digest
        if digests.get("digest_after_baseline") is not None
        else False,
    )
    add(
        "suite_digest_after_candidate",
        digests["digest_after_candidate"] == spec.digest
        if digests.get("digest_after_candidate") is not None
        else False,
    )
    add("suite_digest_after", digest_after == spec.digest)
    for side in _SIDE_NAMES:
        record = sides.get(side) or {}
        add(
            f"{side}_checkout", record.get("head_before") == record.get("requested_sha")
        )
        add(
            f"{side}_head_stable",
            record.get("head_after") == record.get("requested_sha"),
        )
        add(f"{side}_preflight", record.get("preflight") is not None)
        add(
            f"{side}_phase_report",
            record.get("status") in {"ok", *sorted(_STRUCTURAL_REASONS), "timeout"}
            and record.get("report") is not None,
        )
    target = spec.target["nodeid"]
    for side in _SIDE_NAMES:
        observed = (sides.get(side) or {}).get("observed") or {}
        expected = (
            TARGET_BASELINE_OUTCOME if side == "baseline" else TARGET_CANDIDATE_OUTCOME
        )
        add(f"{side}_target", observed.get(target) == expected)
    for control in spec.controls:
        for side in _SIDE_NAMES:
            observed = (sides.get(side) or {}).get("observed") or {}
            add(
                f"{side}_control_{control['nodeid']}",
                observed.get(control["nodeid"]) == CONTROL_OUTCOME,
            )
    return checks


def _base_report(
    *,
    repo: Path,
    spec: SuiteSpec,
    sources: dict,
    suite: dict,
    sides: dict,
    work_root: Path,
    out: Path,
    timeout_s: int,
    verdict: str,
    reason: str,
    detail: str,
    checks: list[dict],
) -> dict:
    plugin = _plugin_path()
    return {
        "schema": {"name": REPORT_SCHEMA_NAME, "version": REPORT_SCHEMA_VERSION},
        "promotional": False,
        "acceptance_evidence": False,
        "verdict": verdict,
        "reason": reason,
        "detail": detail,
        "engine": {
            "python": sys.executable,
            "python_version": sys.version.split()[0],
            "replay_module": str(Path(__file__).resolve()),
            "replay_module_sha256": sha256_file(Path(__file__).resolve()),
            "plugin_path": str(plugin),
            "plugin_sha256": sha256_file(plugin),
        },
        "sources": sources,
        "suite": suite,
        "expectations": {"target": spec.target, "controls": spec.controls},
        "sides": sides,
        "work_root": str(work_root),
        "out": str(out),
        "timeout_s": timeout_s,
        "checks": checks,
        "limits": list(_LIMITS),
    }
