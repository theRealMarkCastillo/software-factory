"""Coverage gate for the Software Factory.

A pre-merge gate that refuses to advance when newly added executable lines
are not covered by the repo's test suite.

Unlike the audit and data-model gates, which validate existing *state*, this
gate is a *delta* gate: it measures only the lines a change actually added
relative to a base git ref, and requires every one of those lines that is
executable to be covered. It is deliberately strict:

- it RUNS the repo's test suite under coverage (pytest --cov --cov-branch
  --cov-report=json) rather than trusting a pre-existing report, so the
  measured data always reflects the exact revision under review;
- if the coverage tooling is missing the gate FAILS with an install
  instruction -- it never silently passes because measurement was skipped;
- a file deliberately excluded from measurement by an omit pattern is treated
  as an attempt to bypass the gate and flagged as a FAILURE;
- a changed file with no recorded coverage data at all is a FAILURE
  (coverage data missing or unmeasurable);
- changed files classified as test files (default: tests/**, test_*.py,
  *_test.py; overridable via [tool.factory.coverage] test_file_patterns in
  pyproject.toml) are excluded from the coverage lookup and pass/fail
  decision, but still appear in the result 'files' list with
  skipped_reason: test_file. A test-only diff therefore passes without
  running the suite or demanding coverage data -- the same no-run logic as
  a diff with no changed python lines at all;
- anti-bypass: a test_file pattern matching a file that coverage DID record
  data for does NOT skip that file -- it is gated like any other measured
  source file, so a mis-scoped pattern can never silently exempt measured
  code from gating.

Like factory.audit and factory.datamodel, this module returns a flat result
object and never raises on non-policy conditions, so the orchestrator can read
the outcome without exception handling. The CLI exits 0 on pass, 1 on fail.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Comment marker that waives a specific line from the coverage requirement.
# The waiver must appear on the SAME line as the code it waives.
PRAGMA_MARKER = "# pragma: no cover"

# Coverage config files, in coverage.py's resolution order, that may carry an
# [omit] list. Each holds a glob pattern (or comma-separated globs) that
# coverage.py uses to skip files during measurement.
COVERAGE_CONFIG_FILES = (".coveragerc", "setup.cfg", "pyproject.toml")

# The interpreter used to run the repo's test suite. Defaults to the
# interpreter executing this module; overridable via env so operators can
# pin a specific venv without editing code (mirrors FACTORY_PIP_AUDIT).
INTERPRETER_ENV = "FACTORY_COVERAGE_INTERPRETER"

# Conservative default classification of test files: only paths under a
# tests/ directory or files named test_*.py / *_test.py are treated as test
# files. These patterns can never match typical production modules (app.py,
# factory/coverage.py, src/*.py), so production source is always gated.
DEFAULT_TEST_FILE_PATTERNS = ("tests/**", "test_*.py", "*_test.py")

TEST_CMD = ["-m", "pytest", "--cov", "--cov-branch", "--cov-report=json"]
TEST_TIMEOUT_S = 600

_INSTALL_HINT = (
    "Install coverage tooling (pytest + pytest-cov): "
    "<interpreter> -m pip install pytest pytest-cov"
)


@dataclass
class CoverageGateResult:
    status: str  # "pass" | "fail"
    passed: bool
    base: str = ""
    files: list = field(default_factory=list)
    waivers: list = field(default_factory=list)
    detail: str = ""
    test_exit_code: int | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "passed": self.passed,
            "base": self.base,
            "files": self.files,
            "waivers": self.waivers,
            "detail": self.detail,
            "test_exit_code": self.test_exit_code,
        }


def _fail(detail: str, **kw) -> CoverageGateResult:
    kw["status"] = "fail"
    kw["passed"] = False
    kw["detail"] = detail
    return CoverageGateResult(**kw)


def _pass_result(
    base: str,
    files: list,
    waivers: list,
    detail: str,
    test_exit_code: int | None = None,
) -> CoverageGateResult:
    return CoverageGateResult(
        status="pass",
        passed=True,
        base=base,
        files=files,
        waivers=waivers,
        detail=detail,
        test_exit_code=test_exit_code,
    )


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git(repo: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run git in `repo`, never raising on non-policy conditions."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - host without git
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f"git unavailable: {exc}"
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - pathological
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="git timed out")


def _ref_exists(repo: Path, ref: str) -> bool:
    proc = _git(repo, ["rev-parse", "--verify", "--quiet", ref])
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _git_toplevel(repo: Path) -> tuple[Path | None, str]:
    """Return the git worktree toplevel containing `repo`, or (None, reason)."""
    proc = _git(repo, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None, (proc.stderr or "git rev-parse --show-toplevel failed").strip()
    out = proc.stdout.strip()
    if not out:
        return None, "git rev-parse --show-toplevel returned no path"
    return Path(out).resolve(), ""


def resolve_base(repo: Path, base: str | None) -> str:
    """Resolve the diff base: explicit arg wins; else origin/main if it
    exists; else HEAD~1; else the empty tree (an initial-commit diff)."""
    if base:
        return base
    if _ref_exists(repo, "origin/main"):
        return "origin/main"
    if _ref_exists(repo, "HEAD~1"):
        return "HEAD~1"
    # Root commit has no parent; diff against the empty tree so the whole
    # initial file set counts as added.
    empty = _git(repo, ["hash-object", "-t", "tree", "/dev/null"])
    return empty.stdout.strip() if empty.returncode == 0 else "HEAD"


def _diff_added_lines(
    repo: Path, base: str
) -> tuple[dict[str, dict[int, str]] | None, str]:
    """Compare the actual working tree with base, including untracked Python.

    Returns (added, err) where `added` maps a repo-relative .py path to a dict
    of {new-file line number: source text} for every line the change added.
    Returns (None, reason) if the diff cannot be produced.
    """
    proc = _git(repo, ["diff", "--unified=0", base, "--", "*.py"])
    if proc.returncode != 0:
        return None, (proc.stderr or "git diff failed").strip()

    added: dict[str, dict[int, str]] = {}
    current: str | None = None
    new_line: int = 0
    in_hunk = False
    for raw in proc.stdout.splitlines():
        if raw.startswith("+++ "):
            # --- a/x.py / +++ b/x.py header: grab the NEW path (may be /dev/null).
            path = raw[4:]
            if path == "/dev/null":
                current = None
                continue
            # git names rename/new-file targets as `b/<path>`.
            resolved: str = path[2:] if path.startswith("b/") else path.lstrip("./")
            current = resolved
            added.setdefault(resolved, {})
            new_line = 0
            in_hunk = False
            continue
        if raw.startswith("@@ "):
            # @@ -a,b +c,d @@  (--unified=0 gives no context, counts reflect change).
            match = raw.split(" ")
            for token in match:
                if token.startswith("+"):
                    num = token[1:].split(",")[0]
                    if num.lstrip("-").isdigit():
                        new_line = int(num)
                        break
            in_hunk = True
            continue
        if not in_hunk or current is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added[current][new_line] = raw[1:]
            new_line += 1
        elif raw.startswith("-"):
            continue  # removed line: does not advance the new-file counter
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            # Context line (space prefix) -- only with -U>0, but keep it safe.
            new_line += 1
    untracked = _git(
        repo,
        [
            "ls-files",
            "--others",
            "--exclude-standard",
            "--full-name",
            "-z",
            "--",
            "*.py",
        ],
    )
    if untracked.returncode != 0:
        return None, (
            untracked.stderr or "could not enumerate untracked Python"
        ).strip()
    names = [name for name in untracked.stdout.split("\0") if name]
    if names:
        root, error = _git_toplevel(repo)
        if root is None:
            return None, error
        for name in names:
            path = root / name
            if not path.resolve().is_relative_to(repo):
                return None, f"untracked Python path escapes subject repo: {name}"
            try:
                added[name] = dict(enumerate(path.read_text().splitlines(), start=1))
            except (OSError, UnicodeError) as exc:
                return None, f"could not read untracked Python {name}: {exc}"
    return added, ""


# ---------------------------------------------------------------------------
# coverage tooling
# ---------------------------------------------------------------------------


def _interpreter() -> str:
    """Interpreter used to run the repo tests (env override else sys.executable)."""
    return os.environ.get(INTERPRETER_ENV) or sys.executable


def _missing_tooling(interpreter: str) -> str | None:
    """Return an install instruction if pytest/pytest-cov/coverage cannot be
    imported by `interpreter`, else None."""
    code = (
        "import importlib.util as u;"
        "print(','.join(m for m in ('pytest','pytest_cov','coverage') if not u.find_spec(m)))"
    )
    try:
        proc = subprocess.run(
            [interpreter, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return f"test interpreter not executable: {interpreter}"
    except subprocess.TimeoutExpired:
        return "test interpreter check timed out"
    if proc.returncode != 0:
        return _INSTALL_HINT
    missing = [m for m in (proc.stdout or "").strip().split(",") if m]
    if missing:
        return f"missing coverage tooling: {', '.join(missing)}. {_INSTALL_HINT}"
    return None


def _run_tests(repo: Path, interpreter: str) -> tuple[int, str]:
    """Run the repo suite with coverage. Returns (exit_code, err_or_empty)."""
    cmd = [interpreter, *TEST_CMD]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_S,
            check=False,
        )
    except OSError as exc:
        return -1, f"could not execute test runner {interpreter}: {exc}"
    except subprocess.TimeoutExpired:
        return -1, f"test suite timed out after {TEST_TIMEOUT_S}s"
    err = (proc.stderr or "").strip()
    return proc.returncode, err[:400] if err else ""


def _load_coverage_json(repo: Path) -> tuple[dict | None, str]:
    """Read repo/coverage.json produced by pytest-cov."""
    path = repo / "coverage.json"
    if not path.is_file():
        return None, "no coverage.json produced by the test run"
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"could not parse coverage.json: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None, "coverage.json has no 'files' section"
    return data, ""


# ---------------------------------------------------------------------------
# omit-pattern detection
# ---------------------------------------------------------------------------


def _load_omit_patterns(repo: Path) -> list[str]:
    """Collect [omit] patterns from coverage config files, if any.

    coverage.py reads .coveragerc / setup.cfg [coverage:run] /
    pyproject.toml [tool.coverage.run]; each may list glob patterns under
    'omit'. This gate only cares about the *omit* list -- including a file to
    measure it is fine; omitting a changed file is what would let the gate
    pass while the code is uncovered.
    """
    patterns: list[str] = []
    for name in COVERAGE_CONFIG_FILES:
        path = repo / name
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError:  # pragma: no cover - defensive
            continue
        if name == ".coveragerc":
            patterns += _omit_from_section(text, "run", "[run]")
        elif name == "setup.cfg":
            patterns += _omit_from_section(text, "coverage:run", "[coverage:run]")
        elif name == "pyproject.toml":
            patterns += _omit_from_pyproject(text)
    return [p for p in patterns if p]


def _omit_from_section(text: str, section: str, section_header: str) -> list[str]:
    """Extract the 'omit' key (comma or newline separated) from a config section."""
    lines = text.splitlines()
    out: list[str] = []
    active = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            active = stripped.lower() == section_header.lower()
            continue
        if not active:
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().lower() != "omit":
            continue
        value = value.strip()
        # .coveragerc allows comma-separated values; also each on its own line.
        for part in value.split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _omit_from_pyproject(text: str) -> list[str]:
    """Very small INI-style scan for [tool.coverage.run] omit= in pyproject.toml."""
    out: list[str] = []
    active = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[tool.coverage"):
            active = "run" in stripped
            continue
        if stripped.startswith("["):
            active = False
            continue
        if not active or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().lower() != "omit":
            continue
        for part in value.strip().split(","):
            part = part.strip().strip('"')
            if part:
                out.append(part)
    return out


def _matches_omit(relpath: str, patterns: list[str]) -> str | None:
    """Return the first omit pattern matching `relpath`, else None."""
    for pattern in patterns:
        if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(
            relpath, f"*/{pattern}"
        ):
            return pattern
    return None


def is_test_file(relpath: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """True when `relpath` is classified as a test file by `patterns`.

    Matching mirrors _matches_omit: the pattern must match the repo-relative
    path directly or any trailing segment of it (so 'tests/**' matches
    'src/tests/x.py' too, and 'test_*.py' matches at any depth).
    """
    for pattern in patterns:
        if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(
            relpath, f"*/{pattern}"
        ):
            return True
    return False


def _load_test_file_patterns(repo: Path) -> list[str]:
    """Read coverage.test_file_patterns from [tool.factory.coverage] in
    pyproject.toml, if the repo declares them; else the conservative
    DEFAULT_TEST_FILE_PATTERNS.

    Accepts an inline list ("a", "b") / comma-separated string, or a
    multi-line list with one quoted pattern per line, mirroring the tolerant
    INI-style parsing used for coverage omit patterns.
    """
    path = repo / "pyproject.toml"
    if not path.is_file():
        return list(DEFAULT_TEST_FILE_PATTERNS)
    try:
        text = path.read_text()
    except OSError:  # pragma: no cover - defensive
        return list(DEFAULT_TEST_FILE_PATTERNS)
    active = False
    collecting = False
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            active = stripped.lower() == "[tool.factory.coverage]"
            collecting = False
            continue
        if not active:
            continue
        if collecting:
            if stripped.startswith("]"):
                collecting = False
                continue
            for part in stripped.strip(",").split(","):
                part = part.strip().strip('"').strip("'")
                if part:
                    out.append(part)
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().lower() != "test_file_patterns":
            continue
        value = value.strip()
        if value.startswith("["):
            inner = value[1:]
            if inner.strip().endswith("]"):
                inner = inner.strip()[:-1]
                for part in inner.split(","):
                    part = part.strip().strip('"').strip("'")
                    if part:
                        out.append(part)
            else:
                collecting = True
            continue
        for part in value.split(","):
            part = part.strip().strip('"').strip("'")
            if part:
                out.append(part)
    return out if out else list(DEFAULT_TEST_FILE_PATTERNS)


# ---------------------------------------------------------------------------
# waiver scan
# ---------------------------------------------------------------------------


def _has_pragma(source: str) -> bool:
    """True when the waiver marker appears inline on this line's source."""
    return PRAGMA_MARKER in source


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def run_coverage_gate(
    repo_path: str, base: str | None = None, *, require_fresh: bool = False
) -> CoverageGateResult:
    """Enforce the coverage delta gate on `repo_path` against `base`.

    Returns a CoverageGateResult; never raises on non-policy conditions.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        return _fail(f"no repo at {repo}")

    interpreter = _interpreter()
    tool_missing = _missing_tooling(interpreter)
    if tool_missing:
        return _fail(f"coverage tooling unavailable: {tool_missing}")

    # Never consume a report left by a previous run. The test command below
    # must create the report that this gate evaluates.
    stale_report = repo / "coverage.json"
    if require_fresh:
        try:
            stale_report.unlink(missing_ok=True)
        except OSError as exc:
            return _fail(f"could not remove stale coverage report: {exc}")

    base_ref = resolve_base(repo, base)
    added, err = _diff_added_lines(repo, base_ref)
    if added is None:
        return _fail(
            f"could not compute diff against {base_ref!r}: {err}", base=base_ref
        )

    # Only files with at least one added line are in scope.
    changed = {f: lines for f, lines in added.items() if lines}
    if not changed:
        return _pass_result(
            base_ref,
            [],
            [],
            f"no added python lines to gate against {base_ref}",
        )

    # Omit patterns are checked BEFORE running tests: an omit pattern touching
    # a changed file is a gate bypass regardless of measured numbers.
    omit_patterns = _load_omit_patterns(repo)
    bypass_failures: list[str] = []
    for f in changed:
        pattern = _matches_omit(f, omit_patterns)
        if pattern:
            bypass_failures.append(
                f"changed file '{f}' is excluded from coverage measurement by "
                f"omit pattern '{pattern}' -- excluding a file to pass the gate is a FAILURE"
            )
    if bypass_failures:
        return _fail("; ".join(bypass_failures), base=base_ref)

    # Classify test files: they carry no production executable lines, so they
    # are excluded from the coverage lookup and pass/fail decision. A
    # test-only diff passes without demanding coverage data ,
    # but the suite still runs whenever any file changed -- consistent with
    # the base gate's run/no-run logic -- so the anti-bypass below can detect
    # a mis-scoped test_file_pattern over measured production code.
    test_patterns = _load_test_file_patterns(repo)
    candidate_test_files = sorted(f for f in changed if is_test_file(f, test_patterns))
    source_files = {
        f: lines for f, lines in changed.items() if f not in candidate_test_files
    }

    test_exit, _ = _run_tests(repo, interpreter)
    cov, cov_err = _load_coverage_json(repo)
    files_data = cov["files"] if cov else {}

    # Nested-repo path resolution: when `repo` is a subdirectory of a larger
    # git worktree (e.g. a templates/ dir inside the factory repo), diff
    # paths are relative to the git toplevel while coverage.json keys are
    # relative to the test run's cwd (`repo`). Alias stripped keys so both
    # flat repos (key == diff path) and nested repos (key == stripped diff
    # path) resolve.
    git_root, _root_err = _git_toplevel(repo)
    if git_root is not None and repo != git_root:
        prefix = repo.relative_to(git_root).as_posix() + "/"
        alias = dict(files_data)
        for key, entry in files_data.items():
            alias.setdefault(prefix + key, entry)
        files_data = alias

    # Anti-bypass: a test_file pattern only *skips* a file that coverage did
    # NOT measure. A test-classified file that coverage DID record data for
    # (e.g. an unscoped coverage config measures the test suite itself, or a
    # repo widened test_file_patterns to cover production) is gated like any
    # other measured file, so a mis-scoped pattern can never silently exempt
    # code from the pass/fail decision. This check runs even for a test-only
    # diff, so the skip decision is never made without inspecting coverage.
    skipped_entries = [
        {"file": f, "added_lines": len(changed[f]), "skipped_reason": "test_file"}
        for f in candidate_test_files
        if f not in files_data
    ]
    measured_test_files = {
        f: changed[f] for f in candidate_test_files if f in files_data
    }

    # A diff whose changed files are all genuine test files (none measured)
    # passes even when coverage.json is absent or scoped away from them.
    # a passing coverage verdict is meaningless when the suite
    # itself failed -- fail closed on nonzero test exit in both paths.
    if test_exit != 0:
        return _fail(
            f"test suite exited nonzero ({test_exit}) -- coverage gate fails closed",
            base=base_ref,
            files=skipped_entries,
            test_exit_code=test_exit,
        )

    if not source_files and not measured_test_files:
        return _pass_result(
            base_ref,
            skipped_entries,
            [],
            "only test files changed; no production coverage data required",
            test_exit_code=test_exit,
        )

    if cov is None:
        return _fail(
            f"coverage data missing: {cov_err} (test run exited {test_exit})",
            base=base_ref,
            test_exit_code=test_exit,
        )

    table: list[dict] = list(skipped_entries)
    waivers: list[dict] = []
    failures: list[str] = []

    for f in sorted({**source_files, **measured_test_files}):
        lines = changed[f]
        entry = files_data.get(f)
        if entry is None:
            failures.append(
                f"no coverage data recorded for changed file '{f}' "
                f"(coverage data missing or unmeasurable)"
            )
            table.append(
                {
                    "file": f,
                    "added_lines": len(lines),
                    "covered_pct": 0.0,
                    "missed_lines": sorted(lines),
                }
            )
            continue

        executed = set(entry.get("executed_lines") or [])
        missing = set(entry.get("missing_lines") or [])
        executable = executed | missing

        # Collect inline waivers on the added lines of this file.
        file_waivers = [n for n, src in lines.items() if _has_pragma(src)]
        waivers.extend(
            {"file": f, "line": n, "text": lines[n].strip()} for n in file_waivers
        )
        waived = set(file_waivers)

        added_executable = [n for n in lines if n in executable and n not in waived]
        missed = sorted(n for n in added_executable if n in missing)
        covered = len(added_executable) - len(missed)
        pct = (covered / len(added_executable) * 100.0) if added_executable else 100.0
        table.append(
            {
                "file": f,
                "added_lines": len(lines),
                "covered_pct": round(pct, 1),
                "missed_lines": missed,
            }
        )
        if missed:
            failures.append(
                f"uncovered added executable line(s) {missed} in '{f}' "
                f"(add a test that executes them, or an inline "
                f"'{PRAGMA_MARKER}' on the same line)"
            )

    if failures:
        return _fail(
            "; ".join(failures),
            base=base_ref,
            waivers=waivers,
            files=table,
            test_exit_code=test_exit,
        )
    return _pass_result(
        base_ref,
        table,
        waivers,
        "all added executable lines are covered",
        test_exit_code=test_exit,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the factory Coverage Gate on a repo."
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--base",
        default=None,
        help="git ref to diff added lines against (default: origin/main if present, else HEAD~1)",
    )
    args = parser.parse_args(argv)

    result = run_coverage_gate(args.repo, base=args.base)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
