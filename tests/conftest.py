"""Keep host-dependent checks explicit for contributors and CI."""

import shutil
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
