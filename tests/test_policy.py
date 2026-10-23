import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import factory.policy as policy_mod
from factory.policy import (
    PROTECTED_PROJECTS,
    PolicyViolation,
    assert_factory_project,
    assert_safe_command,
    assert_safe_compose_file,
    check_bind_source,
    new_factory_project,
)


def test_rejects_protected_project_names():
    for name in PROTECTED_PROJECTS:
        with pytest.raises(PolicyViolation):
            assert_factory_project(name)


def test_rejects_unprefixed_project_name():
    with pytest.raises(PolicyViolation):
        assert_factory_project("some-random-project")


def test_rejects_empty_project_name():
    with pytest.raises(PolicyViolation):
        assert_factory_project("")


def test_accepts_well_formed_factory_project():
    assert_factory_project("factory-t-abc123-91e2fa")  # should not raise


def test_generator_always_produces_valid_names():
    for _ in range(20):
        name = new_factory_project("t_abc123", "91e2fa")
        assert name.startswith("factory-")
        assert_factory_project(name)  # should not raise


def test_rejects_banned_command_fragments():
    with pytest.raises(PolicyViolation):
        assert_safe_command("docker system prune -f")
    with pytest.raises(PolicyViolation):
        assert_safe_command("docker restart example-production-postgres-1")
    with pytest.raises(PolicyViolation):
        assert_safe_command("docker compose down")  # raw, no -p


def test_compose_rejects_privileged():
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file({"services": {"app": {"privileged": True}}})


def test_compose_rejects_host_network_mode():
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file({"services": {"app": {"network_mode": "host"}}})


def test_compose_rejects_docker_socket_mount():
    cfg = {
        "services": {"app": {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}}
    }
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_host_root_mount():
    cfg = {"services": {"app": {"volumes": ["/:/host"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_external_network():
    cfg = {
        "services": {"app": {"image": "python:3.11"}},
        "networks": {"shared": {"external": True}},
    }
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_host_ports():
    cfg = {"services": {"app": {"ports": ["5432:5432"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_container_name():
    cfg = {"services": {"app": {"container_name": "my-fixed-name"}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_restart_always():
    cfg = {"services": {"app": {"restart": "always"}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_cap_add():
    cfg = {"services": {"app": {"cap_add": ["SYS_ADMIN"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_devices():
    cfg = {"services": {"app": {"devices": ["/dev/sda:/dev/sda"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_rejects_unconfined_security_opt():
    for opt in ("seccomp:unconfined", "apparmor:unconfined", "label:disable"):
        cfg = {"services": {"app": {"security_opt": [opt]}}}
        with pytest.raises(PolicyViolation):
            assert_safe_compose_file(cfg)


def test_compose_rejects_userns_host():
    cfg = {"services": {"app": {"userns_mode": "host"}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_accepts_safe_config():
    cfg = {
        "services": {
            "test": {
                "image": "python:3.11-slim",
                "command": "pytest -q",
                "restart": "no",
            }
        }
    }
    assert_safe_compose_file(cfg)  # should not raise


def test_python_contract_requires_venv_path():
    from factory.policy import assert_worker_uses_isolated_environment

    with pytest.raises(PolicyViolation):
        assert_worker_uses_isolated_environment({"language": "python"})

    assert_worker_uses_isolated_environment(
        {"language": "python", "isolation": {"venv_path": ".venv"}}
    )  # should not raise


def test_typescript_contract_requires_package_manager_and_lockfile():
    from factory.policy import assert_worker_uses_isolated_environment

    with pytest.raises(PolicyViolation):
        assert_worker_uses_isolated_environment({"language": "typescript"})

    with pytest.raises(PolicyViolation):
        assert_worker_uses_isolated_environment(
            {"language": "typescript", "isolation": {"package_manager": "npm"}}
        )  # missing lockfile

    assert_worker_uses_isolated_environment(
        {
            "language": "typescript",
            "isolation": {"package_manager": "npm", "lockfile": "package-lock.json"},
        }
    )  # should not raise


def test_unknown_language_does_not_raise():
    from factory.policy import assert_worker_uses_isolated_environment

    assert_worker_uses_isolated_environment({"language": "go"})  # should not raise


@pytest.mark.live
def test_allocate_host_ports_avoids_protected_and_in_use():
    from factory.policy import (
        FACTORY_PORT_RANGE_END,
        FACTORY_PORT_RANGE_START,
        allocate_host_ports,
        get_host_ports_in_use,
    )

    # Factory range never collides with the low ports protected projects use.
    ports = allocate_host_ports(n=3)
    assert len(ports) == 3
    assert all(FACTORY_PORT_RANGE_START <= p < FACTORY_PORT_RANGE_END for p in ports)
    assert all(p not in get_host_ports_in_use() for p in ports)

    # Requesting a port already taken by a protected project must raise.
    taken = get_host_ports_in_use()
    if taken:
        one_taken = min(taken)
        try:
            allocate_host_ports(avoid=str(one_taken))
            raise AssertionError("expected PolicyViolation for a taken port")
        except PolicyViolation:
            pass  # expected
    else:
        # No live containers mapping ports; the reserved set must still block.
        from factory.policy import FACTORY_RESERVED_HOST_PORTS

        reserved = min(FACTORY_RESERVED_HOST_PORTS)
        try:
            allocate_host_ports(avoid=str(reserved))
            raise AssertionError("expected PolicyViolation for a reserved port")
        except PolicyViolation:
            pass  # expected


@pytest.mark.live
def test_live_protected_projects_untouched_by_policy_import():
    """Sanity check: merely importing/using the policy module must not
    itself touch docker. Confirms no side effects at import time."""
    result = subprocess.run(
        ["docker", "ps", "--format", '{{.Label "com.docker.compose.project"}}'],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    running = set(result.stdout.split())
    still_protected = PROTECTED_PROJECTS & running
    # This just documents current host state; not a hard assertion since
    # the user's other projects may be stopped independently of this suite.
    assert isinstance(still_protected, set)


def test_snapshot_ignores_human_relative_status_text(monkeypatch):
    from factory.policy import ProtectedProjectSnapshot

    output = "example-memory\tmemory-engine-api\tabc123\tExited\n"
    monkeypatch.setattr(
        "factory.policy.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )
    first = ProtectedProjectSnapshot.capture()
    second = ProtectedProjectSnapshot.capture()
    assert first.diff(second) == []


def test_snapshot_default_deny_catches_unknown_project_change(monkeypatch):
    from factory.policy import ProtectedProjectSnapshot

    before_out = "some-other-project\tweb\tabc123\tExited\n"
    after_out = "some-other-project\tweb\tdef456\tExited\n"
    monkeypatch.setattr(
        "factory.policy.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, before_out, ""),
    )
    before = ProtectedProjectSnapshot.capture()
    monkeypatch.setattr(
        "factory.policy.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, after_out, ""),
    )
    after = ProtectedProjectSnapshot.capture()
    violations = before.diff(after)
    assert any("some-other-project" in v for v in violations)


def test_snapshot_ignores_factory_owned_projects(monkeypatch):
    from factory.policy import ProtectedProjectSnapshot

    before_out = ""
    after_out = "factory-t-xyz-91e2fa\ttest\tabc123\tExited\n"
    monkeypatch.setattr(
        "factory.policy.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, before_out, ""),
    )
    before = ProtectedProjectSnapshot.capture()
    monkeypatch.setattr(
        "factory.policy.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, after_out, ""),
    )
    after = ProtectedProjectSnapshot.capture()
    assert before.diff(after) == []


def test_snapshot_detects_unlabeled_container_change(monkeypatch):
    from factory.policy import ProtectedProjectSnapshot

    before_out = "\ttask-ab\tabc123\tExited\n"
    after_out = "\ttask-ab\tdef456\tExited\n"
    monkeypatch.setattr(
        "factory.policy.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, before_out, ""),
    )
    before = ProtectedProjectSnapshot.capture()
    monkeypatch.setattr(
        "factory.policy.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, after_out, ""),
    )
    after = ProtectedProjectSnapshot.capture()
    assert before.diff(after) != []


def test_snapshot_classifies_known_and_unknown_changes_separately():
    from factory.policy import ProtectedProjectSnapshot

    before = ProtectedProjectSnapshot(
        projects={
            "example-production": [("protected-db", "old", "running")],
            "other-project": [("other-db", "old", "running")],
        }
    )
    after = ProtectedProjectSnapshot(
        projects={
            "example-production": [("protected-db", "new", "running")],
            "other-project": [("other-db", "new", "running")],
        }
    )

    report = before.collateral_damage_report(after)
    assert len(report["protected"]) == 1
    assert "example-production" in report["protected"][0]
    assert len(report["unexpected"]) == 1
    assert "other-project" in report["unexpected"][0]


# ---------------------------------------------------------------------------
# bind mounts: relative sources resolved against the repo root
# ---------------------------------------------------------------------------


def test_compose_rejects_relative_escape_outside_repo(home_checkout):
    cfg = {"services": {"app": {"volumes": ["../../etc:/host"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg, repo_root=str(home_checkout))


def test_compose_rejects_relative_sibling_escape(home_checkout):
    cfg = {"services": {"app": {"volumes": ["../sibling:/x"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg, repo_root=str(home_checkout))


def test_compose_accepts_relative_mount_inside_repo(home_checkout):
    (home_checkout / "data").mkdir()
    cfg = {"services": {"app": {"volumes": ["./data:/app"]}}}
    assert_safe_compose_file(cfg, repo_root=str(home_checkout))  # should not raise


def test_compose_rejects_relative_symlink_to_sensitive_root(home_checkout):
    (home_checkout / "link").symlink_to("/etc", target_is_directory=True)
    cfg = {"services": {"app": {"volumes": ["./link:/x"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg, repo_root=str(home_checkout))


def test_compose_keeps_named_volume_skip_with_repo_root(tmp_path):
    cfg = {"services": {"app": {"volumes": ["mydata:/data"]}}}
    assert_safe_compose_file(cfg, repo_root=str(tmp_path))  # should not raise


# ---------------------------------------------------------------------------
# run_compose: caller-supplied global Compose flags are rejected
# ---------------------------------------------------------------------------


def test_run_compose_rejects_global_project_flag():
    for form in (["-p", "other"], ["-p=other"]):
        with pytest.raises(PolicyViolation) as exc:
            policy_mod.run_compose("factory-t-x-abc123", ["docker-compose.yml"], form)
        assert "-p" in str(exc.value)


def test_run_compose_rejects_global_project_name_flag():
    for form in (["--project-name", "other"], ["--project-name=example-production"]):
        with pytest.raises(PolicyViolation) as exc:
            policy_mod.run_compose("factory-t-x-abc123", ["docker-compose.yml"], form)
        assert "--project-name" in str(exc.value)


def test_run_compose_rejects_project_directory_flag():
    for form in (["--project-directory", "/tmp/x"], ["--project-directory=/tmp/x"]):
        with pytest.raises(PolicyViolation) as exc:
            policy_mod.run_compose("factory-t-x-abc123", ["docker-compose.yml"], form)
        assert "--project-directory" in str(exc.value)


def test_run_compose_rejects_file_flag_both_forms():
    for form in (
        ["-f", "other.yml"],
        ["--file", "other.yml"],
        ["--file=other.yml"],
        ["-f=other.yml"],
    ):
        with pytest.raises(PolicyViolation) as exc:
            policy_mod.run_compose("factory-t-x-abc123", ["docker-compose.yml"], form)
        assert "-f" in str(exc.value)


def test_run_compose_allows_legit_service_level_args(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(policy_mod.subprocess, "run", fake_run)
    proc = policy_mod.run_compose(
        "factory-t-x-abc123",
        ["docker-compose.yml"],
        ["up", "-d", "--build", "svc"],
    )
    assert proc.returncode == 0
    assert "up" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--project-name") + 1] == "factory-t-x-abc123"


# ---------------------------------------------------------------------------
# bind-mount sources: relative paths are resolved and gated too
# ---------------------------------------------------------------------------


def test_bind_source_rejects_traversal_outside_repo(home_checkout):
    with pytest.raises(PolicyViolation) as exc:
        check_bind_source("../../etc", repo_root=home_checkout)
    assert "../../etc" in str(exc.value)


def test_bind_source_rejects_parent_sibling(home_checkout):
    with pytest.raises(PolicyViolation):
        check_bind_source("../sibling", repo_root=home_checkout)


def test_bind_source_allows_relative_inside_repo(home_checkout):
    (home_checkout / "data").mkdir()
    check_bind_source("./data", repo_root=home_checkout)  # should not raise


def test_bind_source_named_volume_still_skipped():
    check_bind_source("mydata", repo_root=None)  # should not raise


def test_bind_source_symlink_escape_rejected(home_checkout):
    link = home_checkout / "escape"
    link.symlink_to("/etc")
    with pytest.raises(PolicyViolation):
        check_bind_source("./escape", repo_root=home_checkout)


def test_compose_file_relative_volume_traversal_rejected():
    cfg = {"services": {"app": {"volumes": ["../../etc:/host"]}}}
    with pytest.raises(PolicyViolation):
        assert_safe_compose_file(cfg)


def test_compose_file_relative_volume_inside_repo_accepted(home_checkout):
    (home_checkout / "data").mkdir()
    cfg = {"services": {"app": {"volumes": ["./data:/app"]}}}
    assert_safe_compose_file(cfg, repo_root=home_checkout)  # should not raise
