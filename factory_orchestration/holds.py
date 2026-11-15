"""Deterministic engine-originated hold summaries, not decision inference.

Only the record is a diagnostic projection: immutable assignment references and
repair context are never rewritten. No specification content is read here.
"""

from __future__ import annotations

from .redaction import redact_diagnostic

_FALLBACKS = {
    "hold_budget": "budget_exhausted",
    "hold_infrastructure": "worker_unavailable",
    "hold_reconciliation": "effect_unknown",
    "hold_operator": "protocol_error",
}


def _text(value: object, limit: int) -> str:
    # Redact the entire original value before taking a valid UTF-8 prefix.
    raw = redact_diagnostic(str(value or "")).encode("utf-8", errors="replace")
    return raw[:limit].decode("utf-8", errors="ignore")


def hold_record(state: dict, *, node: str = "") -> dict:
    """Project a pre-hold state using the existing reason (or route fallback)."""

    source = (state.get("repair_context") or {}).get("source")
    return {
        "stage": _text(source or state.get("stage"), 128),
        "reason": _text(
            state.get("last_reason_code")
            or _FALLBACKS.get(
                node or state.get("stage") or state.get("route") or "",
                _FALLBACKS.get(state.get("route") or "", "protocol_error"),
            ),
            128,
        ),
        "detail": _text(state.get("last_reason_detail"), 4096),
        "spec_ref": _text(state.get("spec_reference"), 1024),
        "spec_excerpt": _text("", 1024),
        "question": _text("", 1024),
        "options": [],
        "recommendation": _text("", 1024),
        "answer_format": _text("", 1024),
    }
