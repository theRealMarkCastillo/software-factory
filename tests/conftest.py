"""Keep host-dependent checks explicit for contributors and CI."""

import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def home_checkout():
    """A real checkout nested below a user home directory.

    The bind policy only authorizes relative mounts for repositories nested
    below ``/Users/<user>`` or ``/home/<user>``, so pytest's system temp tree
    (``/private/var/...`` on macOS) cannot exercise the allowed branch. The
    fixture lives under the operator scratch area and is removed afterwards.
    """
    home = Path.home()
    if not any(
        home == Path(prefix) or home.is_relative_to(prefix)
        for prefix in ("/Users", "/home")
    ):
        pytest.skip(f"home directory {home} is not under /Users or /home")
    base = home / ".hermes" / "cache" / "scratch" / f"pytest-bind-{uuid.uuid4().hex}"
    try:
        repo = base / "subject-repo"
        repo.mkdir(parents=True)
    except OSError as exc:
        pytest.skip(f"no writable home-rooted checkout: {exc}")
    try:
        yield repo
    finally:
        shutil.rmtree(base, ignore_errors=True)


def pytest_addoption(parser):
    parser.addoption(
        "--run-live", action="store_true", help="Enable live Docker/host checks"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: requires live Docker and host inspection")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="live host check; opt in with --run-live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Offline test boundary
# ---------------------------------------------------------------------------
#
# Every test in this suite must run without Docker or network access. The
# boundary below wraps _both_ subprocess entry points (`run` and `Popen`, which
# `call`/`check_call`/`check_output` funnel through) so a future edit cannot
# silently reach a container daemon or a remote from a test -- including from
# teardown/finally paths, which bypass ordinary monkeypatching of the happy
# path. Commands the suite legitimately needs (git init/commit/status/diff,
# the interpreter, ruff) are unaffected.

_BLOCKED_PROGRAMS = frozenset(
    {
        # container runtimes
        "docker",
        "docker-compose",
        "podman",
        "nerdctl",
        # network clients
        "curl",
        "wget",
        "nc",
        "ncat",
        "ssh",
        "scp",
        "rsync",
    }
)
_BLOCKED_GIT_SUBCOMMANDS = frozenset(
    {"push", "fetch", "pull", "clone", "ls-remote", "archive"}
)
# git global options that consume the following argument.
_GIT_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--config-env",
        "--super-prefix",
        "--attr-source",
    }
)


def _argv(command) -> list[str]:
    """Best-effort argv for a subprocess command (str/list/bytes, shell or not)."""
    if isinstance(command, bytes):
        command = command.decode(errors="replace")
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    try:
        return [
            part.decode(errors="replace") if isinstance(part, bytes) else str(part)
            for part in command
        ]
    except TypeError:
        return []


def blocked_external_command(command) -> str | None:
    """Return why `command` is outside the offline test boundary, else None."""
    argv = _argv(command)
    if not argv:
        return None
    program = Path(argv[0]).name
    if program in _BLOCKED_PROGRAMS:
        return f"blocked program {program!r}"
    if program != "git":
        return None
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in _GIT_VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if token in _BLOCKED_GIT_SUBCOMMANDS:
            return f"blocked network git subcommand {token!r}"
        return None
    return None


@pytest.fixture(autouse=True)
def deny_external_commands(monkeypatch):
    """Fail fast on Docker or network use anywhere in a test's call graph."""
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def guard(command) -> None:
        reason = blocked_external_command(command)
        if reason:
            raise RuntimeError(
                f"offline test boundary: {reason} (command: {command!r}). "
                "Mock the call instead of reaching the host."
            )

    def guarded_run(*args, **kwargs):
        if args:
            guard(args[0])
        return real_run(*args, **kwargs)

    class GuardedPopen(real_popen):
        def __init__(self, *args, **kwargs):
            if args:
                guard(args[0])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
