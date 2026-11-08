"""Deterministic offline adapters backed by durable files, not just mocks.

Every fake adapter writes its request and receipt into a directory on disk, so
crash-window tests can inspect the durable target after a killed process instead
of asserting on in-process call counts. The fake gate delegates verification to
the real engine verifier (``factory.evidence.verify_evidence``): a fake may not
substitute a green flag for evidence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..operations import EffectProbe
from ..state import (
    Attempt,
    GateOutcome,
    ReviewResult,
    VerifyOutcome,
    WorkerReceipt,
    WorkerRequest,
)
from .base import AttemptView, CancelReport, SubmitResult, WorkerHandle


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class CrashPlan:
    """Test-only fault injection at a named phase, for the named stage."""

    phase: str
    stage: str
    marker_dir: Path | None = None
    exit_code: int = 97

    def maybe(self, phase: str, stage: str, key: str = "") -> None:
        if phase != self.phase or stage != self.stage:
            return
        if self.marker_dir is not None:
            _write_json(
                self.marker_dir / f"crashed-{self.phase}-{self.stage}.json",
                {"phase": self.phase, "stage": self.stage, "key": key},
            )
        os._exit(self.exit_code)


@dataclass
class FakeBoard:
    """An in-memory board projection of the owning Hermes attempt."""

    views: dict[str, AttemptView] = field(default_factory=dict)
    heartbeats: list[int] = field(default_factory=list)

    def set_current(self, attempt: Attempt, *, live: bool = True) -> None:
        self.views[attempt.task_id] = AttemptView(
            task_id=attempt.task_id,
            status="running" if live else "ready",
            assignee=attempt.profile,
            live_run_ids=(attempt.run_id,) if live else (),
        )

    def view(self, attempt: Attempt) -> AttemptView:
        return self.views.get(
            attempt.task_id,
            AttemptView(attempt.task_id, "unknown", "", ()),
        )

    def heartbeat(self, attempt: Attempt) -> bool:
        current = self.view(attempt).current(attempt)
        if current:
            self.heartbeats.append(attempt.run_id)
        return current


@dataclass
class FakeWorker:
    root: Path
    candidate_sha: str
    outcomes: list[str] = field(default_factory=lambda: ["completed"])
    crash: CrashPlan | None = None
    opaque: bool = False
    submitted: list[str] = field(default_factory=list)

    # -- paths --------------------------------------------------------------
    def _request_path(self, key: str) -> Path:
        return self.root / "worker-requests" / f"{key.replace(':', '_')}.json"

    def _receipt_path(self, key: str) -> Path:
        return self.root / "worker-receipts" / f"{key.replace(':', '_')}.json"

    def _outcome_for(self, request: WorkerRequest) -> str:
        if not self.outcomes:
            return "completed"
        index = min(request.repair_index, len(self.outcomes) - 1)
        return self.outcomes[index]

    def submit(self, request: WorkerRequest) -> SubmitResult:
        if self.crash:
            self.crash.maybe("before_submit", "request_implementation")
        _write_json(
            self._request_path(request.operation_key),
            {
                "operation_key": request.operation_key,
                "stage": "request_implementation",
                "profile": request.profile,
                "repair_index": request.repair_index,
                "input_digest": request.spec_digest,
                "workspace": request.workspace,
            },
        )
        self.submitted.append(request.operation_key)
        if self.crash:
            self.crash.maybe("after_submit", "request_implementation")
        outcome = self._outcome_for(request)
        if outcome != "pending":
            receipt = WorkerReceipt(
                operation_key=request.operation_key,
                attempt=request.attempt,
                session_id=f"fake-session-{len(self.submitted)}",
                outcome=outcome,
                candidate_sha=self.candidate_sha if outcome == "completed" else None,
                artifact_references=(),
                reported_tests=("pytest -q",),
                detail=""
                if outcome == "completed"
                else f"fake worker reported {outcome}",
            )
            _write_json(self._receipt_path(request.operation_key), receipt.as_dict())
        if self.crash:
            self.crash.maybe("after_receipt", "request_implementation")
        return SubmitResult(
            handle=WorkerHandle(
                operation_key=request.operation_key,
                request_id=f"fake-request-{len(self.submitted)}",
            )
        )

    def poll(self, handle: WorkerHandle) -> WorkerReceipt | None:
        payload = _read_json(self._receipt_path(handle.operation_key))
        if payload is None:
            return None
        from ..operations import _attempt_from

        return WorkerReceipt(
            operation_key=payload["operation_key"],
            attempt=_attempt_from(payload["attempt"]),
            session_id=payload["session_id"],
            outcome=payload["outcome"],
            candidate_sha=payload.get("candidate_sha"),
            artifact_references=tuple(payload.get("artifact_references") or ()),
            reported_tests=tuple(payload.get("reported_tests") or ()),
            detail=payload.get("detail", ""),
        )

    def probe(self, operation_key: str) -> EffectProbe:
        if self.opaque:
            return EffectProbe("unknown", detail="fake target is opaque")
        receipt = _read_json(self._receipt_path(operation_key))
        if receipt is not None:
            return EffectProbe("present", receipt=receipt)
        if _read_json(self._request_path(operation_key)) is not None:
            return EffectProbe("present", detail="request accepted, no receipt yet")
        return EffectProbe("absent")

    def cancel(self, handle: WorkerHandle) -> CancelReport:
        _write_json(
            self.root
            / "worker-cancels"
            / f"{handle.operation_key.replace(':', '_')}.json",
            {"operation_key": handle.operation_key, "cancelled": True},
        )
        return CancelReport(handle.operation_key, True)


@dataclass
class FakeGate:
    """Records acceptance; verifies through the real engine verifier."""

    root: Path
    outcome: str = "pass"
    evidence_dir: Path | None = None
    crash: CrashPlan | None = None
    submissions: list[str] = field(default_factory=list)

    def _receipt_path(self, key: str) -> Path:
        return self.root / "gate-receipts" / f"{key.replace(':', '_')}.json"

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
    ) -> GateOutcome:
        if self.crash:
            self.crash.maybe("before_submit", "run_acceptance")
        self.submissions.append(operation_key)
        _write_json(
            self._receipt_path(operation_key),
            {
                "stage": "run_acceptance",
                "argv": list(argv),
                "workspace": workspace,
                "base_sha": base_sha,
                "candidate_sha": candidate_sha,
                "outcome": self.outcome,
            },
        )
        if self.crash:
            self.crash.maybe("after_receipt", "run_acceptance")
        if self.outcome != "pass":
            return GateOutcome(self.outcome, reason="gate_failed:acceptance")
        if self.evidence_dir is None:
            return GateOutcome(
                "inconclusive", reason="protocol_error", detail="no evidence"
            )
        return GateOutcome(
            "pass",
            evidence_reference=str(self.evidence_dir),
            evidence_digest=_manifest_digest(self.evidence_dir),
        )

    def verify(
        self,
        *,
        artifacts: str,
        repository: str,
        candidate_sha: str,
        base_sha: str,
    ) -> VerifyOutcome:
        from factory.evidence import verify_evidence

        from ..evidence_guard import manifest_guard

        guarded, guard_reason = manifest_guard(artifacts)
        if not guarded:
            return VerifyOutcome(
                certified=False,
                reason=guard_reason,
                detail="manifest byte guard rejected the evidence before verification",
            )
        result = verify_evidence(artifacts, repository, candidate_sha, base_sha)
        return VerifyOutcome(
            certified=bool(result.ok),
            reason=result.reason,
            detail=result.detail,
            evidence_digest=_manifest_digest(artifacts),
        )

    def probe(self, operation_key: str) -> EffectProbe:
        receipt = _read_json(self._receipt_path(operation_key))
        if receipt is not None:
            return EffectProbe("present", receipt=receipt)
        return EffectProbe("absent")

    def cancel(self, operation_key: str) -> CancelReport:
        _write_json(
            self.root / "gate-cancels" / f"{operation_key.replace(':', '_')}.json",
            {"operation_key": operation_key, "cancelled": True},
        )
        return CancelReport(operation_key, True)


def _manifest_digest(directory: str | Path) -> str:
    import hashlib

    path = Path(directory) / "evidence-manifest.json"
    if not path.is_file():
        return ""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class FakeReviewer:
    root: Path
    outcome: str = "approve"
    candidate_override: str | None = None
    crash: CrashPlan | None = None
    submissions: list[str] = field(default_factory=list)

    def _receipt_path(self, key: str) -> Path:
        return self.root / "review-receipts" / f"{key.replace(':', '_')}.json"

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
        if self.crash:
            self.crash.maybe("before_submit", "request_independent_review")
        self.submissions.append(operation_key)
        handle = WorkerHandle(operation_key, f"fake-review-{len(self.submissions)}")
        if self.outcome == "pending":
            return handle, None
        result = ReviewResult(
            operation_key=operation_key,
            attempt=attempt,
            reviewer=reviewer,
            outcome=self.outcome,
            candidate_sha=self.candidate_override or candidate_sha,
            base_sha=base_sha,
            spec_digest=spec_digest,
            evidence_digest=evidence_digest,
            graph_version="factory-card/v1",
            comment="fake review",
        )
        _write_json(self._receipt_path(operation_key), _review_payload(result))
        if self.crash:
            self.crash.maybe("after_receipt", "request_independent_review")
        return handle, result

    def poll(self, handle: WorkerHandle) -> ReviewResult | None:
        payload = _read_json(self._receipt_path(handle.operation_key))
        if payload is None:
            return None
        from ..operations import _attempt_from

        return ReviewResult(
            operation_key=payload["operation_key"],
            attempt=_attempt_from(payload["attempt"]),
            reviewer=payload["reviewer"],
            outcome=payload["outcome"],
            candidate_sha=payload["candidate_sha"],
            base_sha=payload["base_sha"],
            spec_digest=payload["spec_digest"],
            evidence_digest=payload["evidence_digest"],
            graph_version=payload["graph_version"],
            comment=payload.get("comment", ""),
        )

    def probe(self, operation_key: str) -> EffectProbe:
        receipt = _read_json(self._receipt_path(operation_key))
        if receipt is not None:
            return EffectProbe("present", receipt=receipt)
        return EffectProbe("absent")

    def cancel(self, handle: WorkerHandle) -> CancelReport:
        _write_json(
            self.root
            / "review-cancels"
            / f"{handle.operation_key.replace(':', '_')}.json",
            {"operation_key": handle.operation_key, "cancelled": True},
        )
        return CancelReport(handle.operation_key, True)


def _review_payload(result: ReviewResult) -> dict:
    return {
        "operation_key": result.operation_key,
        "attempt": result.attempt.as_dict(),
        "reviewer": result.reviewer,
        "outcome": result.outcome,
        "candidate_sha": result.candidate_sha,
        "base_sha": result.base_sha,
        "spec_digest": result.spec_digest,
        "evidence_digest": result.evidence_digest,
        "graph_version": result.graph_version,
        "comment": result.comment,
    }
