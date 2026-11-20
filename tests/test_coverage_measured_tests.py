"""Real coverage measurements retain anti-bypass verdicts and explain scoping."""

import pytest
from test_runner_cli_regressions import _commit, _git

from factory.coverage import run_coverage_gate


@pytest.fixture
def measured_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("value = 1\n")
    base = _commit(repo, "base")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "import app\n\ndef unused():\n    return 2\n\n"
        "def test_value():\n    assert app.value == 1\n"
    )
    return repo, base


def test_measured_test_failure_explains_production_scoping(measured_repo):
    repo, base = measured_repo
    result = run_coverage_gate(str(repo), base=base, require_fresh=True)
    assert result.passed is False
    assert result.status == "fail"
    entry = next(f for f in result.files if f["file"] == "tests/test_app.py")
    assert entry["missed_lines"] == [4]
    assert entry["measured_test_file"] is True
    assert (
        "'tests/test_app.py' is classified as a test file but coverage measured it"
        in result.detail
    )
    assert "gated like production" in result.detail
    assert "[tool.coverage.run] source = [...] (do not omit it)" in result.detail
    assert "pragma" not in result.detail


def test_production_scoping_passes_without_omitting_tests(measured_repo):
    repo, base = measured_repo
    (repo / "pyproject.toml").write_text('[tool.coverage.run]\nsource = ["app"]\n')
    result = run_coverage_gate(str(repo), base=base, require_fresh=True)
    assert result.passed is True
    assert result.files == [
        {"file": "tests/test_app.py", "added_lines": 7, "skipped_reason": "test_file"}
    ]


def test_uncovered_production_message_is_unchanged(measured_repo):
    repo, base = measured_repo
    (repo / "app.py").write_text("value = 1\n\ndef unused():\n    return 2\n")
    (repo / "pyproject.toml").write_text('[tool.coverage.run]\nsource = ["app"]\n')
    result = run_coverage_gate(str(repo), base=base, require_fresh=True)
    assert result.passed is False
    assert result.detail == (
        "uncovered added executable line(s) [4] in 'app.py' "
        "(add a test that executes them, or an inline '# pragma: no cover' on the same line)"
    )
    assert all("measured_test_file" not in entry for entry in result.files)
