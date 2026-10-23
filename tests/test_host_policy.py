"""Host configuration must add protection and fail closed before Docker."""

import json

import pytest

from factory import policy, runner


def configure(tmp_path, monkeypatch, data):
    path = tmp_path / "host.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("FACTORY_HOST_CONFIG", str(path))
    return path


def test_private_projects_are_protected_without_editing_engine(tmp_path, monkeypatch):
    configure(
        tmp_path,
        monkeypatch,
        {
            "protected_projects": ["private-app", "factory-owned-by-operator"],
            "reserved_host_ports": [49999],
        },
    )
    before = policy.ProtectedProjectSnapshot(
        projects={"private-app": [("db", "old", "running")]}
    )
    after = policy.ProtectedProjectSnapshot(
        projects={"private-app": [("db", "new", "running")]}
    )
    assert before.collateral_damage_report(after)["protected"]
    with pytest.raises(policy.PolicyViolation):
        policy.assert_factory_project("factory-owned-by-operator")
    assert 49999 in policy.reserved_host_ports()
    assert policy.FACTORY_RESERVED_HOST_PORTS <= policy.reserved_host_ports()
    assert policy.PROTECTED_PROJECTS <= policy.protected_projects()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"typo": []},
        {"protected_projects": "app"},
        {"protected_projects": [""]},
        {"reserved_host_ports": [True]},
        {"reserved_host_ports": [65536]},
    ],
)
def test_invalid_config_rejected(tmp_path, monkeypatch, data):
    configure(tmp_path, monkeypatch, data)
    with pytest.raises(policy.PolicyViolation, match="host config"):
        policy.assert_factory_project("factory-test")


def test_missing_config_stops_real_runner_before_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOST_CONFIG", str(tmp_path / "missing.json"))
    (tmp_path / "compose.yml").write_text("services:\n  test:\n    image: alpine\n")
    monkeypatch.setattr(
        policy.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not call Docker or subprocess"),
    )
    with pytest.raises(policy.PolicyViolation, match="host config"):
        runner.run_factory_card(
            "test",
            str(tmp_path),
            ["compose.yml"],
            artifacts_root=str(tmp_path / "artifacts"),
        )
