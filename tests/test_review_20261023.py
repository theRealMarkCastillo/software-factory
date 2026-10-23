"""Regressions for the public engine review of 2026-10-23.

Each class pins one reviewed defect at the public policy/audit boundary:
global Compose flag smuggling, auditor resolution outside the engine
environment, bind policy for repositories checked out under a home root,
and host-port allowlist validation ordering.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory import audit, policy
from factory.policy import PolicyViolation

REPO_ROOT = Path(__file__).resolve().parents[1]


def _forbid_subprocess_run(monkeypatch, module):
    def boom(*args, **kwargs):
        pytest.fail("must reject the request before spawning any subprocess")

    monkeypatch.setattr(module.subprocess, "run", boom)


# ---------------------------------------------------------------------------
# (1) run_compose: compact global flags are injections too
# ---------------------------------------------------------------------------


class TestRunComposeGlobalFlagGuard:
    @pytest.mark.parametrize(
        "args",
        [
            ["-pother"],
            ["-pother", "up"],
            ["-p=other"],
            ["--project-name=other"],
            ["--project-directory=/tmp/elsewhere"],
            ["-fsneaky.yml"],
            ["-fsneaky.yml", "up"],
            ["-f=sneaky.yml"],
            ["--file=sneaky.yml"],
            ["up", "svc", "-pother"],
        ],
    )
    def test_compact_and_long_forms_rejected_before_subprocess(self, monkeypatch, args):
        _forbid_subprocess_run(monkeypatch, policy)
        with pytest.raises(PolicyViolation):
            policy.run_compose("factory-t-x-abc123", ["compose.yml"], args)

    @pytest.mark.parametrize(
        "args",
        [
            ["up", "--build", "--abort-on-container-exit", "--exit-code-from", "test"],
            ["up", "-d"],
            ["logs", "--no-color"],
            ["down", "--volumes", "--remove-orphans", "--timeout", "30"],
            ["ps", "--all"],
            ["run", "--rm", "test", "pytest", "-q"],
        ],
    )
    def test_ordinary_lifecycle_args_still_allowed(self, monkeypatch, args):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(policy.subprocess, "run", fake_run)
        proc = policy.run_compose("factory-t-x-abc123", ["compose.yml"], args)
        assert proc.returncode == 0
        assert seen["cmd"][-len(args) :] == args
        assert "--project-name" in seen["cmd"]


# ---------------------------------------------------------------------------
# (2) pip-audit resolution: engine interpreter environment wins
# ---------------------------------------------------------------------------


class TestPipAuditResolution:
    def _fake_engine_environment(self, tmp_path, monkeypatch):
        engine_bin = tmp_path / "engine-venv" / "bin"
        engine_bin.mkdir(parents=True)
        interpreter = engine_bin / "python"
        interpreter.write_text("")
        auditor = engine_bin / "pip-audit"
        auditor.write_text("#!/bin/sh\n")
        monkeypatch.setattr(audit.sys, "executable", str(interpreter))
        monkeypatch.setattr(
            audit.sysconfig, "get_path", lambda _name: str(tmp_path / "no-scripts")
        )
        monkeypatch.delenv("FACTORY_PIP_AUDIT", raising=False)
        return auditor

    def test_engine_interpreter_precedes_ambient_path_and_hermes_fallback(
        self, tmp_path, monkeypatch
    ):
        auditor = self._fake_engine_environment(tmp_path, monkeypatch)
        monkeypatch.setattr(
            audit.shutil, "which", lambda _name: "/ambient/bin/pip-audit"
        )
        monkeypatch.setattr(audit.Path, "home", lambda: tmp_path / "historical-home")
        assert audit._pip_audit_binary() == str(auditor)

    def test_sysconfig_scripts_location_is_used_when_executable_is_elsewhere(
        self, tmp_path, monkeypatch
    ):
        scripts = tmp_path / "engine-scripts"
        scripts.mkdir()
        auditor = scripts / "pip-audit"
        auditor.write_text("#!/bin/sh\n")
        monkeypatch.setattr(audit.sys, "executable", str(tmp_path / "bin" / "python"))
        monkeypatch.setattr(audit.sysconfig, "get_path", lambda _name: str(scripts))
        monkeypatch.setattr(audit.shutil, "which", lambda _name: None)
        monkeypatch.setattr(audit.Path, "home", lambda: tmp_path / "empty-home")
        monkeypatch.delenv("FACTORY_PIP_AUDIT", raising=False)
        assert audit._pip_audit_binary() == str(auditor)

    def test_explicit_override_still_wins(self, tmp_path, monkeypatch):
        self._fake_engine_environment(tmp_path, monkeypatch)
        monkeypatch.setenv("FACTORY_PIP_AUDIT", "/explicit/pip-audit")
        assert audit._pip_audit_binary() == "/explicit/pip-audit"

    def test_historical_hermes_fallback_remains_last_resort(
        self, tmp_path, monkeypatch
    ):
        empty = tmp_path / "isolated"
        empty.mkdir()
        monkeypatch.setattr(audit.sys, "executable", str(empty / "bin" / "python"))
        monkeypatch.setattr(
            audit.sysconfig, "get_path", lambda _name: str(empty / "bin")
        )
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.delenv("FACTORY_PIP_AUDIT", raising=False)
        home = tmp_path / "home"
        historical = home / ".hermes" / "hermes-agent" / "venv" / "bin" / "pip-audit"
        historical.parent.mkdir(parents=True)
        historical.write_text("#!/bin/sh\n")
        monkeypatch.setattr(audit.Path, "home", lambda: home)
        assert audit._pip_audit_binary() == str(historical)

    def test_isolated_path_and_home_without_auditor_is_fail_closed(
        self, tmp_path, monkeypatch
    ):
        empty = tmp_path / "isolated"
        empty.mkdir()
        monkeypatch.setattr(audit.sys, "executable", str(empty / "bin" / "python"))
        monkeypatch.setattr(
            audit.sysconfig, "get_path", lambda _name: str(empty / "bin")
        )
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.setenv("HOME", str(empty / "home"))
        monkeypatch.delenv("FACTORY_PIP_AUDIT", raising=False)
        assert audit._pip_audit_binary() is None

    def test_engine_environment_resolves_under_isolated_path_and_home(self, tmp_path):
        """Prove the real engine interpreter finds its own auditor.

        PATH and HOME point at empty directories, so neither ambient PATH nor a
        historical Hermes installation can satisfy the lookup.
        """
        empty = tmp_path / "isolated"
        empty.mkdir()
        env = {"PATH": str(empty), "HOME": str(empty), "PYTHONPATH": str(REPO_ROOT)}
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from factory.audit import _pip_audit_binary;"
                "print(_pip_audit_binary())",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        expected = Path(sys.executable).parent / "pip-audit"
        assert expected.is_file(), f"engine environment lacks {expected}"
        assert proc.stdout.strip() == str(expected)


# ---------------------------------------------------------------------------
# (3) repo-local binds under /Users and /home
# ---------------------------------------------------------------------------


class TestRepoLocalBindPolicy:
    def test_relative_mount_inside_repo_is_allowed(self):
        """A repository-relative mount stays legal whatever the checkout root."""
        policy.check_bind_source("./docs", repo_root=REPO_ROOT)

    def test_repo_root_itself_can_be_mounted(self):
        policy.check_bind_source(".", repo_root=REPO_ROOT)

    def test_compose_relative_mount_inside_repo_is_allowed(self):
        policy.assert_safe_compose_file(
            {"services": {"app": {"volumes": ["./docs:/docs:ro"]}}},
            repo_root=REPO_ROOT,
        )

    @pytest.mark.parametrize("home_root", [Path("/Users"), Path("/home")])
    def test_home_rooted_repo_may_mount_its_own_relative_paths(self, home_root):
        """A checkout under /Users or /home is not itself a forbidden mount."""
        repo = home_root / "worker" / "subject-repo"
        policy.check_bind_source("./data", repo_root=repo)
        policy.check_bind_source(".", repo_root=repo)
        policy.assert_safe_compose_file(
            {"services": {"app": {"volumes": ["./data:/data:ro"]}}}, repo_root=repo
        )

    @pytest.mark.parametrize("home_root", [Path("/Users"), Path("/home")])
    def test_home_rooted_repo_absolute_spelling_is_still_rejected(self, home_root):
        repo = home_root / "worker" / "subject-repo"
        with pytest.raises(PolicyViolation, match="sensitive host path"):
            policy.check_bind_source(str(repo / "data"), repo_root=repo)

    @pytest.mark.parametrize(
        "source", ["/Users/example/private", "/home/example/.ssh/id_rsa"]
    )
    def test_absolute_home_roots_are_never_mountable(self, source):
        with pytest.raises(PolicyViolation, match="sensitive host path"):
            policy.check_bind_source(source, repo_root=REPO_ROOT)

    @pytest.mark.parametrize(
        "source",
        [
            "../../etc",
            "../sibling",
            "./docs/../../outside",
            "./missing/../../../etc",
        ],
    )
    def test_traversal_out_of_home_rooted_repo_is_rejected(self, source):
        with pytest.raises(PolicyViolation, match="outside the repo root"):
            policy.check_bind_source(source, repo_root=REPO_ROOT)

    def test_relative_symlink_to_sensitive_root_is_rejected(self, tmp_path):
        (tmp_path / "link").symlink_to("/etc", target_is_directory=True)
        with pytest.raises(PolicyViolation, match="outside the repo root"):
            policy.check_bind_source("./link", repo_root=tmp_path)

    def test_relative_symlink_out_of_repo_is_rejected(self, tmp_path):
        outside = tmp_path / "sibling"
        outside.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PolicyViolation, match="outside the repo root"):
            policy.check_bind_source("./escape", repo_root=repo)

    @pytest.mark.parametrize(
        "source",
        [
            ".git/config",
            "./.git",
            ".ssh/id_rsa",
            ".aws/credentials",
            ".gnupg/secring.gpg",
            ".docker/config.json",
            ".kube/config",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".env",
            "secrets/token",
            "nested/.aws/credentials",
        ],
    )
    def test_sensitive_repo_subpaths_are_never_mountable(self, source):
        with pytest.raises(PolicyViolation, match="sensitive repository path"):
            policy.check_bind_source(source, repo_root=REPO_ROOT)

    @pytest.mark.parametrize("source", ["./data", "./config", "./docs", "./src/app"])
    def test_ordinary_repo_subdirectories_remain_mountable(self, source):
        policy.check_bind_source(source, repo_root=REPO_ROOT)

    def test_named_volume_still_skipped(self):
        policy.check_bind_source("mydata", repo_root=REPO_ROOT)

    def test_docker_socket_still_rejected(self):
        with pytest.raises(PolicyViolation, match="Docker socket"):
            policy.assert_safe_compose_file(
                {
                    "services": {
                        "app": {
                            "volumes": ["/var/run/docker.sock:/var/run/docker.sock"]
                        }
                    }
                },
                repo_root=REPO_ROOT,
            )


# ---------------------------------------------------------------------------
# (4) allocate_host_ports: validate configuration before host inspection
# ---------------------------------------------------------------------------


class TestAllocateHostPortsValidation:
    def _forbid_host_inspection(self, monkeypatch):
        def boom(*args, **kwargs):
            pytest.fail("host inspection must not run before request validation")

        monkeypatch.setattr(policy, "get_host_ports_in_use", boom)
        monkeypatch.setattr(policy.subprocess, "run", boom)

    @pytest.mark.parametrize(
        "avoid",
        ["", " ", "none", "not-a-port", "5000,abc", "0", "-1", "5000.5", "65536"],
    )
    def test_invalid_allowlist_rejected_without_host_scan(self, monkeypatch, avoid):
        monkeypatch.delenv("FACTORY_HOST_CONFIG", raising=False)
        self._forbid_host_inspection(monkeypatch)
        with pytest.raises(PolicyViolation, match="port allowlist"):
            policy.allocate_host_ports(avoid=avoid)

    def test_invalid_host_config_rejected_before_host_scan(self, tmp_path, monkeypatch):
        config = tmp_path / "host.json"
        config.write_text(json.dumps({"reserved_host_ports": [70000]}))
        monkeypatch.setenv("FACTORY_HOST_CONFIG", str(config))
        self._forbid_host_inspection(monkeypatch)
        with pytest.raises(PolicyViolation, match="host config"):
            policy.allocate_host_ports(avoid="5000")

    def test_allowlist_returns_requested_free_ports(self, monkeypatch):
        monkeypatch.delenv("FACTORY_HOST_CONFIG", raising=False)
        monkeypatch.setattr(policy, "get_host_ports_in_use", lambda: {5000})
        assert policy.allocate_host_ports(avoid="5001,5002") == [5001, 5002]

    def test_allowlist_rejects_ports_already_in_use(self, monkeypatch):
        monkeypatch.delenv("FACTORY_HOST_CONFIG", raising=False)
        monkeypatch.setattr(policy, "get_host_ports_in_use", lambda: {5001})
        with pytest.raises(PolicyViolation, match="already in use"):
            policy.allocate_host_ports(avoid="5001,5002")

    def test_allowlist_rejects_reserved_ports(self, monkeypatch):
        monkeypatch.delenv("FACTORY_HOST_CONFIG", raising=False)
        monkeypatch.setattr(policy, "get_host_ports_in_use", lambda: set())
        with pytest.raises(PolicyViolation, match="already in use"):
            policy.allocate_host_ports(
                avoid=str(min(policy.FACTORY_RESERVED_HOST_PORTS))
            )
