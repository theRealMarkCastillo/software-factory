"""Deployment evidence is certifiable but never promotion input."""

import hashlib

import pytest
from test_verify import _clean_runtime, _evidence, _repo, _rewrite

from factory.evidence import REQUIRED_ARTIFACTS, verify_evidence


def deployment_evidence(tmp_path):
    script = "#!/bin/sh\nexit 0\n"
    repo, base, candidate = _repo(tmp_path, candidate_files={"check.sh": script})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    compose = 'services:\n  web:\n    image: nginx\n    ports: ["127.0.0.1:49000:80"]\n'
    (artifacts / "compose.resolved.yml").write_text(compose)
    for name in ("check.stdout.log", "check.stderr.log"):
        (artifacts / name).write_text("")
    runtime = _clean_runtime()
    runtime["deployment"] = {
        "ports": [
            {"service": "web", "target": 80, "published": 49000, "host_ip": "127.0.0.1"}
        ],
        "compose_sha256": hashlib.sha256(compose.encode()).hexdigest(),
        "check": {
            "path": "check.sh",
            "sha256": hashlib.sha256(script.encode()).hexdigest(),
        },
        "wait": {"ok": True, "exit_code": 0},
        "check_outcome": {"ok": True, "exit_code": 0},
    }
    _evidence(
        tmp_path,
        repo,
        base,
        candidate,
        mode="deployment",
        runtime=runtime,
        artifacts=[
            *REQUIRED_ARTIFACTS,
            "compose.resolved.yml",
            "check.stdout.log",
            "check.stderr.log",
        ],
    )
    return repo, base, candidate, artifacts


def test_promote_refuses_deployment(tmp_path):
    from test_verify import _git

    from factory.promote import promote_ref

    repo, base, candidate, artifacts = deployment_evidence(tmp_path)
    _git(repo, "branch", "release", base)
    result = promote_ref(repo, artifacts, candidate, base, "refs/heads/release")
    assert result.reason == "deployment_not_promotable", result.as_dict()
    assert _git(repo, "rev-parse", "release") == base


def test_verify_deployment(tmp_path):
    repo, base, candidate, artifacts = deployment_evidence(tmp_path)
    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.ok, result.as_dict()
    assert result.evidence["mode"] == "deployment"


@pytest.mark.parametrize("field", ["compose_sha256", "check", "ports"])
def test_verify_refuses_deployment_tampering(tmp_path, field):
    repo, base, candidate, artifacts = deployment_evidence(tmp_path)

    def mutate(manifest):
        dep = manifest["runtime"]["deployment"]
        if field == "check":
            dep["check"]["sha256"] = "0" * 64
        elif field == "ports":
            dep["ports"][0]["published"] += 1
        else:
            dep[field] = "0" * 64

    _rewrite(artifacts, mutate)
    result = verify_evidence(artifacts, repo, candidate, base)
    assert not result.ok
    assert "deployment" in result.reason, result.as_dict()
