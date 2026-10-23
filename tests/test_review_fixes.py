"""Tests for review fixes: nested sensitive mounts, port-lock ownership,
loopback port detection, manifest discovery."""

from pathlib import Path

import pytest

from factory import audit
from factory.audit import _discover_manifests, _run_npm_audit
from factory.policy import (
    PolicyViolation,
    assert_safe_compose_file,
    get_host_ports_in_use,
)
from factory.runner import _load_compose_files


class TestNestedSensitiveMounts:
    @pytest.mark.parametrize(
        "source",
        [
            "/etc/passwd",
            "/var/run",
            "/home/alice/.ssh",
            "/Users/example/secrets",
            "/private/etc/hosts",
            "/proc/self",
        ],
    )
    def test_paths_under_sensitive_roots_rejected(self, source):
        with pytest.raises(PolicyViolation, match="sensitive host path"):
            assert_safe_compose_file(
                {"services": {"s": {"image": "alpine", "volumes": [f"{source}:/x:ro"]}}}
            )

    def test_repo_relative_mount_still_allowed(self):
        # A plain named volume must not be caught by the root check.
        assert_safe_compose_file(
            {"services": {"s": {"image": "alpine", "volumes": ["mydata:/data"]}}}
        )

    def test_interpolated_host_mode_is_rejected(self):
        with pytest.raises(PolicyViolation, match="interpolation"):
            assert_safe_compose_file(
                {"services": {"s": {"network_mode": "${MODE:-host}"}}}
            )

    def test_named_volume_escape_is_rejected(self):
        with pytest.raises(PolicyViolation, match="explicit name"):
            assert_safe_compose_file(
                {
                    "services": {"s": {"volumes": ["data:/data"]}},
                    "volumes": {"data": {"name": "other-project_data"}},
                }
            )

    def test_volume_driver_bind_escape_is_rejected(self):
        with pytest.raises(PolicyViolation, match="bind-style"):
            assert_safe_compose_file(
                {
                    "services": {"s": {"volumes": ["data:/data"]}},
                    "volumes": {
                        "data": {
                            "driver": "local",
                            "driver_opts": {
                                "type": "none",
                                "o": "bind",
                                "device": "/etc",
                            },
                        }
                    },
                }
            )

    @pytest.mark.parametrize("field", ["build", "env_file"])
    def test_service_file_inputs_cannot_escape_repo(self, field):
        value = "../outside" if field == "build" else ["../outside.env"]
        with pytest.raises(PolicyViolation, match="outside the repo root"):
            assert_safe_compose_file(
                {
                    "services": {"s": {field: value}},
                },
                repo_root=Path("/tmp/subject-repo"),
            )

    def test_secret_file_cannot_escape_repo(self):
        with pytest.raises(PolicyViolation, match="outside the repo root"):
            assert_safe_compose_file(
                {
                    "services": {"s": {"image": "alpine"}},
                    "secrets": {"token": {"file": "../token"}},
                },
                repo_root=Path("/tmp/subject-repo"),
            )

    def test_compose_include_is_rejected(self):
        with pytest.raises(PolicyViolation, match="include"):
            assert_safe_compose_file(
                {
                    "include": ["../dangerous.yml"],
                    "services": {"s": {"image": "alpine"}},
                }
            )

    def test_service_extends_is_rejected(self):
        with pytest.raises(PolicyViolation, match="extends"):
            assert_safe_compose_file(
                {
                    "services": {
                        "s": {"extends": {"file": "../dangerous.yml", "service": "s"}}
                    },
                }
            )

    def test_build_dockerfile_escape_is_rejected(self):
        with pytest.raises(PolicyViolation, match="Dockerfile"):
            assert_safe_compose_file(
                {
                    "services": {
                        "s": {"build": {"context": ".", "dockerfile": "../Dockerfile"}}
                    },
                },
                repo_root=Path("/tmp/subject-repo"),
            )

    def test_custom_volume_driver_is_rejected(self):
        with pytest.raises(PolicyViolation, match="custom volume"):
            assert_safe_compose_file(
                {
                    "services": {"s": {"volumes": ["data:/data"]}},
                    "volumes": {"data": {"driver": "nfs"}},
                }
            )

    def test_external_secret_is_rejected(self):
        with pytest.raises(PolicyViolation, match="external resources"):
            assert_safe_compose_file(
                {
                    "services": {"s": {"image": "alpine"}},
                    "secrets": {"token": {"external": True}},
                }
            )


def test_compose_override_does_not_hide_privileged_service(tmp_path: Path):
    base = tmp_path / "base.yml"
    override = tmp_path / "override.yml"
    base.write_text("services:\n  test:\n    image: alpine\n    privileged: true\n")
    override.write_text("services:\n  test:\n    command: [true]\n")
    with pytest.raises(PolicyViolation, match="privileged"):
        _load_compose_files([base, override])


def test_port_regex_matches_loopback(monkeypatch):
    from factory import policy

    class P:
        returncode = 0
        stdout = "127.0.0.1:5433->5432/tcp\n"

    monkeypatch.setattr(policy.subprocess, "run", lambda *a, **k: P())
    assert get_host_ports_in_use() == {5433}


class TestManifestDiscovery:
    def test_prefers_lock_over_declaration(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / "poetry.lock").write_text("[]\n")
        manifests, lang = _discover_manifests(tmp_path)
        assert [m.name for m in manifests] == ["poetry.lock"]
        assert lang == "python"

    def test_polyglot_returns_both_languages(self, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / "package-lock.json").write_text("{}")
        manifests, lang = _discover_manifests(tmp_path)
        assert {m.name for m in manifests} == {"requirements.txt", "package-lock.json"}
        assert lang == "python"

    def test_requirements_plus_pyproject_audits_both(self, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        manifests, _ = _discover_manifests(tmp_path)
        assert {m.name for m in manifests} == {"requirements.txt", "pyproject.toml"}

    def test_no_manifests(self, tmp_path: Path):
        assert _discover_manifests(tmp_path) == ([], "")


def test_polyglot_audit_runs_npm_even_with_python_venv(tmp_path: Path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "package-lock.json").write_text("{}")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "pip").write_text("")
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    monkeypatch.setattr(audit, "_pip_audit_binary", lambda: "/bin/true")
    monkeypatch.setattr(
        audit, "_run_npm_audit", lambda repo: audit.AuditResult("fail", "typescript")
    )
    result = audit.run_dependency_audit(str(tmp_path), venv_path=str(venv))
    assert result.status == "fail"
    assert "typescript" in result.language


def test_npm_audit_error_json_is_unavailable(tmp_path: Path, monkeypatch):
    class Proc:
        returncode = 1
        stdout = '{"error":{"code":"ENOLOCK"}}'
        stderr = ""

    monkeypatch.setattr(audit.subprocess, "run", lambda *args, **kwargs: Proc())
    result = _run_npm_audit(tmp_path)
    assert result.status == "unavailable"
