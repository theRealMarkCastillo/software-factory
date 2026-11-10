#!/usr/bin/env python3
"""Report the Hermes Kanban owner context this process actually holds.

The LangGraph orchestration refuses to act unless the process holds the
dispatcher grant (``HERMES_KANBAN_TASK`` + ``HERMES_KANBAN_RUN_ID`` +
``HERMES_KANBAN_CLAIM_LOCK``). Hermes deliberately keeps that grant out of tool
subprocesses, so *where* a command runs decides whether it can be the graph's
owner at all. This probe makes that visible before any effect is attempted.

It prints no grant value: task ids are shown, run ids and lock tokens are only
reported as present/absent. It performs reads only (environment plus an optional
``hermes kanban show``).

Usage::

    python scripts/owner_context_probe.py [--board BOARD] [--card CARD]
    python scripts/owner_context_probe.py --engine-check
    python scripts/owner_context_probe.py --require-grant   # exit 2 when absent

Verdicts:

* ``grant_in_env``       - this process holds all three grant variables.
* ``fenced_descendant``   - the Hermes Kanban fence marker is present and the
  grant variables are absent: this is a tool child of a dispatcher-owned worker
  (or of a delegated child). The orchestration must not run here.
* ``no_grant``           - no grant and no fence marker: an ordinary session.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

GRANT_VARS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
)
FENCE_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"
LOCATION_VARS = (
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_PROFILE",
    "HERMES_HOME",
)


def classify(env: dict[str, str]) -> str:
    """Return the owner-context verdict for an environment mapping."""
    if all((env.get(name) or "").strip() for name in GRANT_VARS):
        return "grant_in_env"
    if (env.get(FENCE_MARKER) or "").strip():
        return "fenced_descendant"
    return "no_grant"


def describe(env: dict[str, str]) -> dict:
    """Build the JSON-safe report for *env* without exposing grant values."""
    grant = {name: bool((env.get(name) or "").strip()) for name in GRANT_VARS}
    location = {name: env.get(name, "") for name in LOCATION_VARS if env.get(name)}
    if grant["HERMES_KANBAN_TASK"] and env.get("HERMES_KANBAN_TASK"):
        location["HERMES_KANBAN_TASK"] = env["HERMES_KANBAN_TASK"]
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "verdict": classify(env),
        "grant_present": grant,
        "fence_marker": env.get(FENCE_MARKER, ""),
        "location": location,
    }


def kanban_show(board: str | None, card: str) -> dict:
    """Read the card through the supported CLI; report only field presence."""
    argv = ["hermes", "kanban"]
    if board:
        argv += ["--board", board]
    argv += ["show", card, "--json"]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return {
            "ok": False,
            "returncode": completed.returncode,
            "stderr": completed.stderr.strip()[:300],
        }
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return {
            "ok": False,
            "returncode": completed.returncode,
            "stderr": "show --json did not return JSON",
        }
    task = payload.get("task") or {}
    return {
        "ok": True,
        "task_keys": sorted(task),
        "exposes_claim_lock": "claim_lock" in task,
        "exposes_current_run_id": "current_run_id" in task,
        "status": task.get("status"),
        "run_ids": [run.get("id") for run in payload.get("runs") or []],
    }


def engine_check(env: dict[str, str]) -> dict:
    """Import the installed adapter and report OwnerGrant.from_env()."""
    try:
        from factory_orchestration.adapters.hermes import OwnerGrant
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    grant = OwnerGrant.from_env(env)
    return {
        "available": True,
        "grant": None
        if grant is None
        else {
            "task_id": grant.task_id,
            "run_id": grant.run_id,
            "claim_lock_present": bool(grant.claim_lock),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default=os.environ.get("HERMES_KANBAN_BOARD"))
    parser.add_argument("--card")
    parser.add_argument(
        "--engine-check",
        action="store_true",
        help="import factory_orchestration and evaluate OwnerGrant.from_env",
    )
    parser.add_argument(
        "--require-grant",
        action="store_true",
        help="exit 2 unless this process holds the full dispatcher grant",
    )
    args = parser.parse_args(argv)

    env = dict(os.environ)
    report = describe(env)
    if args.card:
        report["kanban_show"] = kanban_show(args.board, args.card)
    if args.engine_check:
        report["engine"] = engine_check(env)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_grant and report["verdict"] != "grant_in_env":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
