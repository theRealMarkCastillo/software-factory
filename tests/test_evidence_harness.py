"""The offline test boundary itself: Docker and network are unreachable.

These are meta-tests for `tests/conftest.py`. They exist because every other
test's claim of being offline depends on this guard being installed at BOTH
subprocess entry points (`run` and `Popen`), including on teardown/finally
paths that ordinary monkeypatching does not cover.
"""

import subprocess
import sys

import pytest
from conftest import blocked_external_command


@pytest.mark.parametrize(
    "command",
    [
        ["docker", "compose", "up", "--build"],
        ["/usr/local/bin/docker", "ps"],
        ["podman", "ps"],
        ["git", "push", "origin", "main"],
        ["git", "-c", "core.hooksPath=/dev/null", "push", "origin", "main"],
        ["git", "--no-optional-locks", "fetch", "origin"],
        ["git", "ls-remote", "https://example.invalid/repo.git"],
        ["git", "clone", "https://example.invalid/repo.git"],
        ["curl", "https://example.invalid"],
    ],
)
def test_boundary_blocks_container_and_network_commands(command):
    assert blocked_external_command(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        ["git", "status", "--short"],
        ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD"],
        ["git", "--no-optional-locks", "diff", "--unified=0", "main"],
        [sys.executable, "-V"],
    ],
)
def test_boundary_allows_local_read_only_commands(command):
    assert blocked_external_command(command) is None


def test_boundary_blocks_subprocess_run():
    with pytest.raises(RuntimeError, match="offline test boundary"):
        subprocess.run(["docker", "compose", "version"], check=False)


def test_boundary_blocks_popen_and_its_helpers():
    # Popen directly, and check_output/call, which funnel through it.
    with pytest.raises(RuntimeError, match="offline test boundary"):
        subprocess.Popen(["docker", "ps"])
    with pytest.raises(RuntimeError, match="offline test boundary"):
        subprocess.check_output(["git", "ls-remote", "origin"])
    with pytest.raises(RuntimeError, match="offline test boundary"):
        subprocess.call(["git", "push"])


def test_boundary_allows_an_ordinary_local_command():
    proc = subprocess.run(
        [sys.executable, "-c", "print('ok')"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"
