"""Narrow generated-override policy and CLI negative coverage."""

import subprocess

import pytest
from test_holdout import suite

from factory import policy, runner


def test_teardown_preserves_holdout_project_directory(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        policy,
        "run_compose",
        lambda *a, **kw: calls.append((a, kw)) or subprocess.CompletedProcess([], 0),
    )
    policy.teardown(
        "factory-holdout-abc",
        ["base.yml", "private.yml"],
        project_directory=str(tmp_path),
    )
    assert calls[0][1]["project_directory"] == str(tmp_path)


@pytest.mark.parametrize(
    "flag", [["--deployment"], ["--no-gates"], ["--mode", "diagnostic"]]
)
def test_holdout_cli_refuses_modes(flag, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        runner.main(
            [
                "--card",
                "x",
                "--repo",
                str(tmp_path),
                "--compose",
                "compose.yml",
                "--holdout",
                str(tmp_path / "private"),
                *flag,
            ]
        )
    assert exc.value.code == 2
    assert "--holdout forbids" in capsys.readouterr().err


def test_override_is_not_a_general_mount_exemption(tmp_path):
    root = suite(tmp_path)
    model = {"services": {"test": {"image": "python:3.12-slim", "privileged": True}}}
    with pytest.raises(policy.PolicyViolation, match="privileged"):
        policy.holdout_override(model, str(root), tmp_path / "repo")


def test_override_quotes_dollars(tmp_path):
    import json

    root = suite(tmp_path)
    manifest = root / "holdout.json"
    doc = json.loads(manifest.read_text())
    doc["command"] = ["echo", "$SECRET"]
    manifest.write_text(json.dumps(doc))
    model = {"services": {"test": {"image": "python:3.12-slim"}}}
    override = policy.holdout_override(model, str(root), tmp_path / "repo")
    assert override["services"]["test"]["command"] == ["echo", "$$SECRET"]
