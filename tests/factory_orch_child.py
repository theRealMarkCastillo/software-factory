"""Child process harness for crash-window and concurrency tests.

Run as ``python tests/factory_orch_child.py <config.json>``. It builds the same
offline adapters as the parent, optionally injects a process death at a named
phase, and writes a small result record. The durable effect directory is what
the parent inspects afterwards.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from factory_orch_fixtures import CARD, make_assignment  # noqa: E402

from factory_orchestration.adapters.fake import (  # noqa: E402
    CrashPlan,
    FakeBoard,
    FakeGate,
    FakeReviewer,
    FakeWorker,
)
from factory_orchestration.errors import OrchestrationError  # noqa: E402
from factory_orchestration.graph import Deps, thread_id  # noqa: E402
from factory_orchestration.runner import cancel, drive, resume  # noqa: E402
from factory_orchestration.state import Attempt  # noqa: E402
from factory_orchestration.store import RunStore  # noqa: E402


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text())
    start_file = config.get("start_file")
    if start_file:
        import time

        deadline = time.time() + 20
        while not Path(start_file).exists() and time.time() < deadline:
            time.sleep(0.02)
    attempt = Attempt(
        task_id=CARD,
        run_id=int(config.get("attempt_run_id", 7)),
        profile="factory-runner",
    )
    board = FakeBoard()
    live_run_id = int(config.get("live_run_id", attempt.run_id))
    board.set_current(
        Attempt(task_id=CARD, run_id=live_run_id, profile="factory-runner"),
        live=config.get("attempt_live", True),
    )
    crash = None
    if config.get("crash_phase"):
        crash = CrashPlan(
            phase=config["crash_phase"],
            stage=config["crash_stage"],
            marker_dir=Path(config["marker_dir"]) if config.get("marker_dir") else None,
        )
    worker = FakeWorker(
        root=Path(config["effects"]),
        candidate_sha=config["candidate"],
        outcomes=list(config.get("worker_outcomes") or ["completed"]),
        crash=crash,
    )
    gate = FakeGate(
        root=Path(config["effects"]),
        evidence_dir=Path(config["artifacts"]),
        crash=(
            CrashPlan(
                phase=config["gate_crash_phase"],
                stage="run_acceptance",
                marker_dir=Path(config["marker_dir"])
                if config.get("marker_dir")
                else None,
            )
            if config.get("gate_crash_phase")
            else None
        ),
    )
    reviewer = FakeReviewer(
        root=Path(config["effects"]),
        outcome=config.get("reviewer_outcome", "approve"),
        compact_receipt=bool(config.get("compact_review_receipt")),
        crash=(
            CrashPlan(
                phase=config["review_crash_phase"],
                stage="request_independent_review",
                marker_dir=Path(config["marker_dir"])
                if config.get("marker_dir")
                else None,
            )
            if config.get("review_crash_phase")
            else None
        ),
    )
    deps = Deps(
        store=RunStore(Path(config["state_dir"])),
        board=board,
        worker=worker,
        gate=gate,
        reviewer=reviewer,
        state_dir=Path(config["state_dir"]),
    )
    assignment = make_assignment(
        repo=Path(config["repo"]),
        workspace=Path(config["workspace"]),
        base=config["base"],
        spec=Path(config["spec"]),
        digest=config["digest"],
        artifacts=Path(config["artifacts"]),
    )
    result_path = Path(config["result_path"])
    try:
        action = config.get("action", "run")
        if action == "cancel":
            outcome = cancel(deps, thread_id(assignment), by="test-child")
            payload = {"ok": True, "cancel": outcome}
        elif action == "resume":
            payload_path = Path(config["resume_payload"])
            resumed = resume(
                deps,
                thread_id(assignment),
                attempt,
                payload=json.loads(payload_path.read_text()),
                resolved_hold=config.get("resolved_hold", "test resume"),
            )
            payload = {"ok": True, "result": resumed.as_dict()}
        else:
            driven = drive(deps, assignment, attempt)
            payload = {"ok": True, "result": driven.as_dict()}
        if config.get("attempt_live") is False:
            board.set_current(attempt, live=False)
        result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if config.get("exit_after_drive"):
            os._exit(97)
        return 0
    except OrchestrationError as exc:
        result_path.write_text(
            json.dumps(
                {"ok": False, "reason": exc.reason, "detail": exc.detail},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 4
    except BaseException as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        result_path.write_text(
            json.dumps({"ok": False, "reason": "unexpected", "detail": repr(exc)})
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
