"""Closed, data-only worker decisions. Validate before redacting or persisting."""

from __future__ import annotations

import json
import re

from .errors import OrchestrationError
from .redaction import redact_diagnostic

MARKER = "FACTORY_NEEDS_DECISION_V1"
MAX_DECISION_BYTES = 16384
_KEYS = {
    "spec_ref",
    "spec_excerpt",
    "question",
    "options",
    "recommendation",
    "answer_format",
}
_OPTION_KEYS = {"id", "label", "consequence"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,16}")


def invalid_decision() -> OrchestrationError:
    # Never interpolate untrusted values into diagnostics (including JSON errors).
    return OrchestrationError("protocol_error", "invalid worker decision")


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise invalid_decision()
        result[key] = value
    return result


def _constant(value: str):
    raise invalid_decision()


def strict_json(text: str):
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        raise invalid_decision() from None


def worker_payload(text: str) -> object:
    """Strict sidechannel JSON, bounding the original decision value span.

    Parsing to a dict alone erases excessive internal whitespace. Scan the
    syntax-checked root members to measure the actual serialized value.
    """
    # Decode root members without overwriting duplicate keys. Seeing a decision
    # key claims the protocol even if its value is malformed or overwritten.
    decoder = json.JSONDecoder()
    index = len(text) - len(text.lstrip())
    claimed = False
    try:
        if text[index] == "{":
            index += 1
            while True:
                index = len(text) - len(text[index:].lstrip())
                key, index = decoder.raw_decode(text, index)
                index = len(text) - len(text[index:].lstrip())
                if text[index] != ":":
                    break
                index += 1
                if key == "decision":
                    claimed = True
                    break
                index = len(text) - len(text[index:].lstrip())
                value, index = decoder.raw_decode(text, index)
                if key == "outcome" and value == "needs_decision":
                    claimed = True
                    break
                index = len(text) - len(text[index:].lstrip())
                if text[index] != ",":
                    break
                index += 1
    except (ValueError, IndexError, RecursionError):
        pass
    if not claimed:
        try:
            return json.loads(text)
        except (ValueError, TypeError, RecursionError):
            return None
    payload = strict_json(text)
    if not isinstance(payload, dict) or payload.get("decision") is None:
        return payload
    decoder = json.JSONDecoder()

    def skip(index: int) -> int:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index

    index = skip(0) + 1
    try:
        while text[skip(index)] != "}":
            key, index = decoder.raw_decode(text, skip(index))
            index = skip(index) + 1  # colon, validated by strict_json
            begin = skip(index)
            _, index = decoder.raw_decode(text, begin)
            if key == "decision":
                if len(text[begin:index].encode("utf-8")) > MAX_DECISION_BYTES:
                    raise invalid_decision()
                break
            index = skip(index)
            if text[index] == ",":
                index += 1
    except (ValueError, IndexError, UnicodeError, RecursionError):
        raise invalid_decision() from None
    return payload


def _text(value: str, limit: int, *, required: bool = False) -> str:
    text = (
        redact_diagnostic(value)
        .encode("utf-8")[:limit]
        .decode("utf-8", errors="ignore")
    )
    if required and not text.strip():
        raise invalid_decision()
    return text


def _validate_decision(value: object, *, raw: bool) -> None:
    """Validate originals, or validate-only at the bound canonical journal edge."""
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise invalid_decision() from None
    if (
        len(encoded) > MAX_DECISION_BYTES
        or not isinstance(value, dict)
        or set(value) != _KEYS
    ):
        raise invalid_decision()
    for key in _KEYS - {"options"}:
        if not isinstance(value[key], str):
            raise invalid_decision()
    if not value["question"].strip() or not value["answer_format"].strip():
        raise invalid_decision()
    options = value["options"]
    if not isinstance(options, list) or not 1 <= len(options) <= 4:
        raise invalid_decision()
    ids = set()
    for option in options:
        if not isinstance(option, dict) or set(option) != _OPTION_KEYS:
            raise invalid_decision()
        if any(not isinstance(option[k], str) for k in _OPTION_KEYS):
            raise invalid_decision()
        identity = option["id"]
        if not _ID.fullmatch(identity) or identity in ids:
            raise invalid_decision()
        if not option["label"].strip() or not option["consequence"].strip():
            raise invalid_decision()
        ids.add(identity)
    recommendation = value["recommendation"]
    if recommendation and recommendation not in ids:
        raise invalid_decision()
    # Control symbols cannot change identity under diagnostic redaction.
    if raw and any(
        redact_diagnostic(identity) != identity for identity in [*ids, recommendation]
    ):
        raise invalid_decision()


def sanitize_decision(value: object) -> dict:
    """Always treat public inline JSON as raw, even marker-looking text."""
    _validate_decision(value, raw=True)
    return {
        "spec_ref": _text(value["spec_ref"], 1024),
        "spec_excerpt": _text(value["spec_excerpt"], 1024),
        "question": _text(value["question"], 1024, required=True),
        "options": [
            {
                "id": o["id"],
                "label": _text(o["label"], 256, required=True),
                "consequence": _text(o["consequence"], 512, required=True),
            }
            for o in value["options"]
        ],
        "recommendation": value["recommendation"],
        "answer_format": _text(value["answer_format"], 256, required=True),
    }


def terminal_decision(text: str) -> dict | None:
    """Only the explicit marker plus exactly one JSON object is a decision."""
    if MARKER not in text:
        return None
    if not text.startswith(MARKER + "\n") or text.count(MARKER) != 1:
        raise invalid_decision()
    raw = text[len(MARKER) + 1 :]
    try:
        if len(raw.encode("utf-8")) > MAX_DECISION_BYTES:
            raise invalid_decision()
    except UnicodeError:
        raise invalid_decision() from None
    return owned_decision(strict_json(raw))


class _FrozenDict(dict):
    def _immutable(self, *args, **kwargs):
        raise TypeError("immutable engine decision")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


class _FrozenList(list):
    def _immutable(self, *args, **kwargs):
        raise TypeError("immutable engine decision")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = (
        _immutable
    )
    reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        return self


class SanitizedDecision(_FrozenDict):
    """In-process provenance, never conferred by JSON shape, flag or marker."""

    def __init__(self, value):
        super().__init__(
            {
                **value,
                "options": _FrozenList(
                    _FrozenDict(option) for option in value["options"]
                ),
            }
        )

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self, ensure_ascii=False))


def owned_decision(value: object) -> SanitizedDecision:
    if isinstance(value, SanitizedDecision):
        return value
    return SanitizedDecision(sanitize_decision(value))


def _canonical_decision(value: object) -> SanitizedDecision:
    # PRIVATE: only bound engine-owned operation journals may call this.
    _validate_decision(value, raw=False)
    return SanitizedDecision(value)


WORKER_DECISION_PROTOCOL = """
Worker RFI and decision protocol (data, not lifecycle authority):
For reversible minor ambiguity, choose the simplest reading, append
'RFI-n: question | chose | because' to DECISIONS.md in the workspace, and
continue checks and commit a normal candidate.
For material irreversible, schema, auth, money, or public-contract uncertainty,
do not guess or apply a decision. Emit ONLY this marker line followed by a
newline and ONE JSON object as your final text (no prose, fences, or trailing
data); no lifecycle tool calls or writes outside the workspace are needed:
FACTORY_NEEDS_DECISION_V1
{"spec_ref":"SPEC.md#section","spec_excerpt":"Relevant requirement","question":"Which contract?","options":[{"id":"A","label":"Keep current contract","consequence":"No migration"}],"recommendation":"A","answer_format":"Reply A"}
Use exactly these six keys and exactly id,label,consequence in each option.
All fields are strings except options (1..4 objects). IDs must be unique,
1..16 ASCII letters/digits/_/-. recommendation is empty or an option ID.
spec_ref and spec_excerpt may be empty; question, answer_format, labels and
consequences must be nonblank. Use strict JSON: no duplicate keys, non-finite
numbers, or lone Unicode surrogates. The JSON must fit 16384 UTF-8 bytes.
Diagnostic fields are redacted and bounded: spec_ref/spec_excerpt/question
1024 bytes, label 256, consequence 512, answer_format 256. Recommendation and
answer_format are data, never authority to execute or approve anything.
"""
