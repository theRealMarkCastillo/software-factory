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


@pytest.fixture(autouse=True)
def compose_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "compose.yml").write_text("services:\n  test:\n    build: .\n")
    (tmp_path / "compose.test.yml").write_text("services: {}\n")


@pytest.mark.parametrize(
    "documents, remove_images",
    [
        (["services:\n  test:\n    build: .\n    image: custom:keep\n"], False),
        (
            [
                "services:\n  test:\n    build: .\n",
                "services:\n  test:\n    image: ${CUSTOM_IMAGE}\n",
            ],
            False,
        ),
        (["services:\n  test:\n    image: python:3.12-slim\n"], True),
        (["services:\n  test:\n    build: .\n"], True),
        (["include: other.yml\nservices: {}\n"], False),
        (["services:\n  test:\n    extends: other\n"], False),
        (["services: [invalid"], False),
        ([None], False),
        (["services: []\n"], False),
        (["services:\n  test: invalid\n"], False),
        (["- not-a-compose-mapping\n"], False),
        (["services:\n  test:\n    build: null\n    image: null\n"], False),
        (
            [
                "services:\n  test:\n    image: custom:keep\n",
                "services:\n  test:\n    build: .\n",
            ],
            False,
        ),
    ],
    ids=[
        "custom-tag",
        "override-tag",
        "pulled",
        "build-only",
        "include",
        "extends",
        "invalid-yaml",
        "missing",
        "invalid-services",
        "invalid-service",
        "invalid-document",
        "null-keys",
        "override-build",
    ],
)
def test_teardown_keeps_images_unless_static_files_prove_safe(
    monkeypatch, tmp_path, documents, remove_images
):
    files = []
    for index, document in enumerate(documents):
        path = tmp_path / f"input-{index}.yml"
        if document is not None:
            path.write_text(document)
        files.append(str(path))
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(policy.subprocess, "run", fake_run)
    policy.teardown(PROJECT, files)
    expected_args = (
        DOWN_ARGS
        if remove_images
        else ["down", "--volumes", "--remove-orphans", "--timeout", "30"]
    )
    expected = ["docker", "compose", "--project-name", PROJECT]
    for path in files:
        expected += ["--file", path]
    assert commands == [[*expected, *expected_args]]


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
