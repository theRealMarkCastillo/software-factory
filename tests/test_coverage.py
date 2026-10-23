"""Unit tests for factory.coverage.

Mock subprocess so tests are deterministic (no real git, no real pytest run,
no real coverage measurement), matching the synthetic-fixture style of
test_audit.py. The gate's git plumbing, tooling check, and test run all go
through subprocess.run, so a single fake dispatcher controls them all.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import factory.coverage as cov_gate
from factory.evidence import write_evidence_manifest


class FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _diff(fname: str, added_lines: list[str]) -> str:
    """Build a `git diff --unified=0` for a brand-new file with the given lines.

    The hunk header `@@ -0,0 +1,<n> @@` means the new file starts at line 1
    and spans <n> lines, so added line numbers run 1..<n>.
    """
    lines = [
        f"diff --git a/{fname} b/{fname}",
        "new file mode 100644",
        "index 0000000..1111111",
        "--- /dev/null",
        f"+++ b/{fname}",
        f"@@ -0,0 +1,{len(added_lines)} @@",
    ]
    lines += [f"+{ln}" for ln in added_lines]
    return "\n".join(lines) + "\n"


class FakeRunner:
    """Dispatches every subprocess.run the gate makes."""

    def __init__(self, diff="", missing="", pytest_exit=0, refs=(), toplevel=""):
        self.diff = diff
        self.missing = missing  # stdout of the tooling check
        self.pytest_exit = pytest_exit
        self.refs = set(refs)
        self.toplevel = toplevel  # stdout of `git rev-parse --show-toplevel`

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "git":
            if cmd[1:3] == ["rev-parse", "--verify"]:
                # git rev-parse --verify [--quiet] <ref>
                ref = cmd[4] if len(cmd) > 4 and cmd[3] == "--quiet" else cmd[3]
                ok = ref in self.refs
                return FakeProc(0 if ok else 1, stdout="abc\n" if ok else "")
            if cmd[1:3] == ["rev-parse", "--show-toplevel"]:
                return FakeProc(0, stdout=self.toplevel)
            if cmd[1] == "hash-object":
                return FakeProc(0, stdout="<emptytree>\n")
            if cmd[1] == "diff":
                return FakeProc(0, stdout=self.diff)
            return FakeProc(0, stdout="")
        if cmd[1] == "-c":  # tooling presence check
            return FakeProc(0, stdout=self.missing)
        return FakeProc(self.pytest_exit, stdout="", stderr="")  # pytest run


def _repo(tmp_path, files=None, cov=None):
    """A tmp repo dir with optional coverage.json + config files on disk."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in (files or {}).items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if cov is not None:
        (repo / "coverage.json").write_text(json.dumps({"files": cov}))
    return repo


def _gate(repo, fake, base=None):
    fake_runner = fake if isinstance(fake, FakeRunner) else FakeRunner(**fake)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cov_gate.subprocess, "run", fake_runner)
    try:
        return cov_gate.run_coverage_gate(str(repo), base=base)
    finally:
        monkeypatch.undo()


APP_LINES = [
    "def add(a, b):",
    "    return a + b",
    "",
]


# --- pass / fail core -------------------------------------------------------


def test_all_added_lines_covered_passes(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",)))
    assert r.status == "pass"
    assert r.passed is True
    assert r.base == "origin/main"
    assert r.files[0]["file"] == "app.py"
    assert r.files[0]["added_lines"] == 3
    assert r.files[0]["covered_pct"] == 100.0
    assert r.files[0]["missed_lines"] == []


def test_uncovered_added_executable_line_fails(tmp_path):
    lines = APP_LINES + [
        "",
        "def unused():",
        "    return 1",
    ]
    # Line 6 ("    return 1") is executable and uncovered; the def line is not
    # a coverage statement.
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": [6]}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", lines), refs=("origin/main",)))
    assert r.status == "fail"
    assert r.passed is False
    assert r.files[0]["missed_lines"] == [6]
    assert r.files[0]["covered_pct"] == pytest.approx(66.7)
    assert "uncovered added executable line" in r.detail


def test_waiver_on_same_line_is_honored(tmp_path):
    lines = APP_LINES + [
        "",
        "def unused():",
        "    return 1  # pragma: no cover",
    ]
    # coverage still reports line 6 as missing; the inline waiver must win.
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": [6]}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", lines), refs=("origin/main",)))
    assert r.passed is True
    assert r.files[0]["missed_lines"] == []
    assert len(r.waivers) == 1
    assert r.waivers[0]["line"] == 6
    assert "no cover" in r.waivers[0]["text"]


def test_missing_coverage_tool_fails_never_passes(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    # tooling check reports pytest_cov missing even though all lines would be covered.
    r = _gate(
        repo,
        FakeRunner(
            diff=_diff("app.py", APP_LINES), missing="pytest_cov", refs=("origin/main",)
        ),
    )
    assert r.status == "fail"
    assert r.passed is False
    assert "pip install pytest pytest-cov" in r.detail
    assert r.test_exit_code is None  # tests were never run


def test_unexecutable_interpreter_fails_with_install_hint(tmp_path):
    repo = _repo(tmp_path)

    def _boom(cmd, **kw):
        if cmd[1] == "-c":
            raise OSError("no such interpreter")
        return FakeProc(0, stdout="")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cov_gate.subprocess, "run", _boom)
    r = cov_gate.run_coverage_gate(str(repo))
    monkeypatch.undo()
    assert r.status == "fail"
    assert "test interpreter not executable" in r.detail


# --- omit-pattern detection -------------------------------------------------


def test_omit_pattern_touching_changed_file_fails(tmp_path):
    repo = _repo(
        tmp_path,
        files={
            ".coveragerc": "[run]\nomit = */codegen.py\n",
        },
        cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}},
    )
    fake = FakeRunner(
        diff=_diff("generated/codegen.py", APP_LINES), refs=("origin/main",)
    )
    r = _gate(repo, fake)
    assert r.status == "fail"
    assert r.passed is False
    assert "omit pattern" in r.detail
    assert r.test_exit_code is None  # gate fails before running tests


def test_omit_pattern_in_pyproject_is_detected(tmp_path):
    repo = _repo(
        tmp_path,
        files={
            "pyproject.toml": '[tool.coverage.run]\nomit = "generated/*"\n',
        },
    )
    fake = FakeRunner(
        diff=_diff("generated/codegen.py", APP_LINES), refs=("origin/main",)
    )
    r = _gate(repo, fake)
    assert r.status == "fail"
    assert "omit pattern 'generated/*'" in r.detail


def test_omit_pattern_not_matching_changed_file_is_ignored(tmp_path):
    repo = _repo(
        tmp_path,
        files={
            ".coveragerc": "[run]\nomit = */vendor/*\n",
        },
        cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}},
    )
    fake = FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",))
    r = _gate(repo, fake)
    assert r.passed is True


# --- coverage data availability ----------------------------------------------


def test_missing_coverage_json_fails(tmp_path):
    repo = _repo(tmp_path)  # no coverage.json on disk
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",)))
    assert r.status == "fail"
    assert "coverage data missing" in r.detail


def test_require_fresh_removes_stale_coverage_report(tmp_path):
    repo = _repo(tmp_path, cov={"app.py": {"executed_lines": [1], "missing_lines": []}})
    report = repo / "coverage.json"
    assert report.exists()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cov_gate, "_missing_tooling", lambda _: None)
    monkeypatch.setattr(cov_gate, "_diff_added_lines", lambda *a: ({}, ""))
    try:
        cov_gate.run_coverage_gate(str(repo), base="main", require_fresh=True)
    finally:
        monkeypatch.undo()
    assert not report.exists()


def test_changed_file_with_no_recorded_coverage_data_fails(tmp_path):
    repo = _repo(
        tmp_path, cov={"other.py": {"executed_lines": [1], "missing_lines": []}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",)))
    assert r.status == "fail"
    assert "no coverage data recorded" in r.detail


def test_no_repo_path_fails(tmp_path):
    r = cov_gate.run_coverage_gate(str(tmp_path / "nope"))
    assert r.status == "fail"
    assert "no repo" in r.detail


def test_no_changed_python_lines_passes_without_running_tests(tmp_path):
    repo = _repo(tmp_path)
    fake = FakeRunner(diff="", refs=("origin/main",))
    r = _gate(repo, fake)
    assert r.passed is True
    assert r.files == []
    assert r.test_exit_code is None


# --- base resolution ----------------------------------------------------------


def test_default_base_prefers_origin_main(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",)))
    assert r.base == "origin/main"


def test_default_base_falls_back_to_head_parent(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=("HEAD~1",)))
    assert r.base == "HEAD~1"


def test_default_base_uses_empty_tree_for_root_commit(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=()))
    assert r.base == "<emptytree>"


def test_explicit_base_wins_over_defaults(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=()), base="v1.0")
    assert r.base == "v1.0"


# --- JSON output shape ---------------------------------------------------------


def test_output_shape_all_keys_present(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    r = _gate(repo, FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",)))
    d = r.to_dict()
    assert set(d) == {
        "status",
        "passed",
        "base",
        "files",
        "waivers",
        "detail",
        "test_exit_code",
    }
    assert d["passed"] is True
    assert isinstance(d["files"], list)
    assert set(d["files"][0]) == {"file", "added_lines", "covered_pct", "missed_lines"}
    assert d["waivers"] == []


# --- evidence.py integration ---------------------------------------------------


def test_evidence_manifest_folds_coverage_section(tmp_path):
    repo = _repo(tmp_path, files={".env.example": "X=1\n"})
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=repo,
        check=True,
    )
    output = write_evidence_manifest(
        tmp_path / "artifacts",
        repo,
        entry_point="coverage gate",
        command=["python", "-m", "factory.coverage"],
        coverage={"passed": True, "base": "origin/main"},
    )
    manifest = json.loads(output.read_text())
    assert manifest["coverage"] == {"passed": True, "base": "origin/main"}


def test_evidence_manifest_defaults_coverage_to_empty(tmp_path):
    repo = _repo(tmp_path, files={".env.example": "X=1\n"})
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=repo,
        check=True,
    )
    output = write_evidence_manifest(
        tmp_path / "artifacts",
        repo,
        entry_point="coverage gate",
        command="pytest",
    )
    manifest = json.loads(output.read_text())
    assert manifest["coverage"] == {}


# --- test-only changed files  -----------------------------------

TEST_LINES = [
    "def test_add():",
    "    assert add(1, 2) == 3",
    "",
]


def test_only_test_files_changed_passes_without_coverage_data(tmp_path):
    """A test-only diff passes even when no coverage.json is produced for the
    test files , without demanding coverage data for them. The
    suite still runs (run/no-run logic unchanged) so the anti-bypass can
    inspect coverage, but the absence of test-file coverage data is not a
    failure."""
    repo = _repo(tmp_path)  # no coverage.json on disk
    fake = FakeRunner(
        diff=_diff("tests/test_app.py", TEST_LINES), refs=("origin/main",)
    )
    r = _gate(repo, fake)
    assert r.status == "pass"
    assert r.passed is True
    assert r.test_exit_code == 0  # suite ran, but no coverage data demanded
    assert len(r.files) == 1
    assert r.files[0]["file"] == "tests/test_app.py"
    assert r.files[0]["skipped_reason"] == "test_file"
    assert r.files[0]["added_lines"] == 3


def test_test_file_skipped_in_mixed_diff_source_still_gated(tmp_path):
    """Mixed diff: source file is gated, test file is listed with a skip marker."""
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    diff = _diff("app.py", APP_LINES) + _diff("tests/test_app.py", TEST_LINES)
    r = _gate(repo, FakeRunner(diff=diff, refs=("origin/main",)))
    assert r.passed is True
    assert r.test_exit_code == 0  # tests ran because source lines need coverage
    by_file = {e["file"]: e for e in r.files}
    assert by_file["app.py"]["covered_pct"] == 100.0
    assert "skipped_reason" not in by_file["app.py"]
    assert by_file["tests/test_app.py"]["skipped_reason"] == "test_file"


def test_default_patterns_do_not_match_production_source(tmp_path):
    """Default test-file patterns must never classify production modules."""
    assert cov_gate.is_test_file(
        "tests/test_app.py", cov_gate.DEFAULT_TEST_FILE_PATTERNS
    )
    assert cov_gate.is_test_file("test_app.py", cov_gate.DEFAULT_TEST_FILE_PATTERNS)
    assert cov_gate.is_test_file("app_test.py", cov_gate.DEFAULT_TEST_FILE_PATTERNS)
    assert cov_gate.is_test_file(
        "src/tests/test_app.py", cov_gate.DEFAULT_TEST_FILE_PATTERNS
    )
    assert not cov_gate.is_test_file("app.py", cov_gate.DEFAULT_TEST_FILE_PATTERNS)
    assert not cov_gate.is_test_file("src/app.py", cov_gate.DEFAULT_TEST_FILE_PATTERNS)
    assert not cov_gate.is_test_file(
        "factory/coverage.py", cov_gate.DEFAULT_TEST_FILE_PATTERNS
    )


def test_test_file_patterns_configurable_via_pyproject(tmp_path):
    """Repo can declare coverage.test_file_patterns in pyproject.toml."""
    repo = _repo(
        tmp_path,
        files={
            "pyproject.toml": (
                '[tool.factory.coverage]\ntest_file_patterns = ["spec/*_spec.py"]\n'
            ),
        },
        cov={
            "app.py": {"executed_lines": [1, 2], "missing_lines": []},
            # under the repo-declared pattern, tests/test_app.py is gated as source
            "tests/test_app.py": {"executed_lines": [1, 2, 3], "missing_lines": []},
        },
    )
    diff = _diff("spec/app_spec.py", TEST_LINES) + _diff(
        "tests/test_app.py", TEST_LINES
    )
    r = _gate(repo, FakeRunner(diff=diff, refs=("origin/main",)))
    assert r.passed is True
    by_file = {e["file"]: e for e in r.files}
    assert by_file["spec/app_spec.py"]["skipped_reason"] == "test_file"
    # default pattern no longer applies once the repo declares its own list
    assert "skipped_reason" not in by_file["tests/test_app.py"]
    assert by_file["tests/test_app.py"]["covered_pct"] == 100.0


def test_pattern_matching_measured_file_is_gated_not_skipped(tmp_path):
    """Anti-bypass: a test_file pattern matching a file coverage DID record
    does not skip it -- the file is gated like any other measured file, so a
    mis-scoped pattern can never silently exempt code."""
    repo = _repo(
        tmp_path,
        files={
            "pyproject.toml": (
                '[tool.factory.coverage]\ntest_file_patterns = ["helpers/*"]\n'
            ),
        },
        cov={
            # helpers/thing.py was measured and is fully covered -> gated, passes
            "helpers/thing.py": {"executed_lines": [1, 2], "missing_lines": []},
            "app.py": {"executed_lines": [1, 2], "missing_lines": []},
        },
    )
    diff = _diff("helpers/thing.py", APP_LINES) + _diff("app.py", APP_LINES)
    r = _gate(repo, FakeRunner(diff=diff, refs=("origin/main",)))
    assert r.passed is True
    by_file = {e["file"]: e for e in r.files}
    assert "skipped_reason" not in by_file["helpers/thing.py"]
    assert by_file["helpers/thing.py"]["covered_pct"] == 100.0


def test_pattern_matching_uncovered_measured_file_fails(tmp_path):
    """Anti-bypass (fail direction): a measured-but-uncovered file that a
    test_file pattern claims must still fail the gate."""
    repo = _repo(
        tmp_path,
        files={
            "pyproject.toml": (
                '[tool.factory.coverage]\ntest_file_patterns = ["helpers/*"]\n'
            ),
        },
        cov={
            "helpers/thing.py": {"executed_lines": [1], "missing_lines": [2]},
            "app.py": {"executed_lines": [1, 2], "missing_lines": []},
        },
    )
    lines = APP_LINES + ["", "def extra():", "    return 1"]
    diff = _diff("helpers/thing.py", lines) + _diff("app.py", APP_LINES)
    r = _gate(repo, FakeRunner(diff=diff, refs=("origin/main",)))
    assert r.status == "fail"
    assert "uncovered added executable line" in r.detail


# --- test_file_patterns parser branches --------------------------------------


def test_load_test_file_patterns_defaults_when_no_pyproject(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert cov_gate._load_test_file_patterns(repo) == list(
        cov_gate.DEFAULT_TEST_FILE_PATTERNS
    )


def test_load_test_file_patterns_reads_multi_line_list(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[tool.factory.coverage]\n"
        "test_file_patterns = [\n"
        '    "spec/*_spec.py",\n'
        '    "integration/*_it.py",\n'
        "]\n"
    )
    assert cov_gate._load_test_file_patterns(repo) == [
        "spec/*_spec.py",
        "integration/*_it.py",
    ]


def test_load_test_file_patterns_reads_comma_separated_string(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[tool.factory.coverage]\n"
        'test_file_patterns = "spec/*_spec.py,integration/*_it.py"\n'
    )
    assert cov_gate._load_test_file_patterns(repo) == [
        "spec/*_spec.py",
        "integration/*_it.py",
    ]


def test_load_test_file_patterns_ignores_other_sections_and_keys(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[tool.other]\n"
        'test_file_patterns = ["nope/*"]\n'
        "[tool.factory.coverage]\n"
        'unrelated_key = "x"\n'
        'test_file_patterns = ["spec/*_spec.py"]\n'
    )
    assert cov_gate._load_test_file_patterns(repo) == ["spec/*_spec.py"]


def test_load_test_file_patterns_skips_lines_without_equals_in_section(tmp_path):
    """A bare/comment line inside the active section (no '=') is ignored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[tool.factory.coverage]\n"
        "# a comment line\n"
        'test_file_patterns = ["spec/*_spec.py"]\n'
    )
    assert cov_gate._load_test_file_patterns(repo) == ["spec/*_spec.py"]


def test_diff_failure_fails_gate(tmp_path):
    """When git diff cannot be produced, the gate fails with the reason."""
    repo = _repo(tmp_path)

    class DiffFail(FakeRunner):
        def __call__(self, cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "diff":
                return FakeProc(1, stdout="", stderr="git says no")
            return super().__call__(cmd, **kwargs)

    r = _gate(repo, DiffFail(diff="", refs=("origin/main",)))
    assert r.status == "fail"
    assert "could not compute diff" in r.detail


def test_main_cli_returns_zero_on_pass(tmp_path):
    repo = _repo(
        tmp_path, cov={"app.py": {"executed_lines": [1, 2], "missing_lines": []}}
    )
    fake = FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cov_gate.subprocess, "run", fake)
    try:
        exit_code = cov_gate.main(["--repo", str(repo)])
    finally:
        monkeypatch.undo()
    assert exit_code == 0


def test_main_cli_returns_one_on_fail(tmp_path):
    repo = _repo(tmp_path)  # no coverage.json
    fake = FakeRunner(diff=_diff("app.py", APP_LINES), refs=("origin/main",))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cov_gate.subprocess, "run", fake)
    try:
        exit_code = cov_gate.main(["--repo", str(repo)])
    finally:
        monkeypatch.undo()
    assert exit_code == 1


def test_widened_pattern_cannot_exempt_production_in_test_only_diff(tmp_path):
    """Safety invariant: a repo widening test_file_patterns to cover a
    production file cannot defeat the gate with a test-only-looking diff. Here
    the diff touches ONLY helpers/thing.py (classified as a test file by the
    repo's widened pattern), but coverage DID measure it, so it must be gated
    -- not silently skipped. A fully-covered measured file passes only because
    it is covered; an uncovered one fails."""
    repo = _repo(
        tmp_path,
        files={
            "pyproject.toml": (
                '[tool.factory.coverage]\ntest_file_patterns = ["helpers/*"]\n'
            ),
        },
        cov={
            "helpers/thing.py": {"executed_lines": [1], "missing_lines": [2]},
        },
    )
    # Uncovered added line: a production change mis-classified as "test" must
    # still fail the gate, exactly as if the pattern did not match it.
    lines = APP_LINES + ["", "def extra():", "    return 1"]
    r = _gate(
        repo, FakeRunner(diff=_diff("helpers/thing.py", lines), refs=("origin/main",))
    )
    assert r.status == "fail"
    assert "uncovered added executable line" in r.detail

    # Fully covered production change under a widened pattern passes only
    # because it is covered -- it is gated, not skipped.
    covered_dir = tmp_path / "covered"
    covered_dir.mkdir()
    repo2 = _repo(
        covered_dir,
        files={
            "pyproject.toml": (
                '[tool.factory.coverage]\ntest_file_patterns = ["helpers/*"]\n'
            ),
        },
        cov={
            "helpers/thing.py": {"executed_lines": [1, 2], "missing_lines": []},
        },
    )
    r2 = _gate(
        repo2,
        FakeRunner(diff=_diff("helpers/thing.py", APP_LINES), refs=("origin/main",)),
    )
    assert r2.passed is True
    by_file = {e["file"]: e for e in r2.files}
    assert "skipped_reason" not in by_file["helpers/thing.py"]
    assert by_file["helpers/thing.py"]["covered_pct"] == 100.0


# --- nonzero test exit must fail the gate (both pass paths) -----


def test_nonzero_test_exit_fails_even_when_lines_covered(tmp_path):
    cov = {"app.py": {"executed_lines": [2], "missing_lines": []}}
    r = _gate(
        _repo(tmp_path, cov=cov),
        FakeRunner(
            diff=_diff("app.py", APP_LINES), refs=("origin/main",), pytest_exit=1
        ),
    )
    assert r.passed is False
    assert r.status == "fail"
    assert "nonzero" in r.detail.lower()
    assert r.test_exit_code == 1


def test_nonzero_test_exit_fails_test_only_diff_path(tmp_path):
    diff = _diff("tests/test_app.py", ["def test_x():", "    assert True", ""])
    r = _gate(tmp_path, FakeRunner(diff=diff, refs=("origin/main",), pytest_exit=2))
    assert r.passed is False
    assert r.status == "fail"
    assert "nonzero" in r.detail.lower()
    assert r.test_exit_code == 2


# --- nested-repo path resolution  -------------------------------


def test_nested_repo_diff_paths_resolve_to_coverage_keys(tmp_path):
    """A template repo nested inside the factory repo gets diff paths relative
    to the git toplevel (templates/fastapi-postgres/src/app.py), while its
    coverage.json keys are cwd-relative (src/app.py). The gate must resolve
    one to the other or a nested-repo card can never pass."""
    nested = "templates/fastapi-postgres/src/app.py"
    root = tmp_path / "factory-root"
    repo = root / "templates/fastapi-postgres"
    repo.mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "coverage.json").write_text(
        json.dumps(
            {"files": {"src/app.py": {"executed_lines": [1, 2], "missing_lines": []}}}
        )
    )
    r = _gate(
        repo,
        FakeRunner(
            diff=_diff(nested, APP_LINES),
            refs=("origin/main",),
            toplevel=str(root),
        ),
    )
    assert r.passed is True, r.detail
    assert r.status == "pass"
    assert r.files[0]["file"] == nested
    assert r.files[0]["covered_pct"] == 100.0
