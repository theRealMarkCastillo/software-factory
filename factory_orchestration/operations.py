"""Stable effect identities and the intent/receipt reconciliation protocol.

A checkpoint is not a transaction across Git, Hermes, Docker and APIs. Each
effect therefore gets a stable key derived from the run identity, the stage, the
logical repair index and the input digest. Intent is persisted *before* the
effect; the request handle and the receipt are persisted after it.

Re-execution (crash, interrupt replay, resume) consults the journal and
reconciles the actual target first:

* ``prepared``            -> no effect was submitted; submit.
* ``submitted`` + absent  -> the target proved no effect exists; submit.
* ``submitted`` + present -> adopt the existing effect; do not resubmit.
* ``submitted`` + unknown -> HOLD (``effect_unknown``); never resubmit.
* ``confirmed``           -> replay the state update only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .errors import OrchestrationError
from .state import WorkerReceipt

EFFECT_STATES = ("present", "absent", "unknown")


def digest_parts(*parts: object) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_key(*, run_id: str, stage: str, repair_index: int, input_digest: str) -> str:
    """Deterministic operation identity; repair index is part of the identity."""

    return digest_parts("operation/v1", run_id, stage, repair_index, input_digest)


@dataclass(frozen=True)
class EffectProbe:
    state: str
    request_id: str = ""
    receipt: dict | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in EFFECT_STATES:
            raise OrchestrationError(
                "protocol_error", f"unknown effect probe state {self.state!r}"
            )


@dataclass(frozen=True)
class Resolution:
    """What a node should do with a possibly-existing effect."""

    action: str  # "submit" | "adopt" | "hold" | "replay"
    record: dict
    receipt: dict | None = None
    detail: str = ""

    @property
    def receipt_object(self) -> WorkerReceipt | None:
        if not self.receipt:
            return None
        return WorkerReceipt(
            operation_key=self.receipt["operation_key"],
            attempt=_attempt_from(self.receipt["attempt"]),
            session_id=self.receipt["session_id"],
            outcome=self.receipt["outcome"],
            candidate_sha=self.receipt.get("candidate_sha"),
            artifact_references=tuple(self.receipt.get("artifact_references") or ()),
            reported_tests=tuple(self.receipt.get("reported_tests") or ()),
            detail=self.receipt.get("detail", ""),
        )


def _attempt_from(payload: dict):
    from .state import Attempt

    return Attempt(
        task_id=payload["task_id"],
        run_id=int(payload["run_id"]),
        profile=payload["profile"],
        claim_lock=payload.get("claim_lock"),
    )


def resolve(record: dict, probe: EffectProbe | None) -> Resolution:
    """Decide how to proceed from a journal record and an optional target probe."""

    state = record["state"]
    if state == "confirmed":
        return Resolution("replay", record, _receipt(record))
    if state in {"failed", "cancelled"}:
        return Resolution("submit", record, detail=f"previous state {state}")
    if state == "prepared":
        return Resolution("submit", record, detail="no effect was submitted")
    if state in {"submitted", "unknown"}:
        if probe is None:
            return Resolution(
                "hold",
                record,
                detail="effect target cannot prove presence or absence",
            )
        if probe.state == "present":
            return Resolution("adopt", record, probe.receipt, probe.detail)
        if probe.state == "absent":
            return Resolution("submit", record, detail="target proved no effect")
        return Resolution(
            "hold",
            record,
            detail=probe.detail or "effect target cannot prove presence or absence",
        )
    return Resolution("hold", record, detail=f"unrecognized operation state {state!r}")


def _receipt(record: dict) -> dict | None:
    raw = record.get("receipt_json")
    if not raw:
        return None
    import json

    return json.loads(raw)
