"""Project-scoped image cleanup through the existing Compose teardown path."""

import subprocess

import pytest

from factory import policy


DOWN_ARGS = [
    "down",
    "--volumes",
    "--remove-orphans",
    "--rmi",
    "local",
    "--timeout",
    "30",
]
PROJECT = "factory-image-cleanup-abc123"


@pytest.mark.parametrize("deployment", [False, True])
@pytest.mark.parametrize("with_directory", [False, True])
def test_teardown_removes_local_images_preserving_compose_kwargs(
    monkeypatch, tmp_path, deployment, with_directory
):
    calls = []
    result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def fake_run_compose(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(policy, "run_compose", fake_run_compose)
    directory = str(tmp_path) if with_directory else None
    actual = policy.teardown(
        PROJECT,
        ["compose.yml", "compose.test.yml"],
        deployment=deployment,
        project_directory=directory,
    )

    expected_kwargs = {}
    if with_directory:
        expected_kwargs["project_directory"] = directory
    if deployment:
        expected_kwargs["deployment"] = True
    assert calls == [
        ((PROJECT, ["compose.yml", "compose.test.yml"], DOWN_ARGS), expected_kwargs)
    ]
    assert actual is result


@pytest.mark.parametrize("deployment", [False, True])
@pytest.mark.parametrize("with_directory", [False, True])
def test_only_removal_command_is_compose_down_for_named_project(
    monkeypatch, tmp_path, deployment, with_directory
):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(policy.subprocess, "run", fake_run)
    directory = str(tmp_path) if with_directory else None
    policy.teardown(
        PROJECT,
        ["compose.yml"],
        deployment=deployment,
        project_directory=directory,
    )

    expected = ["docker", "compose", "--project-name", PROJECT]
    if with_directory:
        expected += ["--project-directory", directory]
    expected += ["--file", "compose.yml", *DOWN_ARGS]
    # Capturing every subprocess excludes separate image rm/prune or system prune.
    assert commands == [expected]


def test_teardown_returns_image_removal_failure_without_retry(monkeypatch):
    calls = []
    failure = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="image removal failed"
    )

    def fake_run_compose(*args, **kwargs):
        calls.append((args, kwargs))
        return failure

    monkeypatch.setattr(policy, "run_compose", fake_run_compose)
    assert policy.teardown(PROJECT, ["compose.yml"]) is failure
    assert len(calls) == 1


def test_teardown_preserves_down_exception(monkeypatch):
    error = subprocess.TimeoutExpired("docker compose", 900)

    def fake_run_compose(*args, **kwargs):
        raise error

    monkeypatch.setattr(policy, "run_compose", fake_run_compose)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        policy.teardown(PROJECT, ["compose.yml"])
    assert caught.value is error


def test_teardown_rejects_nonfactory_project_before_any_command(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("nonfactory teardown must not issue any command")

    monkeypatch.setattr(policy, "run_compose", forbidden)
    monkeypatch.setattr(policy.subprocess, "run", forbidden)
    with pytest.raises(policy.PolicyViolation):
        policy.teardown("unrelated-project", ["compose.yml"])
