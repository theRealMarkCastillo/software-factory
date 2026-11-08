"""Adapter contracts: workers, gates, review and board ownership.

All boundaries are code, not prompt text telling an agent to obey a graph. Each
adapter returns typed outcomes; a profile that prints "PASS" is never a gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..operations import EffectProbe
from ..state import (
    Attempt,
    GateOutcome,
    ReviewResult,
    VerifyOutcome,
    WorkerReceipt,
    WorkerRequest,
)


@dataclass(frozen=True)
class AttemptView:
    """Read-only projection of the owning board's view of one attempt."""

    task_id: str
    status: str
    assignee: str
    live_run_ids: tuple[int, ...]
    workspace_path: str = ""

    def current(self, attempt: Attempt) -> bool:
        return (
            self.task_id == attempt.task_id
            and self.status == "running"
            and self.assignee == attempt.profile
            and attempt.run_id in self.live_run_ids
        )


@dataclass(frozen=True)
class WorkerHandle:
    operation_key: str
    request_id: str
    pid: int | None = None
    pgid: int | None = None


@dataclass(frozen=True)
class SubmitResult:
    handle: WorkerHandle
    receipt: WorkerReceipt | None = None


@dataclass(frozen=True)
class CancelReport:
    operation_key: str
    cancelled: bool
    detail: str = ""


class BoardAdapter(Protocol):
    def view(self, attempt: Attempt) -> AttemptView: ...

    def heartbeat(self, attempt: Attempt) -> bool: ...


class WorkerAdapter(Protocol):
    def submit(self, request: WorkerRequest) -> SubmitResult: ...

    def poll(self, handle: WorkerHandle) -> WorkerReceipt | None: ...

    def probe(self, operation_key: str) -> EffectProbe: ...

    def cancel(self, handle: WorkerHandle) -> CancelReport: ...


class GateAdapter(Protocol):
    def run_acceptance(
        self,
        *,
        operation_key: str,
        attempt: Attempt,
        workspace: str,
        base_sha: str,
        candidate_sha: str,
        argv: tuple[str, ...],
        artifacts: str,
        timeout_s: int,
    ) -> GateOutcome: ...

    def verify(
        self,
        *,
        artifacts: str,
        repository: str,
        candidate_sha: str,
        base_sha: str,
    ) -> VerifyOutcome: ...

    def probe(self, operation_key: str) -> EffectProbe: ...

    def cancel(self, operation_key: str) -> CancelReport: ...


class ReviewerAdapter(Protocol):
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
    ) -> tuple[WorkerHandle, ReviewResult | None]: ...

    def poll(self, handle: WorkerHandle) -> ReviewResult | None: ...

    def probe(self, operation_key: str) -> EffectProbe: ...

    def cancel(self, handle: WorkerHandle) -> CancelReport: ...


class CapabilityAttestor(Protocol):
    """Proves an OS-level read-only boundary before a live review may run."""

    def enforce_readonly(self, reviewer: str, workspace: str) -> bool: ...
