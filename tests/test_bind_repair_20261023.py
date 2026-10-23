"""Fail-first regressions for the narrow relative-bind repair (2026-10-23 QC).

Frozen failing subject: engine ``0c0fdb9``. At that SHA the relative-source
branch of ``check_bind_source`` returned before every ``SENSITIVE_HOST_ROOTS``
check, so repo-root mounts, arbitrary home roots, system-root relative mounts,
and ``.env.*`` credential files were all authorized.

The allowed case is deliberately narrow: a checkout nested below a user home
directory (``/Users/<user>/...`` or ``/home/<user>/...``) may mount an existing
ordinary subdirectory whose tree contains no credential/VCS path and no symlink
leaving the repository. Everything else fails closed.
"""

import os

import pytest

from factory import policy, runner
from factory.policy import PolicyViolation


def _write(path, text="placeholder = 'not-a-secret'\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestRepoRootMountsRejected:
    @pytest.mark.parametrize("source", [".", "./", "./.", "docs/.."])
    def test_every_repo_root_spelling_is_rejected(self, home_checkout, source):
        (home_checkout / "docs").mkdir()
        with pytest.raises(PolicyViolation, match="repository root"):
            policy.check_bind_source(source, repo_root=home_checkout)

    def test_compose_root_mount_rejected_even_with_env_and_git_present(
        self, home_checkout
    ):
        _write(home_checkout / ".env")
        (home_checkout / ".git").mkdir()
        with pytest.raises(PolicyViolation, match="repository root"):
            policy.assert_safe_compose_file(
                {"services": {"app": {"volumes": [".:/app"]}}},
                repo_root=home_checkout,
            )

    def test_root_mount_rejected_for_synthetic_home_checkout(self):
        repo = policy.Path("/Users/worker/subject-repo")
        with pytest.raises(PolicyViolation, match="repository root"):
            policy.check_bind_source(".", repo_root=repo)


class TestUnsafeRepoRootsRejected:
    @pytest.mark.parametrize(
        "root",
        [
            "/",
            "/Users",
            "/home",
            "/etc",
            "/private/etc",
            "/var",
            "/run",
            "/dev",
            "/root",
            "/proc",
            "/sys",
        ],
    )
    def test_sensitive_host_roots_are_not_checkouts(self, root):
        with pytest.raises(PolicyViolation):
            policy.check_bind_source("./anything", repo_root=policy.Path(root))

    def test_operator_home_is_not_a_checkout(self):
        with pytest.raises(PolicyViolation, match="home"):
            policy.check_bind_source("./docs", repo_root=policy.Path.home())

    def test_immediate_home_directory_is_not_a_checkout(self):
        immediate = policy.Path.home().parent / "somebody-else"
        with pytest.raises(PolicyViolation, match="home"):
            policy.check_bind_source("./docs", repo_root=immediate)

    def test_system_root_relative_mount_is_rejected(self):
        # QC probe: repo_root=/etc allowed './passwd' at the frozen SHA.
        with pytest.raises(PolicyViolation):
            policy.check_bind_source("./passwd", repo_root=policy.Path("/etc"))

    def test_checkout_under_system_root_cannot_mount_relatively(self):
        # A checkout under /var gets no relative-mount exception, whatever the
        # host temporary directory happens to be.
        with pytest.raises(PolicyViolation, match="home"):
            policy.check_bind_source(
                "./data", repo_root=policy.Path("/var/factory-subject")
            )


class TestMountedDirectoryContents:
    def test_nested_env_variant_rejected(self, home_checkout):
        _write(home_checkout / "source" / ".env.production")
        with pytest.raises(PolicyViolation, match="sensitive"):
            policy.check_bind_source("./source", repo_root=home_checkout)

    def test_nested_vcs_metadata_rejected(self, home_checkout):
        _write(home_checkout / "source" / "pkg" / ".git" / "config")
        with pytest.raises(PolicyViolation, match="sensitive"):
            policy.check_bind_source("./source", repo_root=home_checkout)

    def test_symlink_out_of_repo_rejected(self, home_checkout, tmp_path):
        source = home_checkout / "source"
        source.mkdir()
        (source / "link").symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(PolicyViolation, match="outside"):
            policy.check_bind_source("./source", repo_root=home_checkout)

    def test_symlink_into_sensitive_descendant_rejected(self, home_checkout):
        source = home_checkout / "source"
        source.mkdir()
        (home_checkout / ".git").mkdir()
        (source / "link").symlink_to(home_checkout / ".git", target_is_directory=True)
        with pytest.raises(PolicyViolation, match="sensitive"):
            policy.check_bind_source("./source", repo_root=home_checkout)

    def test_symlink_to_repo_root_rejected(self, home_checkout):
        source = home_checkout / "source"
        source.mkdir()
        (source / "link").symlink_to(home_checkout, target_is_directory=True)
        with pytest.raises(PolicyViolation, match="repository root"):
            policy.check_bind_source("./source", repo_root=home_checkout)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 dirs")
    def test_unreadable_descendant_fails_closed(self, home_checkout):
        locked = home_checkout / "source" / "locked"
        locked.mkdir(parents=True)
        (locked / "inside.txt").write_text("x\n")
        locked.chmod(0o000)
        try:
            with pytest.raises(PolicyViolation, match="fail"):
                policy.check_bind_source("./source", repo_root=home_checkout)
        finally:
            locked.chmod(0o755)

    def test_non_existing_source_is_rejected(self, home_checkout):
        with pytest.raises(PolicyViolation, match="does not exist"):
            policy.check_bind_source("./missing", repo_root=home_checkout)


class TestEnvVariantsRejected:
    @pytest.mark.parametrize(
        "source",
        [
            "./.env.production",
            "./.env.local",
            "./.env.staging",
            "nested/.env.production",
        ],
    )
    def test_env_family_prefix_is_rejected(self, home_checkout, source):
        _write(home_checkout / source)
        with pytest.raises(PolicyViolation, match="sensitive repository path"):
            policy.check_bind_source(source, repo_root=home_checkout)


class TestPreservedDenials:
    @pytest.mark.parametrize(
        "source",
        [
            "../outside",
            "../../etc",
            "./docs/../../outside",
            ".git/config",
            ".ssh/id_rsa",
            "secrets/token",
            "/etc/passwd",
            "/Users/example/private",
            "/home/example/.ssh/id_rsa",
        ],
    )
    def test_previous_denials_remain(self, home_checkout, source):
        (home_checkout / "docs").mkdir()
        with pytest.raises(PolicyViolation):
            policy.check_bind_source(source, repo_root=home_checkout)

    def test_traversal_message_survives_valid_root(self, home_checkout):
        with pytest.raises(PolicyViolation, match="outside the repo root"):
            policy.check_bind_source("../outside", repo_root=home_checkout)


class TestNarrowExceptionPreserved:
    def test_ordinary_source_and_docs_allowed(self, home_checkout):
        _write(home_checkout / "source" / "store.py", "x = 1\n")
        (home_checkout / "docs").mkdir()
        policy.check_bind_source("./source", repo_root=home_checkout)
        policy.check_bind_source("./docs", repo_root=home_checkout)
        policy.assert_safe_compose_file(
            {
                "services": {
                    "app": {
                        "volumes": ["./source:/app:ro", "./docs:/docs:ro"],
                    }
                }
            },
            repo_root=home_checkout,
        )

    def test_e2e_fixture_source_is_allowed(self):
        subject = policy.Path(
            "/Users/markcastillo/.hermes/kanban/factory-artifacts/"
            "review-20261023/e2e-subject"
        )
        if not (subject / "source").is_dir():
            pytest.skip(f"E2E fixture checkout not present at {subject}")
        policy.check_bind_source("./source", repo_root=subject)

    def test_named_volume_unchanged(self):
        policy.check_bind_source("mydata", repo_root=None)


class TestRunnerPreflight:
    def test_sensitive_repo_path_rejected_before_compose_validation(
        self, tmp_path, monkeypatch
    ):
        def boom(*args, **kwargs):
            pytest.fail("preflight guard must reject before Compose validation/Docker")

        monkeypatch.setattr(runner, "assert_safe_compose_file", boom)
        monkeypatch.setattr(runner, "run_compose", boom)
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  test:\n    image: alpine\n")
        with pytest.raises(PolicyViolation, match="sensitive host path"):
            runner.run_factory_card("card", "/etc", [str(compose)], run_gates=False)

    def test_home_directory_repo_rejected(self, monkeypatch):
        def boom(*args, **kwargs):
            pytest.fail("preflight guard must reject before any Compose/Docker work")

        monkeypatch.setattr(runner, "run_compose", boom)
        with pytest.raises(PolicyViolation, match="home"):
            runner.run_factory_card(
                "card", str(policy.Path.home()), ["docker-compose.yml"], run_gates=False
            )

    def test_load_compose_files_rejects_system_root_relative_mount(self, tmp_path):
        compose = tmp_path / "compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    image: alpine\n"
            "    volumes:\n"
            "      - ./passwd:/host/passwd:ro\n"
        )
        with pytest.raises(PolicyViolation):
            runner._load_compose_files([compose], repo_root=policy.Path("/etc"))
