"""Durable local control plane: bindings, operations, approvals, cancellation.

This is *not* a scheduler. It owns one SQLite file per orchestration state
directory, holding the admission binding (so a moved base, changed graph version
or modified spec fails closed), the operation journal (intent before effect,
receipt after), approvals bound to an exact candidate/base/spec/evidence, the
cancel request and a bounded event/metrics log.

Runtime storage lives outside source repositories; paths are created ``0700``
and database files ``0600``. LangGraph checkpoints live in their own database in
the same directory (``checkpoints.sqlite``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errors import OrchestrationError
from .state import Attempt, RunAssignment

RUN_BINDING_FIELDS = (
    "board_id",
    "card_id",
    "repository",
    "workspace_path",
    "base_sha",
    "spec_reference",
    "spec_digest",
    "template_id",
    "graph_version",
    "state_schema_version",
    "max_repairs",
    "engine_fingerprint",
    "host_policy_digest",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    thread_id                 TEXT PRIMARY KEY,
    board_id                  TEXT NOT NULL,
    card_id                   TEXT NOT NULL,
    hermes_task_id            TEXT NOT NULL,
    hermes_run_id             INTEGER NOT NULL,
    claim_lock                TEXT,
    profile                   TEXT NOT NULL,
    repository                TEXT NOT NULL,
    workspace_path            TEXT NOT NULL,
    base_sha                  TEXT NOT NULL,
    spec_reference            TEXT NOT NULL,
    spec_digest               TEXT NOT NULL,
    template_id               TEXT NOT NULL,
    graph_version             TEXT NOT NULL,
    state_schema_version      INTEGER NOT NULL,
    max_repairs               INTEGER NOT NULL,
    engine_release_reference  TEXT NOT NULL,
    engine_fingerprint        TEXT NOT NULL,
    host_policy_digest        TEXT NOT NULL,
    status                    TEXT NOT NULL,
    repair_count              INTEGER NOT NULL DEFAULT 0,
    created_at                INTEGER NOT NULL,
    updated_at                INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    operation_key  TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    stage          TEXT NOT NULL,
    kind           TEXT NOT NULL,
    repair_index   INTEGER NOT NULL,
    input_digest   TEXT NOT NULL,
    state          TEXT NOT NULL,
    request_id     TEXT,
    receipt_json   TEXT,
    detail         TEXT NOT NULL DEFAULT '',
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operations_thread ON operations(thread_id);
CREATE TABLE IF NOT EXISTS approvals (
    approval_key   TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    reviewer       TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    candidate_sha  TEXT NOT NULL,
    base_sha       TEXT NOT NULL,
    spec_digest    TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    graph_version  TEXT NOT NULL,
    comment        TEXT NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id, ts);
CREATE TABLE IF NOT EXISTS cancels (
    thread_id     TEXT PRIMARY KEY,
    requested_at  INTEGER NOT NULL,
    requested_by  TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> int:
    return int(time.time())


def _token(run_id: str) -> str:
    return hashlib.sha256(
        f"{os.getpid()}:{time.time_ns()}:{run_id}".encode()
    ).hexdigest()[:24]


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return path


class RunStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = _secure_dir(Path(state_dir).expanduser().resolve())
        self.db_path = self.state_dir / "orchestration.sqlite"
        self.checkpoint_path = self.state_dir / "checkpoints.sqlite"
        self._connection: sqlite3.Connection | None = None

    # -- connection ---------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            connection = sqlite3.connect(self.db_path, timeout=60)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=60000")
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                # Another process holds the write lock; WAL is persistent once set.
                pass
            connection.execute("PRAGMA foreign_keys=ON")
            for attempt in range(20):
                try:
                    connection.executescript(_SCHEMA)
                    connection.commit()
                    break
                except sqlite3.OperationalError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
            try:
                self.db_path.chmod(0o600)
            except OSError:  # pragma: no cover
                pass
            self._connection = connection
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- admission binding --------------------------------------------------
    def record_admission(
        self, thread_id: str, assignment: RunAssignment, attempt: Attempt
    ) -> dict:
        """Insert the immutable binding, or fail closed on any difference."""

        expected = {
            "board_id": assignment.board_id,
            "card_id": assignment.card_id,
            "repository": assignment.repository,
            "workspace_path": assignment.workspace_path,
            "base_sha": assignment.immutable_base_sha,
            "spec_reference": assignment.spec_reference,
            "spec_digest": assignment.spec_digest,
            "template_id": assignment.template_id,
            "graph_version": assignment.graph_version,
            "state_schema_version": assignment.state_schema_version,
            "max_repairs": assignment.max_repairs,
            "engine_fingerprint": assignment.engine_fingerprint,
            "host_policy_digest": assignment.host_policy_digest,
        }
        row = self.connection.execute(
            "SELECT * FROM runs WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            now = _now()
            self.connection.execute(
                """
                INSERT INTO runs (
                    thread_id, board_id, card_id, hermes_task_id, hermes_run_id,
                    claim_lock, profile, repository, workspace_path, base_sha,
                    spec_reference, spec_digest, template_id, graph_version,
                    state_schema_version, max_repairs, engine_release_reference,
                    engine_fingerprint, host_policy_digest, status, repair_count,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    thread_id,
                    assignment.board_id,
                    assignment.card_id,
                    attempt.task_id,
                    attempt.run_id,
                    attempt.claim_lock,
                    attempt.profile,
                    assignment.repository,
                    assignment.workspace_path,
                    assignment.immutable_base_sha,
                    assignment.spec_reference,
                    assignment.spec_digest,
                    assignment.template_id,
                    assignment.graph_version,
                    assignment.state_schema_version,
                    assignment.max_repairs,
                    assignment.engine_release_reference,
                    assignment.engine_fingerprint,
                    assignment.host_policy_digest,
                    "running",
                    0,
                    now,
                    now,
                ),
            )
            self.connection.commit()
            self.event(thread_id, "admitted", {"run_id": attempt.run_id})
            return self.run(thread_id) or {}

        existing = dict(row)
        mismatched = [
            field for field, value in expected.items() if existing.get(field) != value
        ]
        if existing["hermes_task_id"] != attempt.task_id:
            mismatched.append("hermes_task_id")
        if mismatched:
            raise OrchestrationError(
                "version_mismatch",
                "admission binding differs for: " + ", ".join(sorted(mismatched)),
            )
        self.connection.execute(
            "UPDATE runs SET updated_at = ?, status = 'running' WHERE thread_id = ?",
            (_now(), thread_id),
        )
        self.connection.commit()
        return dict(row)

    def run(self, thread_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_status(self, thread_id: str, status: str) -> None:
        self.connection.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE thread_id = ?",
            (status, _now(), thread_id),
        )
        self.connection.commit()

    # -- durable repair budget ---------------------------------------------
    def repair_count(self, thread_id: str) -> int:
        row = self.connection.execute(
            "SELECT repair_count FROM runs WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise OrchestrationError("invalid_assignment", "unknown run")
        return int(row["repair_count"])

    def bump_repair(self, thread_id: str) -> int:
        """Atomically consume one repair slot; process restarts cannot reset it."""

        cursor = self.connection.execute(
            "UPDATE runs SET repair_count = repair_count + 1, updated_at = ? "
            "WHERE thread_id = ? AND repair_count < max_repairs",
            (_now(), thread_id),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise OrchestrationError("budget_exhausted", "no repair slots remain")
        self.connection.commit()
        return self.repair_count(thread_id)

    # -- operation journal --------------------------------------------------
    def operation(self, operation_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE operation_key = ?", (operation_key,)
        ).fetchone()
        return dict(row) if row else None

    def operations(self, thread_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM operations WHERE thread_id = ? ORDER BY updated_at, operation_key",
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def begin_operation(
        self,
        operation_key: str,
        thread_id: str,
        *,
        stage: str,
        kind: str,
        repair_index: int,
        input_digest: str,
    ) -> dict:
        existing = self.operation(operation_key)
        if existing is not None:
            if existing["input_digest"] != input_digest:
                raise OrchestrationError(
                    "version_mismatch",
                    "operation key reused with a different input digest",
                )
            return existing
        self.connection.execute(
            "INSERT INTO operations (operation_key, thread_id, stage, kind, "
            "repair_index, input_digest, state, updated_at) "
            "VALUES (?,?,?,?,?,?,'prepared',?)",
            (
                operation_key,
                thread_id,
                stage,
                kind,
                repair_index,
                input_digest,
                _now(),
            ),
        )
        self.connection.commit()
        self.event(thread_id, "operation_prepared", {"operation_key": operation_key})
        return self.operation(operation_key) or {}

    def mark_operation(
        self,
        operation_key: str,
        state: str,
        *,
        request_id: str | None = None,
        receipt: dict | None = None,
        detail: str = "",
    ) -> None:
        if state not in {
            "prepared",
            "submitted",
            "confirmed",
            "failed",
            "unknown",
            "cancelled",
        }:
            raise ValueError(f"unknown operation state {state!r}")
        self.connection.execute(
            "UPDATE operations SET state = ?, request_id = COALESCE(?, request_id), "
            "receipt_json = COALESCE(?, receipt_json), detail = ?, updated_at = ? "
            "WHERE operation_key = ?",
            (
                state,
                request_id,
                json.dumps(receipt, sort_keys=True) if receipt is not None else None,
                detail,
                _now(),
                operation_key,
            ),
        )
        self.connection.commit()

    # -- approvals ----------------------------------------------------------
    @staticmethod
    def approval_key(
        *, candidate_sha: str, base_sha: str, spec_digest: str, evidence_digest: str
    ) -> str:
        payload = f"{candidate_sha}|{base_sha}|{spec_digest}|{evidence_digest}"
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    def record_approval(
        self,
        thread_id: str,
        *,
        reviewer: str,
        outcome: str,
        candidate_sha: str,
        base_sha: str,
        spec_digest: str,
        evidence_digest: str,
        graph_version: str,
        comment: str = "",
    ) -> str:
        key = self.approval_key(
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            spec_digest=spec_digest,
            evidence_digest=evidence_digest,
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO approvals (approval_key, thread_id, reviewer, "
            "outcome, candidate_sha, base_sha, spec_digest, evidence_digest, "
            "graph_version, comment, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                thread_id,
                reviewer,
                outcome,
                candidate_sha,
                base_sha,
                spec_digest,
                evidence_digest,
                graph_version,
                comment,
                _now(),
            ),
        )
        self.connection.commit()
        self.event(
            thread_id,
            "review_recorded",
            {"approval_key": key, "outcome": outcome, "reviewer": reviewer},
        )
        return key

    def approval(self, approval_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM approvals WHERE approval_key = ?", (approval_key,)
        ).fetchone()
        return dict(row) if row else None

    # -- cancellation -------------------------------------------------------
    def request_cancel(self, thread_id: str, *, by: str, reason: str = "") -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO cancels (thread_id, requested_at, requested_by, reason) "
            "VALUES (?,?,?,?)",
            (thread_id, _now(), by, reason),
        )
        self.connection.commit()
        created = cursor.rowcount == 1
        if created:
            self.event(thread_id, "cancel_requested", {"by": by, "reason": reason})
        return created

    def cancel_requested(self, thread_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM cancels WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row is not None

    # -- events / inspect / export -----------------------------------------
    def event(self, thread_id: str, kind: str, detail: dict | None = None) -> None:
        self.connection.execute(
            "INSERT INTO events (thread_id, ts, kind, detail_json) VALUES (?,?,?,?)",
            (thread_id, _now(), kind, json.dumps(detail or {}, sort_keys=True)),
        )
        self.connection.commit()

    def events(self, thread_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE thread_id = ? ORDER BY id", (thread_id,)
        ).fetchall()
        return [
            {
                "ts": row["ts"],
                "kind": row["kind"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def checkpoint_metadata(self) -> dict:
        if not self.checkpoint_path.is_file():
            return {"path": str(self.checkpoint_path), "present": False}
        return {
            "path": str(self.checkpoint_path),
            "present": True,
            "bytes": self.checkpoint_path.stat().st_size,
            "sha256": hashlib.sha256(self.checkpoint_path.read_bytes()).hexdigest(),
        }

    def inspect_view(self, thread_id: str, state: dict | None) -> dict:
        run = self.run(thread_id)
        if run is None:
            raise OrchestrationError("invalid_assignment", f"no run {thread_id}")
        events = self.events(thread_id)
        observed = [event for event in events if event["kind"] == "stage"]
        held = [event for event in events if event["kind"] == "held"]
        cancelled = [event for event in events if event["kind"] == "cancel_requested"]
        last_event_at = events[-1]["ts"] if events else run["created_at"]
        metrics = {
            "observed": True,
            "admitted_at": run["created_at"],
            "last_event_at": last_event_at,
            "elapsed_s": max(0, last_event_at - run["created_at"]),
            "stage_transitions": len(observed),
            "interventions": len(held) + len(cancelled),
            "repairs": int(run["repair_count"]),
            "diagnosis": {
                "current_stage": (state or {}).get("stage", ""),
                "last_reason_code": (state or {}).get("last_reason_code", ""),
                "pending_effects": [
                    record["operation_key"]
                    for record in self.operations(thread_id)
                    if record["state"] in {"submitted", "unknown"}
                ],
            },
            "baseline": {
                "available": False,
                "reason": (
                    "no historical intervention/diagnosis measurements were recorded "
                    "for this host; the pilot reports observed values only"
                ),
            },
        }
        return {
            "run": run,
            "hermes_attempt": {
                "task_id": run["hermes_task_id"],
                "run_id": run["hermes_run_id"],
                "profile": run["profile"],
            },
            "state": state or {},
            "operations": self.operations(thread_id),
            "approval": (
                self.approval(state["approval_reference"])
                if state and state.get("approval_reference")
                else None
            ),
            "cancellation_requested": self.cancel_requested(thread_id),
            "events": events,
            "metrics": metrics,
            "checkpoint": self.checkpoint_metadata(),
            "storage": {
                "state_dir": str(self.state_dir),
                "orchestration_db": str(self.db_path),
            },
        }

    def export(self, thread_id: str, state: dict | None) -> dict:
        view = self.inspect_view(thread_id, state)
        view["export_version"] = 1
        view["exported_at"] = _now()
        return view


@dataclass
class CoordinatorOwner:
    token: str
    pid: int
    host: str
    thread_id: str
    acquired_at: int

    def as_dict(self) -> dict:
        return {
            "token": self.token,
            "pid": self.pid,
            "host": self.host,
            "thread_id": self.thread_id,
            "acquired_at": self.acquired_at,
        }


class CoordinatorLock:
    """One local graph evaluation at a time; kernel-backed, never stolen.

    ``flock`` is released by the kernel when the owning process dies, so a crashed
    run does not require age-based lock theft: the next admission reconciles the
    operation journal instead. While held, a second admission refuses with
    ``workspace_conflict`` and performs no effect.
    """

    def __init__(self, state_dir: str | Path, thread_id: str):
        self.state_dir = _secure_dir(Path(state_dir).expanduser().resolve())
        self.thread_id = thread_id
        self.owner_path = self.state_dir / "coordinator.owner.json"
        self.lock_path = self.state_dir / "coordinator.lock"
        self._handle = None
        self.owner: CoordinatorOwner | None = None

    def acquire(self) -> CoordinatorOwner:
        handle = open(self.lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            current = self.current_owner()
            detail = (
                f"another graph evaluation owns this state directory "
                f"(pid={current.get('pid')}, thread={current.get('thread_id')})"
                if current
                else "another graph evaluation owns this state directory"
            )
            raise OrchestrationError("workspace_conflict", detail) from exc
        self._handle = handle
        self.owner = CoordinatorOwner(
            token=_token(self.thread_id),
            pid=os.getpid(),
            host=socket.gethostname() or "unknown",
            thread_id=self.thread_id,
            acquired_at=_now(),
        )
        self.owner_path.write_text(
            json.dumps(self.owner.as_dict(), sort_keys=True) + "\n"
        )
        try:
            self.owner_path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
        return self.owner

    def current_owner(self) -> dict | None:
        try:
            return json.loads(self.owner_path.read_text())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            current = self.current_owner()
            if current and self.owner and current.get("token") == self.owner.token:
                self.owner_path.unlink(missing_ok=True)
        finally:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "CoordinatorLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


@dataclass(frozen=True)
class AdmissionPolicy:
    """Explicit opt-in: a card runs only when enrolled while admission is on."""

    enabled: bool
    enrolled_cards: frozenset[str]

    @classmethod
    def load(cls, state_dir: str | Path) -> "AdmissionPolicy":
        path = Path(state_dir).expanduser().resolve() / "admission.json"
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(enabled=False, enrolled_cards=frozenset())
        except (OSError, ValueError) as exc:
            raise OrchestrationError(
                "invalid_assignment", f"unreadable admission policy: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise OrchestrationError(
                "invalid_assignment", "admission policy must be a JSON object"
            )
        cards = payload.get("enrolled_cards", [])
        if not isinstance(cards, list) or not all(isinstance(c, str) for c in cards):
            raise OrchestrationError(
                "invalid_assignment", "enrolled_cards must be a list of card ids"
            )
        return cls(
            enabled=payload.get("enabled") is True,
            enrolled_cards=frozenset(cards),
        )

    def check(self, card_id: str) -> None:
        if not self.enabled:
            raise OrchestrationError(
                "admission_disabled", "graph admission is disabled for this host"
            )
        if card_id not in self.enrolled_cards:
            raise OrchestrationError(
                "invalid_assignment", f"card {card_id} is not enrolled"
            )


@contextmanager
def coordinator(state_dir: str | Path, thread_id: str) -> Iterator[CoordinatorOwner]:
    lock = CoordinatorLock(state_dir, thread_id)
    owner = lock.acquire()
    try:
        yield owner
    finally:
        lock.release()
