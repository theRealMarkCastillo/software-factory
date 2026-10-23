"""Credential directories cannot become trusted by declaring them a repo."""

from pathlib import Path

import pytest

from factory import policy


@pytest.mark.parametrize(
    "component", [".ssh", ".aws", ".git", "secrets", ".env.production"]
)
@pytest.mark.parametrize("nested", [False, True])
def test_credential_ancestor_is_not_a_subject_repository(tmp_path, component, nested):
    root = tmp_path / component
    if nested:
        root = root / "pretend-checkout"
    root.mkdir(parents=True)
    with pytest.raises(policy.PolicyViolation, match="sensitive"):
        policy.assert_safe_repo_root(root)


def test_symlink_cannot_hide_credential_repository(tmp_path):
    secret = tmp_path / ".ssh" / "pretend-checkout"
    secret.mkdir(parents=True)
    alias = tmp_path / "ordinary-checkout"
    alias.symlink_to(secret, target_is_directory=True)
    with pytest.raises(policy.PolicyViolation, match="sensitive"):
        policy.assert_safe_repo_root(alias)


def test_normal_nested_checkout_remains_supported(tmp_path):
    policy.assert_safe_repo_root(tmp_path / "ordinary-checkout")
    policy.assert_safe_repo_root(
        Path.home() / ".hermes/kanban/factory-artifacts/example"
    )
