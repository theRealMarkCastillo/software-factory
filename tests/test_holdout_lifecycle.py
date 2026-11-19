"""Real Git/evidence and CLI; simulated policy Docker boundary."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from test_holdout import suite

from factory import policy, runner, verify
from factory.evidence import check_evidence, verify_evidence


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text(
        "services:\n  test:\n    image: python:3.12-slim\n    command: [python, -V]\n"
    )
    (repo / ".env.example").write_text("# no configuration required\n")
    for args in (
        ["init", "-b", "main"],
        ["add", "compose.yml", ".env.example"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    root = suite(tmp_path)
    calls = []
    state = {"holdout_code": 0, "visible_code": 0, "raise": False, "teardown_code": 0}
    monkeypatch.setattr(
        runner,
        "_run_preflight_gates",
        lambda repo, result, base: (
            result.gates.update(
                {
                    "audit": {"status": "no_manifest", "passed": True},
                    "coverage": {
                        "status": "skipped",
                        "reason": "no_added_python",
                        "base": "main",
                        "base_sha": sha,
                        "base_kind": "commit",
                    },
                    "datamodel": {"status": "skipped", "reason": "no_data_model"},
                }
            )
            or True
        ),
    )
    monkeypatch.setattr(
        policy.ProtectedProjectSnapshot, "capture", classmethod(lambda cls: cls())
    )

    def compose(project, files, args, **kwargs):
        secret = len(files) > 1
        calls.append((project, list(files), list(args)))
        if secret:
            override = yaml.safe_load(Path(files[-1]).read_text())
            svc = override["services"]["test"]
            assert svc["command"] == ["python", "/factory-holdout/test_secret.py"]
            assert svc["volumes"][-1] == {
                "type": "bind",
                "source": str(root),
                "target": "/factory-holdout",
                "read_only": True,
            }
            if state["raise"] and args[0] == "up":
                raise subprocess.TimeoutExpired(
                    "CANARY_HOLDOUT_SOURCE", 1, output="CANARY_HOLDOUT_SOURCE"
                )
            return subprocess.CompletedProcess(
                [],
                state["holdout_code"] if args[0] == "up" else 0,
                "CANARY_HOLDOUT_SOURCE\n"
                + ("1 failed, 2 passed\n" if state["holdout_code"] else "3 passed\n"),
                "CANARY_HOLDOUT_SOURCE",
            )
        return subprocess.CompletedProcess(
            [], state["visible_code"] if args[0] == "up" else 0, "visible", ""
        )

    def down(project, files, **kwargs):
        calls.append((project, list(files), ["down"]))
        return subprocess.CompletedProcess(
            [], state["teardown_code"] if len(files) > 1 else 0, "", ""
        )

    monkeypatch.setattr(runner, "run_compose", compose)
    monkeypatch.setattr(runner, "teardown", down)
    # Holdout lifecycle uses the same sanctioned runner boundaries.
    return repo, root, tmp_path / "artifacts", calls, state, sha


def run(lifecycle):
    repo, root, artifacts, *_ = lifecycle
    return runner.run_factory_card(
        "holdout",
        str(repo),
        ["compose.yml"],
        artifacts_root=str(artifacts),
        holdout=str(root),
    )


@pytest.mark.parametrize("code", [0, 1])
def test_fresh_holdout_project_private_output_and_evidence(lifecycle, code):
    repo, root, artifacts, calls, state, sha = lifecycle
    state["holdout_code"] = code
    result = run(lifecycle)
    assert result.passed is (code == 0)
    assert result.stage == ("complete" if code == 0 else "holdout_failed")
    ups = [call for call in calls if call[2][0] == "up"]
    assert len(ups) == 2
    assert ups[0][0] != ups[1][0]
    assert any(c[0] == ups[1][0] and c[2] == ["down"] for c in calls)
    public = Path(result.artifacts_dir)
    for path in public.rglob("*"):
        if path.is_file():
            assert b"CANARY_HOLDOUT_SOURCE" not in path.read_bytes()
    assert any(
        "CANARY_HOLDOUT_SOURCE" in p.read_text() for p in (root / "runs").rglob("*.log")
    )
    manifest = json.loads((public / "evidence-manifest.json").read_text())
    assert manifest["holdout_required"] is True
    block = manifest["holdout"]
    assert block["exit_code"] == code
    assert block["project_name"] == ups[1][0]
    assert block["passed"] == (2 if code else 3) and block["failed"] == (
        1 if code else 0
    )
    assert block["teardown_ok"] is True
    if code == 0:
        verified = verify_evidence(public, repo, sha, sha, holdout=str(root))
        assert verified.ok, verified.as_dict()
        assert verified.evidence["holdout"]["verification"] == "reverified"
        recorded = verify_evidence(public, repo, sha, sha)
        assert recorded.ok
        assert recorded.evidence["holdout"]["verification"] == "recorded_not_reverified"
        (root / "test_secret.py").write_text("tampered")
        assert (
            verify_evidence(public, repo, sha, sha, holdout=str(root)).reason
            == "holdout_digest_mismatch"
        )
        assert (
            verify.main(
                [
                    "--repo",
                    str(repo),
                    "--artifacts-dir",
                    str(public),
                    "--candidate",
                    sha,
                    "--base",
                    sha,
                    "--holdout",
                    str(root),
                    "--json",
                ]
            )
            == 1
        )


def test_visible_failure_never_runs_holdout(lifecycle):
    lifecycle[4]["visible_code"] = 1
    result = run(lifecycle)
    assert not result.passed
    assert len([c for c in lifecycle[3] if c[2][0] == "up"]) == 1


@pytest.mark.parametrize("failure", ["raise", "teardown_code"])
def test_holdout_exception_or_teardown_fails_closed(lifecycle, failure):
    lifecycle[4][failure] = True if failure == "raise" else 1
    result = run(lifecycle)
    assert not result.passed
    assert result.stage == "holdout_failed"
    assert len([c for c in lifecycle[3] if c[2] == ["down"]]) == 2
    for p in Path(result.artifacts_dir).rglob("*"):
        if p.is_file():
            assert b"CANARY_HOLDOUT_SOURCE" not in p.read_bytes()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "failed",
        "digest",
        "counts",
        "teardown",
        "safety",
        "same_project",
        "extra_output",
    ],
)
def test_declared_holdout_cannot_certify_without_success(lifecycle, mutation):
    result = run(lifecycle)
    manifest = json.loads(
        (Path(result.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    if mutation == "missing":
        manifest.pop("holdout")
    elif mutation == "failed":
        manifest["holdout"]["exit_code"] = 1
    elif mutation == "digest":
        manifest["holdout"]["manifest_sha256"] = "bad"
    elif mutation == "counts":
        manifest["holdout"]["failed"] = True
    elif mutation == "teardown":
        manifest["holdout"]["teardown_ok"] = False
    elif mutation == "safety":
        manifest["holdout"]["safety_clean"] = False
    elif mutation == "same_project":
        manifest["holdout"]["project_name"] = manifest["project_name"]
    elif mutation == "extra_output":
        manifest["holdout"]["stdout"] = "withheld"
    assert (
        check_evidence(manifest, artifacts_dir=result.artifacts_dir).reason
        == "holdout_invalid"
    )


def test_holdout_uses_visible_compose_project_directory(lifecycle, monkeypatch):
    repo, root, artifacts, calls, state, sha = lifecycle
    nested = repo / "deploy"
    nested.mkdir()
    (repo / "compose.yml").rename(nested / "compose.yml")
    original = runner.run_compose

    def check_directory(project, files, args, **kwargs):
        if len(files) > 1:
            assert kwargs["project_directory"] == str(nested)
        return original(project, files, args, **kwargs)

    monkeypatch.setattr(runner, "run_compose", check_directory)
    result = runner.run_factory_card(
        "nested",
        str(repo),
        ["deploy/compose.yml"],
        artifacts_root=str(artifacts),
        holdout=str(root),
    )
    assert result.stage != "holdout_failed"
    assert len([call for call in calls if call[2][0] == "up"]) == 2


def test_run_cli_holdout_argument(lifecycle, capsys):
    repo, root, artifacts, *_ = lifecycle
    assert (
        runner.main(
            [
                "--card",
                "cli",
                "--repo",
                str(repo),
                "--compose",
                "compose.yml",
                "--holdout",
                str(root),
                "--artifacts-root",
                str(artifacts),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["passed"] is True
