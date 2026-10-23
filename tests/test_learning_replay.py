"""Deterministic replay tests: suite binding, admission and classification.

Cycle 1 covers the *binding* half of the replay contract: the non-circular
suite digest, the strict expectation contract, and every admission refusal that
must happen before a Git object is read or a process is spawned.

Admission/unit cases monkeypatch ``factory.replay._run_process`` so a regression
in path/admission ordering cannot silently launch a process; the module also
denies sockets outright. Real replay processes appear only in the reviewed
disposable-fixture cases added in later cycles.
"""

import hashlib
import json
import socket
from pathlib import Path

import pytest

from factory import learning, replay

REPO = Path(__file__).resolve().parents[1]
BASELINE = "a07521c7a8761a9edada4d9faaa9c22efb4ec61a"
CANDIDATE = "f90af08906583a5ebe13a7acf0bc65aa18d53a99"


# ---------------------------------------------------------------------------
# offline boundary for this module
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    """No replay code path may open a socket in this test process."""

    def blocked(*args, **kwargs):
        raise AssertionError("replay tests forbid network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked, raising=True)


@pytest.fixture
def deny_processes(monkeypatch):
    """Fail if a unit-level replay case reaches process creation."""

    def blocked(*args, **kwargs):
        raise AssertionError("this replay case must refuse before any process")

    monkeypatch.setattr(replay, "_run_process", blocked)
    return blocked


@pytest.fixture
def work_base():
    """A disposable directory below the operator home (safe-root semantics)."""
    import uuid

    base = (
        Path.home()
        / ".hermes"
        / "cache"
        / "scratch"
        / f"replay-tests-{uuid.uuid4().hex}"
    )
    base.mkdir(parents=True)
    try:
        yield base
    finally:
        # Test-owned disposable fixtures only; never a host/global cleanup.
        if base.is_dir():
            import shutil

            shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# independent reference digest (deliberately not the implementation)
# ---------------------------------------------------------------------------


def _reference_digest(suite: Path) -> str:
    rows = []
    for path in sorted(p for p in suite.iterdir() if p.is_file()):
        if path.name == "expectations.json":
            payload = json.loads(path.read_text())
            payload.pop("suite_digest", None)
            blob = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        else:
            blob = path.read_bytes()
        rows.append(
            {
                "name": path.name,
                "bytes": len(blob),
                "sha256": "sha256:" + hashlib.sha256(blob).hexdigest(),
            }
        )
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
    )


# ---------------------------------------------------------------------------
# suite fixtures
# ---------------------------------------------------------------------------


def write_suite(
    suite: Path,
    *,
    target: str = "test_subject.py::test_subject_is_two",
    baseline: str = "assertion_failure",
    candidate: str = "pass",
    controls: list[dict] | None = None,
    timeout_s: int = 120,
    digest: str | None = None,
) -> str:
    suite.mkdir(parents=True, exist_ok=True)
    (suite / "pytest.ini").write_text("[pytest]\n")
    (suite / "test_subject.py").write_text(
        "def test_subject_is_two():\n"
        "    from factory import thing\n"
        "    assert thing.value() == 2\n"
        "\n"
        "\n"
        "def test_subject_is_positive():\n"
        "    from factory import thing\n"
        "    assert thing.value() >= 1\n"
    )
    if controls is None:
        controls = [
            {
                "nodeid": "test_subject.py::test_subject_is_positive",
                "baseline": {"outcome": "pass"},
                "candidate": {"outcome": "pass"},
            }
        ]
    expectations = {
        "schema": {"name": "factory-replay-expectations", "version": 1},
        "suite_id": "duplicate-json-key",
        "suite_digest": "sha256:" + "0" * 64,
        "cases": [
            {
                "nodeid": target,
                "baseline": {"outcome": baseline},
                "candidate": {"outcome": candidate},
            }
        ],
        "controls": controls,
        "timeout_s": timeout_s,
    }
    path = suite / "expectations.json"
    path.write_text(json.dumps(expectations, indent=2, sort_keys=True) + "\n")
    computed = _reference_digest(suite)
    expectations["suite_digest"] = digest if digest is not None else computed
    path.write_text(json.dumps(expectations, indent=2, sort_keys=True) + "\n")
    return computed


def expectations_path(suite: Path) -> Path:
    return suite / "expectations.json"


def rewrite_expectations(suite: Path, mutate) -> None:
    payload = json.loads(expectations_path(suite).read_text())
    mutate(payload)
    expectations_path(suite).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def paths(work: Path) -> dict:
    return {
        "repo": REPO,
        "baseline": BASELINE,
        "candidate": CANDIDATE,
        "suite_dir": work / "suite",
        "work_root": work / "run",
        "out": work / "out",
    }


def prepare(work: Path, **overrides) -> dict:
    """Build a complete, valid admission set and return the keyword arguments."""
    suite = work / "suite"
    digest = write_suite(suite)
    work_root = work / "run"
    work_root.mkdir()
    kwargs = {
        "repo": str(REPO),
        "baseline": BASELINE,
        "candidate": CANDIDATE,
        "suite_dir": str(suite),
        "suite_digest": digest,
        "work_root": str(work_root),
        "out": str(work / "out"),
        "approve_execution": True,
    }
    kwargs.update(overrides)
    return kwargs


def rejected(result) -> tuple[str, str]:
    assert result.verdict == "rejected", (result.verdict, result.reason, result.detail)
    assert result.ok is False
    return result.reason, result.detail


# ---------------------------------------------------------------------------
# C3 -- non-circular suite digest
# ---------------------------------------------------------------------------


def test_suite_digest_matches_an_independent_reference(work_base):
    suite = work_base / "suite"
    computed = write_suite(suite)

    assert replay.compute_suite_digest(suite) == computed == _reference_digest(suite)


def test_suite_digest_excludes_only_the_digest_field(work_base):
    suite = work_base / "suite"
    write_suite(suite)
    before = replay.compute_suite_digest(suite)

    rewrite_expectations(
        suite, lambda payload: payload.__setitem__("suite_digest", "sha256:" + "1" * 64)
    )

    assert replay.compute_suite_digest(suite) == before


def test_suite_digest_changes_when_expectation_content_changes(work_base):
    suite = work_base / "suite"
    write_suite(suite)
    before = replay.compute_suite_digest(suite)

    rewrite_expectations(
        suite,
        lambda payload: payload["controls"][0]["candidate"].__setitem__(
            "outcome", "assertion_failure"
        ),
    )

    assert replay.compute_suite_digest(suite) != before


def test_suite_digest_changes_when_a_test_file_changes(work_base):
    suite = work_base / "suite"
    write_suite(suite)
    before = replay.compute_suite_digest(suite)
    (suite / "test_subject.py").write_text("def test_other():\n    assert True\n")

    assert replay.compute_suite_digest(suite) != before


def test_suite_digest_ignores_file_order(work_base):
    suite = work_base / "suite"
    write_suite(suite)
    (suite / "a_extra_test.py").write_text("def test_extra():\n    assert True\n")
    first = replay.compute_suite_digest(suite)

    assert replay.compute_suite_digest(suite) == first == _reference_digest(suite)


# ---------------------------------------------------------------------------
# C3/C4 -- external digest authority and strict expectations
# ---------------------------------------------------------------------------


def test_external_digest_mismatch_is_rejected_without_execution(
    work_base, deny_processes
):
    kwargs = prepare(work_base, suite_digest="sha256:" + "2" * 64)

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "suite_digest_mismatch"
    assert not Path(kwargs["out"]).exists()
    assert not (Path(kwargs["work_root"]) / "baseline").exists()


def test_missing_external_digest_is_a_usage_error(work_base, capsys):
    kwargs = prepare(work_base)

    with pytest.raises(SystemExit) as excinfo:
        learning.main(
            [
                "replay",
                "--repo",
                kwargs["repo"],
                "--baseline",
                kwargs["baseline"],
                "--candidate",
                kwargs["candidate"],
                "--suite-dir",
                kwargs["suite_dir"],
                "--work-root",
                kwargs["work_root"],
                "--out",
                kwargs["out"],
                "--approve-execution",
            ]
        )

    assert excinfo.value.code == 2
    assert "suite-digest" in capsys.readouterr().err


def test_suite_missing_expectations_is_rejected(work_base, deny_processes):
    suite = work_base / "suite"
    digest = write_suite(suite)
    expectations_path(suite).unlink()
    work_root = work_base / "run"
    work_root.mkdir()

    result = replay.replay(
        repo=str(REPO),
        baseline=BASELINE,
        candidate=CANDIDATE,
        suite_dir=str(suite),
        suite_digest=digest,
        work_root=str(work_root),
        out=str(work_base / "out"),
        approve_execution=True,
    )

    reason, _detail = rejected(result)
    assert reason in {"suite_invalid", "suite_digest_mismatch"}


def test_suite_symlink_is_rejected(work_base, deny_processes):
    suite = work_base / "suite"
    write_suite(suite)
    link = work_base / "suite-link"
    link.symlink_to(suite, target_is_directory=True)
    work_root = work_base / "run"
    work_root.mkdir()

    result = replay.replay(
        repo=str(REPO),
        baseline=BASELINE,
        candidate=CANDIDATE,
        suite_dir=str(link),
        suite_digest=_reference_digest(suite),
        work_root=str(work_root),
        out=str(work_base / "out"),
        approve_execution=True,
    )

    reason, _detail = rejected(result)
    assert reason in {"suite_invalid", "suite_digest_mismatch"}


def test_expectations_that_do_not_require_a_red_to_green_transition_are_rejected(
    work_base, deny_processes
):
    suite = work_base / "suite"
    write_suite(suite, baseline="pass", candidate="pass")
    work_root = work_base / "run"
    work_root.mkdir()

    result = replay.replay(
        repo=str(REPO),
        baseline=BASELINE,
        candidate=CANDIDATE,
        suite_dir=str(suite),
        suite_digest=replay.compute_suite_digest(suite),
        work_root=str(work_root),
        out=str(work_base / "out"),
        approve_execution=True,
    )

    reason, _detail = rejected(result)
    assert reason == "unsupported_suite"


def test_control_expectations_must_be_pass_on_both_sides(work_base, deny_processes):
    suite = work_base / "suite"
    write_suite(
        suite,
        controls=[
            {
                "nodeid": "test_subject.py::test_subject_is_positive",
                "baseline": {"outcome": "assertion_failure"},
                "candidate": {"outcome": "pass"},
            }
        ],
    )
    work_root = work_base / "run"
    work_root.mkdir()

    result = replay.replay(
        repo=str(REPO),
        baseline=BASELINE,
        candidate=CANDIDATE,
        suite_dir=str(suite),
        suite_digest=replay.compute_suite_digest(suite),
        work_root=str(work_root),
        out=str(work_base / "out"),
        approve_execution=True,
    )

    reason, _detail = rejected(result)
    assert reason == "unsupported_suite"


def test_unknown_expectation_outcome_is_rejected(work_base, deny_processes):
    suite = work_base / "suite"
    write_suite(suite)
    rewrite_expectations(
        suite,
        lambda payload: payload["cases"][0]["baseline"].__setitem__(
            "outcome", "improved"
        ),
    )
    work_root = work_base / "run"
    work_root.mkdir()

    result = replay.replay(
        repo=str(REPO),
        baseline=BASELINE,
        candidate=CANDIDATE,
        suite_dir=str(suite),
        suite_digest=replay.compute_suite_digest(suite),
        work_root=str(work_root),
        out=str(work_base / "out"),
        approve_execution=True,
    )

    reason, _detail = rejected(result)
    assert reason == "unsupported_suite"


def test_suite_must_live_outside_the_repository(work_base, deny_processes):
    suite = REPO / "tests" / "fixtures" / "replay-suite"
    would_be_digest = replay.compute_suite_digest(suite)
    work_root = work_base / "run"
    work_root.mkdir()

    result = replay.replay(
        repo=str(REPO),
        baseline=BASELINE,
        candidate=CANDIDATE,
        suite_dir=str(suite),
        suite_digest=would_be_digest,
        work_root=str(work_root),
        out=str(work_base / "out"),
        approve_execution=True,
    )

    reason, _detail = rejected(result)
    assert reason == "suite_invalid"


# ---------------------------------------------------------------------------
# C1 -- explicit approval and safe, empty, disjoint roots
# ---------------------------------------------------------------------------


def test_replay_without_approval_refuses_before_any_work(work_base, deny_processes):
    kwargs = prepare(work_base, approve_execution=False)

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "approval_required"
    assert not Path(kwargs["out"]).exists()
    assert not (Path(kwargs["work_root"]) / "baseline").exists()


def test_cli_replay_without_approval_exits_two(work_base, deny_processes, capsys):
    kwargs = prepare(work_base, approve_execution=False)
    argv = [
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
        "--json",
    ]

    code = learning.main(argv)

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "rejected"
    assert payload["reason"] == "approval_required"
    assert payload["promotional"] is False
    assert payload["acceptance_evidence"] is False


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"baseline": "abc"}, "invalid_revision"),
        ({"candidate": "z" * 40}, "invalid_revision"),
        ({"baseline": CANDIDATE}, "identical_revisions"),
        ({"baseline": "0" * 40}, "unknown_revision"),
    ],
)
def test_revision_admission_refusals(work_base, deny_processes, override, reason):
    kwargs = prepare(work_base, **override)

    result = replay.replay(**kwargs)

    got, _detail = rejected(result)
    assert got == reason


def test_work_root_must_be_empty(work_base, deny_processes):
    kwargs = prepare(work_base)
    (Path(kwargs["work_root"]) / "leftover").write_text("x")

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "work_root_invalid"


def test_work_root_must_not_be_inside_the_repository(work_base, deny_processes):
    inside = REPO / ".pytest-replay-work"
    kwargs = prepare(work_base, work_root=str(inside))

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "work_root_invalid"
    assert not inside.exists()


def test_work_root_must_be_a_safe_explicit_directory(work_base, deny_processes):
    kwargs = prepare(work_base, work_root="/")

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "work_root_invalid"


def test_out_must_not_already_exist(work_base, deny_processes):
    kwargs = prepare(work_base)
    Path(kwargs["out"]).mkdir()

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "out_invalid"


def test_out_must_not_overlap_the_work_root(work_base, deny_processes):
    kwargs = prepare(work_base)
    kwargs["out"] = str(Path(kwargs["work_root"]) / "out")

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "out_invalid"


def test_timeout_must_be_bounded(work_base, deny_processes):
    kwargs = prepare(work_base, timeout_s=0)

    result = replay.replay(**kwargs)

    reason, _detail = rejected(result)
    assert reason == "invalid_timeout"
