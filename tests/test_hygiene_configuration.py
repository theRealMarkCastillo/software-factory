"""Public checkout configuration and documentation hygiene."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_default_dev_install_includes_orchestration_test_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    extras = project["project"]["optional-dependencies"]
    for dependency in extras["orchestration"]:
        assert dependency in extras["dev"]
        assert dependency in project["dependency-groups"]["dev"]


def test_local_checks_are_the_only_contributor_gate():
    assert not (ROOT / ".github/workflows/tests.yml").exists()
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    for command in (
        "pytest -q",
        "ruff check .",
        "ruff format --check .",
        "git diff --check",
    ):
        assert command in contributing
    for name in ("README.md", "CONTRIBUTING.md"):
        text = (ROOT / name).read_text()
        assert not re.search(r"\bCI\b|actions/workflows|GitHub Actions", text)


def test_design_docs_use_generic_roles():
    for name in ("evidence-gate-design.md", "failure-regression-design.md"):
        text = (ROOT / "docs" / name).read_text()
        # Case-insensitive: also catches a private repository namespace.
        assert not re.search(r"\b" + "ar" + r"x\b", text, re.IGNORECASE)
    rollout = (ROOT / "docs/langgraph-hybrid/rollout-2026-11-09.md").read_text()
    assert "build-" + "host.local" not in rollout
    assert "the " + "***" not in rollout


def test_failure_regression_authorization_example_is_valid_json():
    text = (ROOT / "docs/failure-regression.md").read_text()
    match = re.search(r"`authorization: (.*?)`", text, re.DOTALL)
    assert match
    assert json.loads(match[1])["grants"] == []
