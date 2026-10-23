"""Replay safety: timeout reaping, suite tampering, gate refusal, real replay.

The historical training replay at the end runs the *actual* commits cited by the
plan (``a07521c`` and ``f90af08``) through the shipped CLI against the committed
evaluator suite. Its generated checkouts are disposable test fixtures and are
removed from the shared repository's worktree registry when the test ends; the
retained-tree rule applies to the recorded demonstration, not to test fixtures.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from test_learning_replay_exec import (
    FakeRunner,
    git,
    make_repo,
    passing_all,
    paths_for,
)

from factory import learning, promote, replay, verify

REPO = Path(__file__).resolve().parents[1]
HISTORICAL_BASELINE = "a07521c7a8761a9edada4d9faaa9c22efb4ec61a"
HISTORICAL_CANDIDATE = "f90af08906583a5ebe13a7acf0bc65aa18d53a99"
TRAINING_SUITE = REPO / "tests" / "fixtures" / "replay-suite"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("replay tests forbid network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked, raising=True)


@pytest.fixture
def work_base():
    base = (
        Path.home()
        / ".hermes"
        / "cache"
        / "scratch"
        / f"replay-safe-{uuid.uuid4().hex}"
    )
    base.mkdir(parents=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def write_suite_file(suite: Path, name: str, content: str) -> None:
    (suite / name).write_text(content)


def custom_suite(suite: Path, files: dict[str, str], expectations: dict) -> str:
    suite.mkdir(parents=True)
    (suite / "pytest.ini").write_text("[pytest]\n")
    for name, content in files.items():
        write_suite_file(suite, name, content)
    (suite / "expectations.json").write_text(
        json.dumps(expectations, indent=2, sort_keys=True) + "\n"
    )
    digest = replay.compute_suite_digest(suite)
    expectations["suite_digest"] = digest
    (suite / "expectations.json").write_text(
        json.dumps(expectations, indent=2, sort_keys=True) + "\n"
    )
    return digest


def expectations_for(target: str, control: str, *, timeout_s: int = 1) -> dict:
    return {
        "schema": {"name": "factory-replay-expectations", "version": 1},
        "suite_id": "safety-case",
        "suite_digest": "sha256:" + "0" * 64,
        "cases": [
            {
                "nodeid": target,
                "baseline": {"outcome": "assertion_failure"},
                "candidate": {"outcome": "pass"},
            }
        ],
        "controls": [
            {
                "nodeid": control,
                "baseline": {"outcome": "pass"},
                "candidate": {"outcome": "pass"},
            }
        ],
        "timeout_s": timeout_s,
    }


# ---------------------------------------------------------------------------
# timeout: kill only the launched process group, reap the leader, no survivor
# ---------------------------------------------------------------------------


def test_timeout_kills_its_process_group_and_leaves_no_child(work_base):
    fixture = make_repo(work_base)
    suite = work_base / "suite"
    digest = custom_suite(
        suite,
        {
            "test_sleep.py": (
                "import os\n"
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                "from pathlib import Path\n"
                "\n"
                "\n"
                "def test_slow():\n"
                "    child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'])\n"
                "    Path(os.environ['TMPDIR'], 'child.pid').write_text(str(child.pid))\n"
                "    time.sleep(30)\n"
                "\n"
                "\n"
                "def test_control():\n"
                "    assert True\n"
            )
        },
        expectations_for(
            "test_sleep.py::test_slow", "test_sleep.py::test_control", timeout_s=1
        ),
    )
    kwargs = paths_for(work_base, fixture, suite, digest)
    kwargs["timeout_s"] = 1

    started = time.monotonic()
    result = replay.replay(**kwargs)
    elapsed = time.monotonic() - started

    assert result.verdict == "inconclusive", (result.reason, result.detail)
    assert result.reason == "timeout"
    assert result.report["sides"]["baseline"]["timed_out"] is True
    assert elapsed < 20

    pid_files = sorted(
        (Path(kwargs["out"]) / "logs" / "harness").glob("*/tmp/child.pid")
    )
    assert pid_files, "the fixture child never recorded its pid"
    pid = int(pid_files[0].read_text().strip())
    deadline = time.monotonic() + 10
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert alive is False, f"fixture child {pid} survived the replay timeout"


# ---------------------------------------------------------------------------
# suite mutation during execution
# ---------------------------------------------------------------------------


def test_missing_dependency_is_inconclusive_not_improvement(work_base):
    fixture = make_repo(work_base)
    suite = work_base / "suite"
    digest = custom_suite(
        suite,
        {
            "test_missing.py": (
                "import definitely_missing_replay_dependency\n"
                "\n"
                "\n"
                "def test_subject():\n"
                "    assert definitely_missing_replay_dependency.VALUE == 1\n"
                "\n"
                "\n"
                "def test_control():\n"
                "    assert True\n"
            )
        },
        expectations_for(
            "test_missing.py::test_subject", "test_missing.py::test_control"
        ),
    )
    kwargs = paths_for(work_base, fixture, suite, digest)

    result = replay.replay(**kwargs)

    assert result.verdict == "inconclusive", (result.verdict, result.reason)
    assert result.reason in {"collection_error", "harness_error"}


def test_suite_changed_during_execution_is_rejected(work_base, monkeypatch):
    from test_learning_replay import write_suite

    fixture = make_repo(work_base)
    suite = work_base / "suite"
    digest = write_suite(suite)
    kwargs = paths_for(work_base, fixture, suite, digest)

    def tamper(side, spec, suite=suite):
        if side == "baseline":
            (suite / "test_subject.py").write_text(
                "def test_subject_is_two():\n    assert True\n"
            )

    runner = FakeRunner(
        {"baseline": passing_all(), "candidate": passing_all()},
        {"baseline": 0, "candidate": 0},
    )
    original = runner.__call__

    def wrapping(argv, **kwargs_):
        result = original(argv, **kwargs_)
        if kwargs_["stdout_path"].name.startswith("baseline.stdout"):
            tamper("baseline", {})
        return result

    monkeypatch.setattr(replay, "_run_process", wrapping)

    result = replay.replay(**kwargs)

    assert result.verdict == "rejected", (result.verdict, result.reason)
    assert result.reason == "suite_modified"
    assert result.report["suite"]["digest_after_baseline"] != digest


# ---------------------------------------------------------------------------
# factory-verify / factory-promote must reject a replay report
# ---------------------------------------------------------------------------


def test_verify_and_promote_reject_a_replay_report(work_base, capsys):
    from test_learning_replay import write_suite

    fixture = make_repo(work_base)
    suite = work_base / "suite"
    digest = write_suite(suite)
    kwargs = paths_for(work_base, fixture, suite, digest)
    result = replay.replay(**kwargs)
    assert result.verdict == "reproduced", (result.reason, result.detail)
    out = Path(kwargs["out"])
    assert (out / "replay-report.json").is_file()

    before_ref = git(fixture["repo"], "rev-parse", "refs/heads/main")
    verify_code = verify.main(
        [
            "--artifacts-dir",
            str(out),
            "--repo",
            str(fixture["repo"]),
            "--candidate",
            fixture["candidate"],
            "--base",
            fixture["baseline"],
        ]
    )
    assert verify_code == 1
    capsys.readouterr()

    # A ref that is not checked out, so promotion reaches evidence verification.
    git(fixture["repo"], "update-ref", "refs/heads/replay-target", fixture["baseline"])
    target_before = git(fixture["repo"], "rev-parse", "refs/heads/replay-target")
    promote_code = promote.main(
        [
            "--repo",
            str(fixture["repo"]),
            "--artifacts-dir",
            str(out),
            "--candidate",
            fixture["candidate"],
            "--base",
            fixture["baseline"],
            "--target",
            "refs/heads/replay-target",
            "--dry-run",
            "--json",
        ]
    )
    target_after = git(fixture["repo"], "rev-parse", "refs/heads/replay-target")
    after_ref = git(fixture["repo"], "rev-parse", "refs/heads/main")
    payload = json.loads(capsys.readouterr().out)
    assert promote_code == 1
    assert "manifest_missing" in payload["reason"], payload
    assert target_after == target_before, "a rejected replay report must not move a ref"
    assert after_ref == before_ref


# ---------------------------------------------------------------------------
# the real historical training replay (a07521c -> f90af08)
# ---------------------------------------------------------------------------


def test_ci_checkout_fetches_the_history_the_historical_replay_needs():
    """A shallow checkout lacks ``a07521c``; CI must provision full history."""
    workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text()
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if "actions/checkout@" in line)
    step = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("- "):
            break
        if stripped.startswith("with:"):
            continue
        step.append(stripped)
    assert "fetch-depth: 0" in step, step


def test_real_historical_training_replay(work_base):
    suite = work_base / "training-suite"
    shutil.copytree(TRAINING_SUITE, suite)
    digest = replay.compute_suite_digest(suite)
    committed = json.loads((TRAINING_SUITE / "expectations.json").read_text())
    assert digest == committed["suite_digest"]

    work_root = work_base / "training-run"
    work_root.mkdir()
    out = work_base / "training-out"
    checkouts = [work_root / "baseline", work_root / "candidate"]
    try:
        result = replay.replay(
            repo=str(REPO),
            baseline=HISTORICAL_BASELINE,
            candidate=HISTORICAL_CANDIDATE,
            suite_dir=str(suite),
            suite_digest=digest,
            work_root=str(work_root),
            out=str(out),
            approve_execution=True,
            timeout_s=120,
        )
    finally:
        for checkout in checkouts:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                cwd=REPO,
                capture_output=True,
                check=False,
            )

    assert result.verdict == "reproduced", (result.reason, result.detail)
    report = result.report
    assert report["verdict"] == "reproduced"
    assert report["promotional"] is False
    assert report["acceptance_evidence"] is False
    target = "test_duplicate_json_keys.py::test_duplicate_json_keys_are_rejected"
    baseline_call = report["sides"]["baseline"]["nodes"][target]["call"]
    candidate_call = report["sides"]["candidate"]["nodes"][target]["call"]
    assert baseline_call["outcome"] == "failed"
    assert baseline_call["exception_type"] == "AssertionError"
    assert baseline_call["exception_is_assertion"] is True
    assert candidate_call["outcome"] == "passed"
    for control in report["expectations"]["controls"]:
        for side in ("baseline", "candidate"):
            assert (
                report["sides"][side]["nodes"][control["nodeid"]]["call"]["outcome"]
                == "passed"
            )
    assert report["suite"]["digest"] == digest
    assert report["sources"]["baseline"]["requested_sha"] == HISTORICAL_BASELINE
    assert report["sources"]["candidate"]["requested_sha"] == HISTORICAL_CANDIDATE


# ---------------------------------------------------------------------------
# shipped CLI: exit codes + JSON + human output
# ---------------------------------------------------------------------------


def cli_argv(kwargs: dict, *extra: str) -> list[str]:
    return [
        "replay",
        "--repo",
        kwargs["repo"],
        "--baseline",
        kwargs["baseline"],
        "--candidate",
        kwargs["candidate"],
        "--suite-dir",
        kwargs["suite_dir"],
        "--suite-digest",
        kwargs["suite_digest"],
        "--work-root",
        kwargs["work_root"],
        "--out",
        kwargs["out"],
        "--approve-execution",
        "--timeout-s",
        "60",
        *extra,
    ]


def test_cli_json_round_trip_and_human_output(work_base, capsys):
    from test_learning_replay import write_suite

    fixture = make_repo(work_base)
    suite = work_base / "suite"
    digest = write_suite(suite)
    kwargs = paths_for(work_base, fixture, suite, digest)

    assert learning.main(cli_argv(kwargs, "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "reproduced"
    assert payload["promotional"] is False
    assert payload["acceptance_evidence"] is False
    assert payload["sources"]["baseline"]["requested_sha"] == fixture["baseline"]

    second = work_base / "second"
    second.mkdir()
    second_fixture = make_repo(second)
    second_suite = second / "suite"
    second_digest = write_suite(second_suite)
    human_kwargs = paths_for(second, second_fixture, second_suite, second_digest)

    assert learning.main(cli_argv(human_kwargs)) == 0
    human = capsys.readouterr().out
    assert "REPRODUCED" in human
    assert human_kwargs["baseline"] in human
    assert human_kwargs["candidate"] in human
    assert human_kwargs["suite_digest"] in human
