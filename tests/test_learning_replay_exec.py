"""Replay classification, authoritative phase reports and real execution.

The real-process cases use disposable fixtures created under the operator home:
a throwaway git repository with two commits, a throwaway evaluator suite, an
empty work root and a non-existent out directory. No host repository, service or
network resource is touched; every generated checkout is retained inside that
fixture work root and removed with it by the test fixture.
"""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from factory import replay

REPO = Path(__file__).resolve().parents[1]
GIT = ["git", "-c", "user.name=Replay Test", "-c", "user.email=replay@example.invalid"]


# ---------------------------------------------------------------------------
# offline boundary
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("replay tests forbid network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked, raising=True)


@pytest.fixture
def work_base():
    import shutil
    import uuid

    base = (
        Path.home()
        / ".hermes"
        / "cache"
        / "scratch"
        / f"replay-exec-{uuid.uuid4().hex}"
    )
    base.mkdir(parents=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# disposable git fixture
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    process = subprocess.run(
        [*GIT, *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert process.returncode == 0, (args, process.stdout, process.stderr)
    return process.stdout.strip()


def make_repo(
    base: Path,
    *,
    base_value: int = 1,
    candidate_value: int = 2,
    candidate_config: bool = False,
):
    repo = base / "repo"
    (repo / "factory").mkdir(parents=True)
    (repo / "factory" / "__init__.py").write_text("")
    (repo / "factory" / "thing.py").write_text(
        f"def value():\n    return {base_value}\n"
    )
    # Pin the initial branch: GitHub runners may default to ``master`` while the
    # safety tests address ``refs/heads/main`` explicitly.
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")

    (repo / "factory" / "thing.py").write_text(
        f"def value():\n    return {candidate_value}\n"
    )
    if candidate_config:
        (repo / "conftest.py").write_text(
            "import pytest\n"
            "\n"
            "@pytest.hookimpl(wrapper=True)\n"
            "def pytest_runtest_makereport(item, call):\n"
            "    report = yield\n"
            "    report.outcome = 'passed'\n"
            "    return report\n"
        )
        (repo / "pytest.ini").write_text("[pytest]\naddopts = -k does_not_match\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--allow-empty", "-m", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "baseline": baseline, "candidate": candidate}


def test_make_repo_pins_main_regardless_of_ambient_init_default(tmp_path, monkeypatch):
    """The disposable repo must not inherit the runner's ``init.defaultBranch``."""
    monkeypatch.setattr(
        sys.modules[__name__], "GIT", [*GIT, "-c", "init.defaultBranch=trunk"]
    )

    fixture = make_repo(tmp_path)

    assert git(fixture["repo"], "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(fixture["repo"], "rev-parse", "refs/heads/main") == fixture["candidate"]
    branches = git(
        fixture["repo"], "for-each-ref", "--format=%(refname:short)", "refs/heads"
    )
    assert branches.splitlines() == ["main"]


def make_suite(base: Path, *, write_suite, controls=None) -> tuple[Path, str]:
    suite = base / "suite"
    digest = write_suite(suite, controls=controls)
    return suite, digest


def paths_for(base: Path, fixture: dict, suite: Path, digest: str) -> dict:
    work_root = base / "run"
    work_root.mkdir()
    return {
        "repo": str(fixture["repo"]),
        "baseline": fixture["baseline"],
        "candidate": fixture["candidate"],
        "suite_dir": str(suite),
        "suite_digest": digest,
        "work_root": str(work_root),
        "out": str(base / "out"),
        "approve_execution": True,
        "timeout_s": 60,
    }


# ---------------------------------------------------------------------------
# authoritative phase report classification (no process)
# ---------------------------------------------------------------------------


def phase(outcome="passed", *, exception_type=None, is_assertion=None):
    entry = {"outcome": outcome, "duration": 0.001}
    if exception_type is not None:
        entry.update(
            {
                "exception_type": exception_type,
                "exception_module": "builtins",
                "exception_is_assertion": bool(is_assertion),
            }
        )
    return entry


def node(*, call=None, setup=None, teardown=None):
    return {
        "setup": setup or phase(),
        "call": call or phase(),
        "teardown": teardown or phase(),
    }


TARGET = "test_subject.py::test_subject_is_two"
CONTROL = "test_subject.py::test_subject_is_positive"
EXPECTED = [TARGET, CONTROL]


def classify(nodes, returncode, *, collected=None, collection_errors=None):
    report = {
        "collected": collected if collected is not None else list(nodes),
        "nodes": nodes,
        "collection_errors": collection_errors or [],
    }
    return replay._classify_side(
        report=report, returncode=returncode, expected=EXPECTED, target=TARGET
    )


def test_classify_assertion_failure_and_pass():
    status, _reason, observed = classify(
        {
            TARGET: node(
                call=phase("failed", exception_type="AssertionError", is_assertion=True)
            ),
            CONTROL: node(),
        },
        1,
    )

    assert status == "ok"
    assert observed[TARGET] == "assertion_failure"


@pytest.mark.parametrize(
    "nodes, returncode, collected, collection_errors, expected",
    [
        (
            {
                TARGET: node(
                    call=phase(
                        "failed", exception_type="AssertionError", is_assertion=True
                    )
                )
            },
            1,
            None,
            None,
            "missing_node",
        ),
        (
            {
                TARGET: node(
                    call=phase(
                        "failed", exception_type="AssertionError", is_assertion=True
                    )
                ),
                CONTROL: node(),
            },
            1,
            [TARGET, CONTROL, TARGET],
            None,
            "duplicate_node",
        ),
        (
            {
                TARGET: node(
                    call=phase(
                        "failed", exception_type="AssertionError", is_assertion=True
                    )
                ),
                CONTROL: node(),
                "test_subject.py::test_extra": node(),
            },
            1,
            None,
            None,
            "unexpected_node",
        ),
        (
            {
                TARGET: node(
                    call=phase(
                        "failed", exception_type="AssertionError", is_assertion=True
                    )
                ),
                CONTROL: node(),
            },
            2,
            None,
            [
                {
                    "nodeid": "test_subject.py",
                    "outcome": "failed",
                    "message": "ImportError",
                }
            ],
            "collection_error",
        ),
        ({}, 5, [], None, "no_tests"),
        (
            {
                TARGET: node(
                    call=phase(
                        "failed", exception_type="AssertionError", is_assertion=True
                    )
                ),
                CONTROL: node(),
            },
            3,
            None,
            None,
            "interrupted",
        ),
        (
            {
                TARGET: node(call=phase("skipped")),
                CONTROL: node(),
            },
            0,
            None,
            None,
            "skipped",
        ),
        (
            {
                TARGET: node(setup=phase("failed", exception_type="RuntimeError")),
                CONTROL: node(),
            },
            1,
            None,
            None,
            "error_failure",
        ),
        (
            {
                TARGET: node(teardown=phase("failed", exception_type="RuntimeError")),
                CONTROL: node(),
            },
            1,
            None,
            None,
            "error_failure",
        ),
        (
            {
                TARGET: node(
                    call=phase("failed", exception_type="KeyError", is_assertion=False)
                ),
                CONTROL: node(),
            },
            1,
            None,
            None,
            "error_failure",
        ),
        (
            {
                TARGET: node(
                    call=phase(
                        "failed", exception_type="AssertionError", is_assertion=True
                    )
                ),
                CONTROL: node(),
            },
            0,
            None,
            None,
            "contradictory_exit",
        ),
        (
            {TARGET: node(), CONTROL: node()},
            1,
            None,
            None,
            "contradictory_exit",
        ),
    ],
)
def test_structural_conditions_can_never_reproduce(
    nodes, returncode, collected, collection_errors, expected
):
    status, _reason, _observed = classify(
        nodes, returncode, collected=collected, collection_errors=collection_errors
    )

    assert status == expected


# ---------------------------------------------------------------------------
# real execution on a disposable fixture
# ---------------------------------------------------------------------------


def test_disposable_fixture_replay_reproduces_with_bound_evidence(work_base):
    fixture = make_repo(work_base)
    suite = work_base / "suite"
    from test_learning_replay import write_suite

    digest = write_suite(suite)
    kwargs = paths_for(work_base, fixture, suite, digest)

    result = replay.replay(**kwargs)

    assert result.verdict == "reproduced", (result.reason, result.detail)
    report = result.report
    assert report["promotional"] is False
    assert report["acceptance_evidence"] is False
    assert report["suite"]["digest"] == digest
    assert report["suite"]["digest_before"] == digest
    assert report["suite"]["digest_after"] == digest
    assert report["sources"]["baseline"]["requested_sha"] == fixture["baseline"]
    assert report["sources"]["candidate"]["requested_sha"] == fixture["candidate"]
    baseline_target = report["sides"]["baseline"]["nodes"][TARGET]["call"]
    candidate_target = report["sides"]["candidate"]["nodes"][TARGET]["call"]
    assert baseline_target["outcome"] == "failed"
    assert baseline_target["exception_type"] == "AssertionError"
    assert baseline_target["exception_is_assertion"] is True
    assert candidate_target["outcome"] == "passed"
    assert report["sides"]["baseline"]["status"] == "ok"
    assert Path(report["sides"]["baseline"]["worktree"]).is_dir()
    assert Path(report["sides"]["candidate"]["worktree"]).is_dir()
    out = Path(kwargs["out"])
    for side in ("baseline", "candidate"):
        record = report["sides"][side]
        assert record["head_before"] == record["requested_sha"]
        assert record["head_after"] == record["requested_sha"]
        assert record["exit_code"] in (0, 1)
        assert record["report"]["sha256"].startswith("sha256:")
        assert (out / "logs" / f"{side}.stdout.log").is_file()
        assert (out / "logs" / f"{side}.phase.json").is_file()
    assert (out / "replay-report.json").is_file()
    assert (out / "replay-report.sha256").is_file()


def test_candidate_configuration_cannot_force_green(work_base):
    fixture = make_repo(work_base, candidate_value=1, candidate_config=True)
    suite = work_base / "suite"
    from test_learning_replay import write_suite

    digest = write_suite(suite)
    kwargs = paths_for(work_base, fixture, suite, digest)

    result = replay.replay(**kwargs)

    assert result.verdict == "not_reproduced", (result.reason, result.detail)
    assert result.reason == "target_candidate_mismatch"
    assert (
        result.report["sides"]["candidate"]["observed"][TARGET] == "assertion_failure"
    )


def test_both_pass_is_not_improvement(work_base):
    fixture = make_repo(work_base, base_value=2, candidate_value=2)
    suite = work_base / "suite"
    from test_learning_replay import write_suite

    digest = write_suite(suite)
    kwargs = paths_for(work_base, fixture, suite, digest)

    result = replay.replay(**kwargs)

    assert result.verdict == "not_reproduced"
    assert result.reason == "both_pass"


def test_engine_source_cannot_leak_into_a_checkout(work_base):
    """The pinned editable engine must not satisfy a missing checkout module."""
    checkout = work_base / "bare-checkout"
    (checkout / "factory").mkdir(parents=True)
    (checkout / "factory" / "__init__.py").write_text("")
    probe = work_base / "isolation_probe.py"
    probe.write_text(
        "import sys\n"
        "from importlib.util import module_from_spec, spec_from_file_location\n"
        "from pathlib import Path\n"
        f"guard_path = Path({str(REPO)!r}) / 'factory' / '_replay_isolation.py'\n"
        "spec = spec_from_file_location('factory_replay_isolation', guard_path)\n"
        "module = module_from_spec(spec)\n"
        "sys.modules['factory_replay_isolation'] = module\n"
        "spec.loader.exec_module(module)\n"
        "module.install(Path(sys.argv[1]))\n"
        "import factory\n"
        "print('factory:', factory.__file__)\n"
        "import factory.evidence\n"
        "print('LEAKED:', factory.evidence.__file__)\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work_base / "home"),
        "PYTHONPATH": str(checkout),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.run(
        [sys.executable, str(probe), str(checkout)],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert process.returncode != 0
    assert "LEAKED" not in process.stdout
    assert (
        "factory.evidence" in process.stderr or "ModuleNotFoundError" in process.stderr
    )


# ---------------------------------------------------------------------------
# fake runner: report authority, malformed reports
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, reports: dict, exits: dict, stdout: dict | None = None):
        self.reports = reports
        self.exits = exits
        self.stdout = stdout or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd, env, timeout_s, stdout_path, stderr_path):
        self.calls.append(argv)
        stdout_path.write_text(self.stdout.get(stdout_path.name, ""))
        stderr_path.write_text("")
        if stdout_path.name.endswith("preflight.log"):
            Path(stdout_path).write_text(
                json.dumps(
                    {
                        "factory_file": str(Path(cwd) / "factory" / "__init__.py"),
                        "evidence_file": str(Path(cwd) / "factory" / "evidence.py"),
                        "executable": sys.executable,
                        "python": sys.version.split()[0],
                        "sitecustomize": [],
                    }
                )
            )
            return replay.ProcessOutcome(0, False, 0.01, stdout_path, stderr_path)
        side = stdout_path.name.split(".")[0]
        spec = json.loads(Path(argv[-1]).read_text())
        payload = self.reports[side]
        Path(spec["report_path"]).write_text(
            payload
            if isinstance(payload, str)
            else json.dumps(self._fill(payload, spec))
        )
        return replay.ProcessOutcome(
            self.exits[side], False, 0.01, stdout_path, stderr_path
        )

    @staticmethod
    def _fill(payload: dict, spec: dict) -> dict:
        plugin_sha = replay.sha256_file(replay._plugin_path())
        filled = dict(payload)
        filled.setdefault(
            "schema", {"name": "factory-replay-phase-report", "version": 1}
        )
        filled["nonce"] = spec["nonce"]
        filled["checkout"] = spec["checkout"]
        filled["plugin"] = {
            "path": str(replay._plugin_path()),
            "sha256": plugin_sha,
        }
        filled.setdefault("exitstatus", 0)
        filled.setdefault("collection_errors", [])
        return filled


def fake_run(work_base, monkeypatch, reports, exits, stdout=None):
    from test_learning_replay import write_suite

    fixture = make_repo(work_base)
    suite = work_base / "suite"
    digest = write_suite(suite)
    kwargs = paths_for(work_base, fixture, suite, digest)
    runner = FakeRunner(reports, exits, stdout)
    monkeypatch.setattr(replay, "_run_process", runner)
    return replay.replay(**kwargs), runner


def failing_target(exception="AssertionError", is_assertion=True):
    return {
        "collected": [TARGET, CONTROL],
        "nodes": {
            TARGET: node(
                call=phase(
                    "failed", exception_type=exception, is_assertion=is_assertion
                )
            ),
            CONTROL: node(),
        },
        "exitstatus": 1,
    }


def passing_all():
    return {
        "collected": [TARGET, CONTROL],
        "nodes": {TARGET: node(), CONTROL: node()},
        "exitstatus": 0,
    }


def test_stdout_pass_or_fail_markers_are_not_authoritative(work_base, monkeypatch):
    result, runner = fake_run(
        work_base,
        monkeypatch,
        {"baseline": failing_target(), "candidate": passing_all()},
        {"baseline": 1, "candidate": 0},
        stdout={
            "baseline.stdout.log": "1 passed in 0.01s\n",
            "candidate.stdout.log": "1 failed, 1 passed in 0.01s\nFAILED test_subject.py::test_subject_is_two\n",
        },
    )

    assert result.verdict == "reproduced", (result.reason, result.detail)
    report = result.report
    assert report["sides"]["baseline"]["nodes"][TARGET]["call"]["outcome"] == "failed"
    assert report["sides"]["candidate"]["nodes"][TARGET]["call"]["outcome"] == "passed"


def test_malformed_phase_report_is_inconclusive(work_base, monkeypatch):
    result, _runner = fake_run(
        work_base,
        monkeypatch,
        {"baseline": "{not json", "candidate": passing_all()},
        {"baseline": 1, "candidate": 0},
    )

    assert result.verdict == "inconclusive"
    assert result.reason == "malformed_report"


def test_phase_report_with_wrong_nonce_is_inconclusive():
    checkout = Path("/tmp/checkout")
    report = {
        "schema": {"name": "factory-replay-phase-report", "version": 1},
        "nonce": "stale",
        "checkout": str(checkout),
        "plugin": {"sha256": "sha256:" + "0" * 64},
        "exitstatus": 1,
        "collected": [],
        "nodes": {},
    }

    problem = replay._report_problem(
        report,
        nonce="fresh",
        checkout=checkout,
        plugin_sha="sha256:" + "0" * 64,
        returncode=1,
    )

    assert problem is not None
    assert problem[0] == "malformed_report"
    assert "nonce" in problem[1]
