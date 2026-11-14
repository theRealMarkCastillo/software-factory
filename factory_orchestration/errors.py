"""Closed reason/error vocabulary for the orchestration adapter.

Every hold, refusal and diagnostic carries one of :data:`REASON_CODES`. Wrapper
categories never replace the underlying verifier reason: ``detail`` keeps it.
"""

from __future__ import annotations

from dataclasses import dataclass

REASON_CODES = frozenset(
    {
        "ok",
        # assignment / admission
        "invalid_assignment",
        "admission_disabled",
        "workspace_conflict",
        "version_mismatch",
        "stale_attempt",
        "cross_attempt_resume_unsupported",
        "owner_grant_absent",
        # ownership / controls
        "cancellation_pending",
        "cancelled",
        "budget_exhausted",
        "not_converging",
        # worker
        "worker_failed",
        "worker_unavailable",
        "protocol_error",
        "effect_unknown",
        # gates and review
        "evidence_rejected",
        "review_changes",
        "review_inconclusive",
        "review_scope_violation",
        "review_unavailable",
        "review_credentials_unavailable",
        "gate_unavailable",
        "capability_unenforced",
    }
)


class OrchestrationError(Exception):
    """A refusal that a node converts into a bounded route or a hold."""

    def __init__(self, reason: str, detail: str = "", route: str | None = None):
        if reason not in REASON_CODES:
            raise ValueError(f"unknown reason code: {reason}")
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.route = route


class HoldError(OrchestrationError):
    """A condition that must stop the graph and wait for an operator decision."""


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.code not in REASON_CODES:
            raise ValueError(f"unknown reason code: {self.code}")

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}
