"""Supported Hermes adapter surfaces, discovered in G0 (no invented flags).

Invocation is the documented CLI boundary: ``hermes -p <profile> chat -q ...``
with ``--format stream-json``. That gives a durable ``session_id`` and a terminal
``result`` record, and it never touches the board. Ownership is validated through
``hermes kanban show --json`` (read-only) and ``hermes kanban heartbeat``
(run-scoped liveness). See ``docs/langgraph-hybrid/g0-capabilities.md``.

Workers spawned here are ordinary Hermes profile sessions owned by this process
group. They are not Kanban-managed attempts, so this adapter never claims,
reassigns or completes a card.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import OrchestrationError
from ..operations import EffectProbe
from ..state import Attempt, ReviewResult, WorkerReceipt, WorkerRequest
from .base import AttemptView, CancelReport, SubmitResult, WorkerHandle

# Ownership markers belong to the dispatcher's attempt. A worker subprocess must
# not inherit them: it must never believe it owns a card it does not own.
OWNERSHIP_ENV = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_TENANT",
    "HERMES_SESSION_SOURCE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
)


def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in OWNERSHIP_ENV}
    env.pop("HERMES_KANBAN_HOME", None)
    return env


@dataclass
class HermesBoard:
    """Read-only attempt projection plus run-scoped heartbeat."""

    executable: str = "hermes"
    board: str | None = None
    timeout_s: int = 60

    def _board_args(self) -> list[str]:
        if self.board:
            return ["--board", self.board]
        return []

    def show(self, task_id: str) -> dict:
        argv = [
            self.executable,
            "kanban",
            *self._board_args(),
            "show",
            task_id,
            "--json",
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(
                "worker_unavailable", f"hermes kanban show failed: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise OrchestrationError(
                "worker_unavailable",
                f"hermes kanban show exited {completed.returncode}: "
                f"{completed.stderr.strip()[:200]}",
            )
        try:
            return json.loads(completed.stdout)
        except ValueError as exc:
            raise OrchestrationError(
                "protocol_error", "hermes kanban show did not return JSON"
            ) from exc

    def view(self, attempt: Attempt) -> AttemptView:
        payload = self.show(attempt.task_id)
        task = payload.get("task") or {}
        runs = payload.get("runs") or []
        live = tuple(
            int(run["id"])
            for run in runs
            if run.get("status") == "running" and run.get("id") is not None
        )
        return AttemptView(
            task_id=str(task.get("id") or ""),
            status=str(task.get("status") or "unknown"),
            assignee=str(task.get("assignee") or ""),
            live_run_ids=live,
            workspace_path=str(task.get("workspace_path") or ""),
        )

    def heartbeat(self, attempt: Attempt) -> bool:
        """Run-scoped heartbeat: proves this run is still the current attempt.

        ``kanban heartbeat`` refuses when ``HERMES_KANBAN_RUN_ID`` is not the
        card's current run, so a superseded worker cannot extend its lease.
        """

        argv = [
            self.executable,
            "kanban",
            *self._board_args(),
            "heartbeat",
            attempt.task_id,
        ]
        env = _clean_env()
        env["HERMES_KANBAN_TASK"] = attempt.task_id
        env["HERMES_KANBAN_RUN_ID"] = str(attempt.run_id)
        if attempt.claim_lock:
            env["HERMES_KANBAN_CLAIM_LOCK"] = attempt.claim_lock
        if self.board:
            env["HERMES_KANBAN_BOARD"] = self.board
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0


@dataclass
class HermesWorker:
    """One ``hermes -p PROFILE chat -q`` subprocess per operation request."""

    artifact_root: Path
    executable: str = "hermes"
    max_turns: int = 200
    terminate_grace_s: float = 10.0
    _processes: dict[str, subprocess.Popen] = field(default_factory=dict)
    _records: dict[str, dict] = field(default_factory=dict)

    def _dir(self, key: str) -> Path:
        safe = key.replace(":", "_")
        return Path(self.artifact_root) / "hermes-worker" / safe

    def _request_path(self, key: str) -> Path:
        return self._dir(key) / "request.json"

    def _receipt_path(self, key: str) -> Path:
        return self._dir(key) / "receipt.json"

    def submit(self, request: WorkerRequest) -> SubmitResult:
        directory = self._dir(request.operation_key)
        directory.mkdir(parents=True, exist_ok=True)
        transcript = directory / "stream.jsonl"
        argv = [
            self.executable,
            "-p",
            request.profile,
            "chat",
            "-q",
            request.prompt,
            "--oneshot",
            "--format",
            "stream-json",
            "--max-turns",
            str(self.max_turns),
            "--in",
            request.workspace,
        ]
        (self._request_path(request.operation_key)).write_text(
            json.dumps(
                {
                    "operation_key": request.operation_key,
                    "argv": argv,
                    "workspace": request.workspace,
                    "spec_digest": request.spec_digest,
                    "repair_index": request.repair_index,
                    "submitted_at": int(time.time()),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        log_file = None
        try:
            log_file = open(transcript, "wb")
            process = subprocess.Popen(
                argv,
                cwd=request.workspace,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=_clean_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise OrchestrationError(
                "worker_unavailable", f"could not start hermes worker: {exc}"
            ) from exc
        finally:
            if log_file is not None:
                log_file.close()
        self._processes[request.operation_key] = process
        self._records[request.operation_key] = {
            "request_id": f"hermes-pid-{process.pid}",
            "started": time.time(),
            "timeout_s": request.timeout_s,
            "transcript": str(transcript),
            "result_path": str(directory / "worker-result.json"),
            "attempt": request.attempt.as_dict(),
        }
        return SubmitResult(
            handle=WorkerHandle(
                operation_key=request.operation_key,
                request_id=f"hermes-pid-{process.pid}",
                pid=process.pid,
                pgid=process.pid,
            )
        )

    def poll(self, handle: WorkerHandle) -> WorkerReceipt | None:
        process = self._processes.get(handle.operation_key)
        record = self._records.get(handle.operation_key) or {}
        if process is None:
            payload = self._read_result(handle.operation_key)
            if payload is None:
                return None
            return self._receipt_from(handle.operation_key, payload, session_id="")
        exit_code = process.poll()
        if exit_code is None:
            started = float(record.get("started", time.time()))
            if time.time() - started > float(record.get("timeout_s", 3600)):
                self.cancel(handle)
                return self._failed(
                    handle.operation_key, "unavailable", "worker exceeded its timeout"
                )
            return None
        session_id, failed = self._parse_transcript(record.get("transcript", ""))
        if exit_code != 0 or failed:
            detail = f"worker exited {exit_code}"
            return self._failed(handle.operation_key, "failed", detail)
        payload = self._read_result(handle.operation_key)
        if payload is None:
            return self._failed(
                handle.operation_key,
                "ambiguous",
                "worker exited 0 without a structured result file",
            )
        return self._receipt_from(handle.operation_key, payload, session_id=session_id)

    def _parse_transcript(self, path: str) -> tuple[str, bool]:
        session_id = ""
        failed = False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "system" and event.get("session_id"):
                        session_id = str(event["session_id"])
                    if event.get("type") == "result":
                        session_id = str(event.get("session_id") or session_id)
                        failed = bool(event.get("exit_code"))
        except OSError:
            return session_id, True
        return session_id, failed

    def _read_result(self, key: str) -> dict | None:
        record = self._records.get(key) or {}
        path = Path(record.get("result_path", self._dir(key) / "worker-result.json"))
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _receipt_from(
        self, key: str, payload: dict, *, session_id: str
    ) -> WorkerReceipt:
        attempt = payload.get("attempt") or {}
        from ..operations import _attempt_from

        try:
            parsed_attempt = _attempt_from(attempt)
        except (KeyError, TypeError, ValueError):
            raise OrchestrationError(
                "protocol_error", "worker result carries no valid attempt identity"
            ) from None
        return WorkerReceipt(
            operation_key=key,
            attempt=parsed_attempt,
            session_id=session_id or str(payload.get("session_id") or ""),
            outcome=str(payload.get("outcome") or "completed"),
            candidate_sha=payload.get("candidate_sha"),
            artifact_references=tuple(payload.get("artifact_references") or ()),
            reported_tests=tuple(payload.get("reported_tests") or ()),
            detail=str(payload.get("detail") or ""),
        )

    def _failed(self, key: str, outcome: str, detail: str) -> WorkerReceipt:
        record = self._records.get(key) or {}
        attempt_payload = record.get("attempt") or {}
        from ..operations import _attempt_from

        try:
            attempt = _attempt_from(attempt_payload)
        except (KeyError, TypeError, ValueError):
            raise OrchestrationError(
                "protocol_error", "worker failure carries no attempt identity"
            ) from None
        return WorkerReceipt(
            operation_key=key,
            attempt=attempt,
            session_id="",
            outcome=outcome,
            detail=detail,
        )

    def probe(self, operation_key: str) -> EffectProbe:
        if self._receipt_path(operation_key).is_file():
            return EffectProbe(
                "present",
                receipt=json.loads(self._receipt_path(operation_key).read_text()),
            )
        if self._request_path(operation_key).is_file():
            return EffectProbe(
                "unknown",
                detail="a worker was already launched; its mutations cannot be disproved",
            )
        return EffectProbe("absent")

    def cancel(self, handle: WorkerHandle) -> CancelReport:
        process = self._processes.get(handle.operation_key)
        if process is None:
            return CancelReport(
                handle.operation_key,
                False,
                "process is not owned by this evaluation; reconcile the target",
            )
        if process.poll() is not None:
            return CancelReport(handle.operation_key, True, "already exited")
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return CancelReport(handle.operation_key, True, "already exited")
        deadline = time.time() + self.terminate_grace_s
        while time.time() < deadline:
            if process.poll() is not None:
                return CancelReport(handle.operation_key, True, "terminated")
            time.sleep(0.05)
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=self.terminate_grace_s)
        return CancelReport(handle.operation_key, True, "killed after grace period")


class NoReadonlyEnforcement:
    """Default attestor: the host provides no read-only boundary for a live run.

    Hermes exposes write tools (``file`` includes ``write_file``/``patch``, and
    ``terminal``/``execute_code`` can write) in the same UID. Restricting a
    toolset is not a capability boundary, so the adapter refuses to describe a
    live review as read-only.
    """

    def enforce_readonly(self, reviewer: str, workspace: str) -> bool:
        return False


@dataclass
class HermesReviewer:
    worker: HermesWorker
    attestor: object = field(default_factory=NoReadonlyEnforcement)

    def submit(
        self,
        *,
        operation_key: str,
        attempt: Attempt,
        reviewer: str,
        workspace: str,
        candidate_sha: str,
        base_sha: str,
        spec_digest: str,
        evidence_digest: str,
        artifacts: str,
        timeout_s: int,
    ) -> tuple[WorkerHandle, ReviewResult | None]:
        if not self.attestor.enforce_readonly(reviewer, workspace):
            raise OrchestrationError(
                "capability_unenforced",
                "no OS-level read-only boundary was proven for a live reviewer; "
                "capability acceptance is blocked, not passed",
            )
        raise OrchestrationError(
            "capability_unenforced",
            "live reviewer transport is intentionally not activated in this increment",
        )

    def poll(self, handle: WorkerHandle) -> ReviewResult | None:  # pragma: no cover
        return None

    def probe(self, operation_key: str) -> EffectProbe:  # pragma: no cover
        return EffectProbe("absent")

    def cancel(self, handle: WorkerHandle) -> CancelReport:  # pragma: no cover
        return CancelReport(handle.operation_key, True)
