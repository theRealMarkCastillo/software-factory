"""Deployment validation remains an explicit, isolated evidence class."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from factory import policy, runner
from factory.policy import ProtectedProjectSnapshot


@pytest.mark.parametrize("args", [[], ["--test-service", "test"]])
def test_cli_deployment_requires_check_and_no_test_service(args, tmp_path):
    with pytest.raises(SystemExit) as error:
        runner.main(
            [
                "--card",
                "deploy",
                "--repo",
                str(tmp_path),
                "--compose",
                "compose.yml",
                "--deployment",
                *args,
            ]
        )
    assert error.value.code == 2


@pytest.mark.parametrize(
    "failure,stage",
    [
        (None, "complete"),
        ("wait", "deployment_wait_failed"),
        ("check", "deployment_check_failed"),
        ("timeout", "deployment_check_timeout"),
        ("teardown", "deployment_teardown_failed"),
    ],
)
def test_deployment_lifecycle(monkeypatch, tmp_path, failure, stage):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text(
        'services:\n  web-api:\n    image: nginx\n    ports: ["127.0.0.1:8000:80"]\n  optional:\n    image: nginx\n    profiles: [debug]\n'
    )
    script = repo / "check.sh"
    script.write_text(
        "#!/bin/sh\nprintf '%s' \"$FACTORY_HOST:$FACTORY_PORT_WEB_API_80\"\n"
    )
    script.chmod(0o755)
    (repo / ".env.example").write_text("# no configuration\n")
    source = {"sha": "1" * 40, "status_porcelain": ""}
    monkeypatch.setattr(runner, "capture_source", lambda _: source)
    monkeypatch.setattr(
        runner,
        "resolve_base_info",
        lambda *a: {"ref": "main", "kind": "commit", "sha": "2" * 40},
    )

    def gates(repo, result, base):
        result.gates = {
            "audit": {"status": "pass", "passed": True},
            "coverage": {"status": "pass", "passed": True, "base_sha": base["sha"]},
            "datamodel": {"status": "skipped", "reason": "no_data_model"},
        }
        return True

    monkeypatch.setattr(runner, "_run_preflight_gates", gates)
    monkeypatch.setattr(
        ProtectedProjectSnapshot,
        "capture",
        classmethod(lambda cls: ProtectedProjectSnapshot({})),
    )
    monkeypatch.setenv("FACTORY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COMPOSE_PROFILES", "debug")
    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret")
    monkeypatch.setattr(policy, "_is_factory_port_free", lambda p: True)
    lock = tmp_path / "state" / "ports" / ".lock"
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] != "docker":
            if cmd == [str(script)]:
                calls.append("check")
                assert lock.exists()
                assert kwargs["cwd"] == repo
                assert set(kwargs["env"]) <= {
                    "PATH",
                    "HOME",
                    "TMPDIR",
                    "LANG",
                    "FACTORY_HOST",
                    "FACTORY_PORT_WEB_API_80",
                }
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(cmd, 1)
                if failure == "check":
                    return subprocess.CompletedProcess(
                        cmd, 1, "failed", "check failure"
                    )
            return real_run(cmd, **kwargs)
        assert lock.exists()
        assert kwargs["env"]["COMPOSE_PROFILES"] == ""
        resolved = Path(cmd[cmd.index("--file") + 1])
        assert resolved.name == "compose.resolved.yml"
        doc = yaml.safe_load(resolved.read_text())
        assert doc["services"]["web-api"]["ports"] == ["127.0.0.1:49000:80"]
        if "up" in cmd:
            calls.append("wait")
            assert cmd[cmd.index("up") :] == [
                "up",
                "-d",
                "--build",
                "--wait",
                "--wait-timeout",
                "300",
            ]
            return subprocess.CompletedProcess(cmd, int(failure == "wait"), "up", "")
        if "down" in cmd:
            calls.append("teardown")
            return subprocess.CompletedProcess(cmd, int(failure == "teardown"), "", "")
        calls.append("logs")
        return subprocess.CompletedProcess(cmd, 0, "logs", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.run_factory_card(
        "deploy",
        str(repo),
        ["compose.yml"],
        deployment=True,
        check="check.sh",
        artifacts_root=str(tmp_path / "artifacts"),
    )
    assert result.stage == stage, result.to_dict()
    assert result.passed is (failure is None)
    assert not lock.exists()
    assert calls == (
        ["wait", "logs", "teardown"]
        if failure == "wait"
        else ["wait", "check", "logs", "teardown"]
    )
    manifest = json.loads(
        (Path(result.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    assert manifest["mode"] == "deployment"
    dep = manifest["runtime"]["deployment"]
    assert dep["ports"] == [
        {"service": "web-api", "target": 80, "published": 49000, "host_ip": "127.0.0.1"}
    ]
    assert len(dep["check"]["sha256"]) == 64
    assert len(dep["compose_sha256"]) == 64
    assert manifest["runtime"]["teardown"]["ok"] is (failure != "teardown")
    if failure is None:
        assert (
            Path(result.artifacts_dir) / "check.stdout.log"
        ).read_text() == "127.0.0.1:49000"


@pytest.mark.parametrize(
    "mapping",
    [
        "127.0.0.1:8080:80",
        "127.0.0.1::80",
        "[::1]:8080:80",
        {"target": 80, "published": 8080, "host_ip": "127.0.0.1"},
    ],
)
def test_deployment_port_rewrite(mapping):
    doc = {"services": {"web": {"image": "nginx", "ports": [mapping]}}}
    policy.assert_safe_compose_file(doc, deployment=True)
    ports = policy.rewrite_deployment_ports(doc, [49001])
    assert doc["services"]["web"]["ports"] == ["127.0.0.1:49001:80"]
    assert ports == [
        {"service": "web", "target": 80, "published": 49001, "host_ip": "127.0.0.1"}
    ]


@pytest.mark.parametrize(
    "mapping",
    [
        "80-81",
        "8080-8081:80",
        "80/udp",
        {"target": 80, "protocol": "udp"},
        {"target": 80, "mode": "host"},
        {"target": "80-81"},
        "${PORT}:80",
        True,
        "0:80",
        "65536:80",
    ],
)
def test_deployment_rejects_unsafe_ports(mapping):
    with pytest.raises(policy.PolicyViolation):
        policy.assert_safe_compose_file(
            {"services": {"web": {"ports": [mapping]}}}, deployment=True
        )


@pytest.mark.parametrize(
    "field",
    [
        {"network_mode": "host"},
        {"container_name": "web"},
        {"privileged": True},
        {"restart": "always"},
        {"restart": "unless-stopped"},
        {"volumes": ["/var/run/docker.sock:/sock"]},
    ],
)
def test_deployment_retains_other_policy(field):
    with pytest.raises(policy.PolicyViolation):
        policy.assert_safe_compose_file(
            {"services": {"web": {**field, "ports": ["80"]}}}, deployment=True
        )


def test_default_still_refuses_ports():
    with pytest.raises(
        policy.PolicyViolation, match="host port publication is forbidden by default"
    ):
        policy.assert_safe_compose_file({"services": {"web": {"ports": ["80"]}}})
