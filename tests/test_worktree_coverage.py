"""Real Git regressions for coverage of the worktree sent to the build."""

import subprocess

import pytest

from factory import runner
from factory.coverage import _diff_added_lines
from factory.runner import _diff_has_added_python


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("state", ["committed", "staged", "unstaged", "untracked"])
@pytest.mark.parametrize("nested", [False, True])
def test_coverage_includes_actual_worktree(tmp_path, state, nested):
    _git(tmp_path, "init", "-b", "main")
    repo = tmp_path / "subject" if nested else tmp_path
    repo.mkdir(exist_ok=True)
    source = repo / "app.py"
    source.write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    _git(tmp_path, "checkout", "-b", "work")
    target = repo / "new.py" if state == "untracked" else source
    target.write_text("x = 1\nraise RuntimeError('untested')\n")
    if state in ("staged", "committed"):
        _git(tmp_path, "add", ".")
    if state == "committed":
        _git(
            tmp_path,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "change",
        )
    added, error = _diff_added_lines(repo, "main")
    assert not error
    assert (
        added[target.relative_to(tmp_path).as_posix()][2]
        == "raise RuntimeError('untested')"
    )
    assert _diff_has_added_python(repo, "main")


def test_unchanged_worktree_has_no_coverage_delta(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "app.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    assert _diff_added_lines(tmp_path, "main") == ({}, "")


@pytest.mark.parametrize("new_file", [False, True])
def test_runner_blocks_uncovered_worktree_before_docker(
    tmp_path, monkeypatch, new_file
):
    """Run the real coverage subprocess over dirty code through the runner."""
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "test_app.py").write_text(
        "import app\n\ndef test_x():\n    assert app.x == 1\n"
    )
    (tmp_path / "compose.yml").write_text("services:\n  test:\n    image: alpine\n")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "base",
    )
    target = tmp_path / ("new.py" if new_file else "app.py")
    target.write_text("x = 1\n\ndef unused():\n    return 123\n")
    monkeypatch.setattr(
        runner, "run_dependency_audit", lambda *a, **k: {"status": "no_manifest"}
    )

    def no_docker():
        pytest.fail("uncovered dirty code must be rejected before Docker")

    monkeypatch.setattr(runner.ProtectedProjectSnapshot, "capture", no_docker)
    result = runner.run_factory_card(
        "dirty",
        str(tmp_path),
        ["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert not result.passed
    assert result.stage == "coverage_gate"
    assert result.gates["coverage"]["test_exit_code"] == 0
    assert result.gates["coverage"]["files"]
