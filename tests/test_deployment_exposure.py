"""Source exposure must be judged before deployment rewrites hide it."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from factory import policy, runner


@pytest.mark.parametrize(
    "mapping,allowed",
    [
        ("127.0.0.1:8080:80", True),
        ("[::1]:8080:80", True),
        ("127.0.0.1:8080:80/tcp", True),
        ("8080:80", False),
        ("80", False),
        (80, False),
        ("0.0.0.0:8080:80", False),
        ("[::]:8080:80", False),
        ("192.0.2.10:8080:80", False),
        (":8080:80", False),
        ("8080-8081:80-81", False),
        ("127.0.0.1:8080-8081:80-81", False),
        *[
            ({"target": 80, "published": 8080, "host_ip": host}, allowed)
            for host, allowed in [
                ("127.0.0.1", True),
                ("::1", True),
                ("0.0.0.0", False),
                ("::", False),
                ("192.0.2.10", False),
                ("", False),
                (None, False),
                ("${HOST_IP}", False),
            ]
        ],
        ({"target": 80, "published": 8080}, False),
        ({"target": 80, "published": "8080-8081"}, False),
        (
            {"target": 80, "published": "8080-8081", "host_ip": "127.0.0.1"},
            False,
        ),
    ],
)
def test_source_mapping_policy(mapping, allowed):
    doc = {"services": {"web": {"image": "nginx", "ports": [mapping]}}}
    if allowed:
        policy.assert_safe_compose_file(doc, deployment=True)
        assert policy.rewrite_deployment_ports(doc, [49001])[0]["target"] == 80
    else:
        with pytest.raises(policy.PolicyViolation):
            policy.assert_safe_compose_file(doc, deployment=True)


@pytest.mark.parametrize("overridden", [False, True])
def test_source_exposure_refusal_records_every_mapping_before_docker(
    monkeypatch, tmp_path, overridden
):
    repo = tmp_path / "repo"
    repo.mkdir()
    mappings = ["8080:80", {"target": 81, "published": 8081, "host_ip": "0.0.0.0"}]
    (repo / "compose.yml").write_text(
        yaml.safe_dump({"services": {"web": {"image": "nginx", "ports": mappings}}})
    )
    files = ["compose.yml"]
    if overridden:
        (repo / "override.yml").write_text(
            yaml.safe_dump(
                {"services": {"web": {"ports": ["127.0.0.1:8080:80"]}}}
            )
        )
        files.append("override.yml")
    script = repo / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(
        runner, "capture_source", lambda _: {"sha": "1" * 40, "status_porcelain": ""}
    )
    monkeypatch.setattr(
        runner,
        "resolve_base_info",
        lambda *a: {"ref": "main", "kind": "commit", "sha": "2" * 40},
    )
    monkeypatch.setattr(runner, "_run_preflight_gates", lambda *a: True)
    monkeypatch.setattr(
        policy.ProtectedProjectSnapshot,
        "capture",
        classmethod(lambda cls: policy.ProtectedProjectSnapshot({})),
    )
    monkeypatch.setenv("FACTORY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(policy, "_is_factory_port_free", lambda _: True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.run_factory_card(
        "exposure",
        str(repo),
        files,
        deployment=True,
        check="check.sh",
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.stage == "exposure_refused", result.to_dict()
    assert result.passed is False
    assert not [argv for argv in calls if argv[0] == "docker"]
    assert not (tmp_path / "state" / "ports" / ".lock").exists()
    for mapping in mappings:
        assert "web" in result.error
        assert repr(mapping) in result.error
    manifest = json.loads(
        (Path(result.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    assert manifest["execution"]["stage"] == "exposure_refused"
    assert manifest["execution"]["passed"] is False
    assert manifest["execution"]["error"] == result.error
    refusals = manifest["runtime"]["deployment"]["exposure_refusals"]
    assert [item["spec"] for item in refusals] == mappings
    assert all(item["service"] == "web" and item["reason"] for item in refusals)
    assert manifest["runtime"]["teardown"]["attempted"] is False
