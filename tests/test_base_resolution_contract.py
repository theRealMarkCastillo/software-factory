"""Closed base grammar: real Git, offline lifecycle spies only."""

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from factory import evidence, policy, runner


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def subject(tmp_path):
    repo = tmp_path / "subject"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "compose.yml").write_text("services:\n  test:\n    image: example:test\n")
    (repo / ".env.example").write_text("EXAMPLE=value\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "slice", base)
    git(repo, "commit", "--allow-empty", "-qm", "candidate")
    head = git(repo, "rev-parse", "HEAD")
    return repo, base, head


@pytest.fixture
def lifecycle(monkeypatch):
    calls = []

    def compose(*args, **kwargs):
        calls.append("compose")
        return subprocess.CompletedProcess([], 0, "offline output", "")

    def snapshot(cls):
        calls.append("snapshot")
        return policy.ProtectedProjectSnapshot()

    def gates(*args):
        calls.append("gate")
        return False

    monkeypatch.setattr(runner, "run_compose", compose)
    monkeypatch.setattr(runner, "teardown", compose)
    monkeypatch.setattr(runner, "_run_preflight_gates", gates)
    monkeypatch.setattr(
        runner.ProtectedProjectSnapshot, "capture", classmethod(snapshot)
    )
    return calls


def run(repo, tmp_path, **kwargs):
    return runner.run_factory_card(
        "base-contract",
        str(repo),
        ["compose.yml"],
        artifacts_root=str(tmp_path / "artifacts"),
        **kwargs,
    )


def refused(result, calls, code):
    assert not result.passed
    assert result.stage == "base_resolution"
    assert code in result.error
    assert result.artifacts_dir is None
    assert calls == []


@pytest.mark.parametrize(
    "spelling",
    [
        "sha",
        "uppercase_sha",
        "HEAD",
        "refs/heads/slice",
        "slice",
        "tag",
        "remote",
        "annotated",
        "custom",
    ],
)
def test_accepted_spellings(subject, spelling):
    repo, base, head = subject
    git(repo, "tag", "release", base)
    git(repo, "tag", "-a", "annotated", "-m", "tag", base)
    git(repo, "update-ref", "refs/remotes/origin/slice", base)
    git(repo, "update-ref", "refs/custom/base", base)
    ref = {
        "sha": base,
        "uppercase_sha": base.upper(),
        "tag": "release",
        "remote": "origin/slice",
        "custom": "refs/custom/base",
    }.get(spelling, spelling)
    assert evidence.resolve_base_info(repo, ref) == {
        "ref": ref,
        "kind": "commit",
        "sha": head if ref == "HEAD" else base,
    }


@pytest.mark.parametrize("gates", [True, False])
@pytest.mark.parametrize("same_commit", [True, False])
@pytest.mark.parametrize(
    "namespaces",
    [
        ("heads", "tags"),
        ("heads", "remotes"),
        ("tags", "remotes"),
        ("heads", "tags", "remotes"),
    ],
)
def test_collisions_refused(
    subject, lifecycle, tmp_path, gates, same_commit, namespaces
):
    repo, base, head = subject
    for index, namespace in enumerate(namespaces):
        git(
            repo,
            "update-ref",
            f"refs/{namespace}/collision",
            base if same_commit or index == 0 else head,
        )
    result = run(repo, tmp_path, base_ref="collision", run_gates=gates)
    refused(result, lifecycle, "ambiguous_base")
    for namespace in namespaces:
        assert f"refs/{namespace}/collision" in result.error


@pytest.mark.parametrize("namespace", ["heads", "tags"])
def test_qualified_collision_disambiguates(subject, lifecycle, tmp_path, namespace):
    repo, base, head = subject
    git(repo, "tag", "slice", head)
    result = run(repo, tmp_path, base_ref=f"refs/{namespace}/slice", run_gates=False)
    manifest = json.loads(
        (Path(result.artifacts_dir) / evidence.MANIFEST_FILENAME).read_text()
    )
    assert manifest["base"] == {
        "ref": f"refs/{namespace}/slice",
        "kind": "commit",
        "sha": base if namespace == "heads" else head,
    }
    assert lifecycle.count("compose") == 3


@pytest.mark.parametrize("gates", [True, False])
@pytest.mark.parametrize(
    "ref",
    [
        "main~1",
        "HEAD^",
        "main:compose.yml",
        "main@{0}",
        "a..b",
        "a\\b",
        "a b",
        "a\tb",
        "-main",
        "",
        "abc1234",
        "a" * 39,
        "a" * 41,
        "refs/heads/slice~1",
        "refs/heads/slice^{commit}",
    ],
)
def test_unsupported_refused(subject, lifecycle, tmp_path, gates, ref):
    repo, _, _ = subject
    refused(
        run(repo, tmp_path, base_ref=ref, run_gates=gates),
        lifecycle,
        "unsupported_base",
    )


@pytest.mark.parametrize("gates", [True, False])
@pytest.mark.parametrize("ref", ["missing", "refs/heads/missing", "0" * 40, "main"])
def test_missing_explicit_refused(subject, lifecycle, tmp_path, gates, ref):
    repo, _, _ = subject
    git(repo, "branch", "-m", "trunk")
    refused(
        run(repo, tmp_path, base_ref=ref, run_gates=gates), lifecycle, "base_not_found"
    )


@pytest.mark.parametrize("ref_kind", ["qualified", "simple", "object"])
def test_noncommit_refused(subject, lifecycle, tmp_path, ref_kind):
    repo, _, _ = subject
    blob = git(repo, "rev-parse", "HEAD:compose.yml")
    git(repo, "update-ref", "refs/tags/blob", blob)
    ref = {"qualified": "refs/tags/blob", "simple": "blob", "object": blob}[ref_kind]
    refused(run(repo, tmp_path, base_ref=ref), lifecycle, "base_not_found")


@pytest.mark.parametrize("api", [True, False])
def test_omitted_missing_main_retains_diagnostics(
    subject, lifecycle, tmp_path, capsys, api
):
    repo, _, _ = subject
    git(repo, "branch", "-m", "trunk")
    if api:
        result = run(repo, tmp_path, run_gates=False).to_dict()
    else:
        assert (
            runner.main(
                [
                    "--card",
                    "base-contract",
                    "--repo",
                    str(repo),
                    "--compose",
                    "compose.yml",
                    "--artifacts-root",
                    str(tmp_path / "artifacts"),
                    "--no-gates",
                ]
            )
            == 1
        )
        result = json.loads(capsys.readouterr().out)
    assert lifecycle.count("compose") == 3
    assert lifecycle.count("snapshot") == 2
    assert not result["passed"]
    assert result["stage"] == "evidence_enforcement"
    assert "malformed_manifest" in result["error"]
    manifest = json.loads(
        (Path(result["artifacts_dir"]) / evidence.MANIFEST_FILENAME).read_text()
    )
    assert manifest["base"] == {"ref": "main", "kind": "unresolved", "sha": ""}
    assert (Path(result["artifacts_dir"]) / "compose.log").exists()


@pytest.mark.parametrize("gates", [True, False])
def test_omitted_ambiguous_main_refused(subject, lifecycle, tmp_path, gates):
    repo, base, _ = subject
    git(repo, "tag", "main", base)
    refused(run(repo, tmp_path, run_gates=gates), lifecycle, "ambiguous_base")


def test_cli_explicit_missing_main_refused(subject, lifecycle, tmp_path, capsys):
    repo, _, _ = subject
    git(repo, "branch", "-m", "trunk")
    assert (
        runner.main(
            [
                "--card",
                "base-contract",
                "--repo",
                str(repo),
                "--compose",
                "compose.yml",
                "--artifacts-root",
                str(tmp_path / "artifacts"),
                "--base",
                "main",
                "--no-gates",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "base_resolution"
    assert "base_not_found" in result["error"]
    assert result["artifacts_dir"] is None
    assert lifecycle == []


def test_api_omitted_unique_main(subject, lifecycle, tmp_path):
    repo, _, head = subject
    result = run(repo, tmp_path, run_gates=False)
    manifest = json.loads(
        (Path(result.artifacts_dir) / evidence.MANIFEST_FILENAME).read_text()
    )
    assert manifest["base"] == {"ref": "main", "kind": "commit", "sha": head}
    assert lifecycle.count("compose") == 3


def test_api_and_cli_default_none_and_help_main(subject, monkeypatch, capsys):
    assert (
        inspect.signature(runner.run_factory_card).parameters["base_ref"].default
        is None
    )
    received = []

    def capture(**kwargs):
        received.append(kwargs["base_ref"])
        return runner.RunResult("base-contract", "")

    monkeypatch.setattr(runner, "run_factory_card", capture)
    runner.main(
        [
            "--card",
            "base-contract",
            "--repo",
            str(subject[0]),
            "--compose",
            "compose.yml",
        ]
    )
    assert received == [None]
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        runner.main(["--help"])
    assert exc.value.code == 0
    assert "default: main" in capsys.readouterr().out
