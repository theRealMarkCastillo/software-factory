"""Owner-context probe: it must classify a Hermes worker vs a fenced child honestly."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "owner_context_probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("owner_context_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def test_classify_requires_all_three_grant_variables():
    complete = {
        "HERMES_KANBAN_TASK": "t_x",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_CLAIM_LOCK": "host:1",
    }
    assert probe.classify(complete) == "grant_in_env"
    for missing in complete:
        partial = {k: v for k, v in complete.items() if k != missing}
        assert probe.classify(partial) == "no_grant"
    blank = dict(complete, HERMES_KANBAN_CLAIM_LOCK="  ")
    assert probe.classify(blank) == "no_grant"


def test_classify_fence_marker_is_a_descendant_context():
    assert probe.classify({"HERMES_DELEGATED_CHILD_CONTEXT": "/home/me/.hermes"}) == (
        "fenced_descendant"
    )
    # A child carries the location pins but never the grant.
    child = {
        "HERMES_DELEGATED_CHILD_CONTEXT": "/home/me/.hermes",
        "HERMES_KANBAN_BOARD": "software-factory",
        "HERMES_KANBAN_DB": "/tmp/kanban.db",
    }
    assert probe.classify(child) == "fenced_descendant"


def test_describe_never_prints_run_id_or_lock_values():
    env = {
        "HERMES_KANBAN_TASK": "t_x",
        "HERMES_KANBAN_RUN_ID": "424242",
        "HERMES_KANBAN_CLAIM_LOCK": "secret-host:999",
        "HERMES_KANBAN_BOARD": "software-factory",
    }
    report = probe.describe(env)
    rendered = json.dumps(report)
    assert "424242" not in rendered
    assert "secret-host:999" not in rendered
    assert report["grant_present"] == {
        "HERMES_KANBAN_TASK": True,
        "HERMES_KANBAN_RUN_ID": True,
        "HERMES_KANBAN_CLAIM_LOCK": True,
    }
    assert report["location"]["HERMES_KANBAN_TASK"] == "t_x"


def _run_cli(extra_env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HERMES_KANBAN_")
        and key != "HERMES_DELEGATED_CHILD_CONTEXT"
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_cli_exits_2_without_a_grant_and_0_with_one():
    absent = _run_cli(
        {"HERMES_DELEGATED_CHILD_CONTEXT": "/home/me/.hermes"}, "--require-grant"
    )
    assert absent.returncode == 2, absent.stderr
    assert json.loads(absent.stdout)["verdict"] == "fenced_descendant"

    present = _run_cli(
        {
            "HERMES_KANBAN_TASK": "t_x",
            "HERMES_KANBAN_RUN_ID": "1",
            "HERMES_KANBAN_CLAIM_LOCK": "host:1",
        },
        "--require-grant",
    )
    assert present.returncode == 0, present.stderr
    assert json.loads(present.stdout)["verdict"] == "grant_in_env"
