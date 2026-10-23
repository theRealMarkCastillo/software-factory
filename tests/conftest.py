"""Keep host-dependent checks explicit for contributors and CI."""

import pytest


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
