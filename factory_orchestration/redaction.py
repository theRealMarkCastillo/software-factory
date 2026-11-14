"""Defence-in-depth redaction for free-text orchestration diagnostics.

This deliberately recognizes only the owner's exact environment and shape rules.
It is not a general-purpose credential detector or an encoding decoder.
"""

from __future__ import annotations

import os
import re

_ENV_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)
_SHAPES = (
    (
        "pem",
        re.compile(
            r"-----BEGIN (?P<kind>[A-Z ]*PRIVATE KEY)-----.*?(?:-----END (?P=kind)-----(?=\r?$)|\Z)",
            re.S | re.M,
        ),
    ),
    ("openai", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}")),
    ("aws", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("slack", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("bearer", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
)


def _redaction_spans(text: str, *, cut_window: bool = False) -> list[tuple]:
    """Disjoint, sorted ``(start, end, label)`` spans to replace in ``text``.

    Every rule is matched against the ORIGINAL text and the matches are merged
    into a span union. No rule ever sees another rule's output, so a
    replacement cannot hide a shape (an environment value inside a PEM
    delimiter) and overlapping matches cannot leave a fragment. Environment
    values are located one value at a time, including overlapping occurrences
    of the same or of different values; shapes are located at every start
    offset, including matches nested in or crossing another match.
    """
    from .adapters.hermes import PROVIDER_ENV, REVIEW_PROVIDER_KEYS

    provider_names = PROVIDER_ENV | {
        name for names in REVIEW_PROVIDER_KEYS.values() for name in names
    }
    values = {}
    for name, value in sorted(os.environ.items()):
        if len(value) >= 8 and (_ENV_NAME.search(name) or name in provider_names):
            values.setdefault(value, name)

    # Earlier entries win the label of a merged span.
    found: list[tuple[int, int, str]] = []
    pem_spans = []
    for label, pattern in _SHAPES:
        # Every start offset that matches contributes, not only the
        # non-overlapping matches ``finditer`` yields: a block nested inside or
        # crossing another one is still a match of the original text.
        position = 0
        while position <= len(text):
            match = pattern.search(text, position)
            if match is None:
                break
            begin = match.end(1) if label == "bearer" else match.start()
            found.append((begin, match.end(), label))
            if label == "pem":
                pem_spans.append((begin, match.end()))
            if match.end() >= len(text):
                break  # later starts of this shape lie inside this span
            position = match.start() + 1
    if cut_window:
        # An END line outside every complete block belongs to a BEGIN that lies
        # before the readable window: everything up to it is key material.
        orphan_ends = [
            match.end()
            for match in re.finditer(
                r"^-----END [A-Z ]*PRIVATE KEY-----\r?$", text, re.M
            )
            if not any(a <= match.start() < b for a, b in pem_spans)
        ]
        if orphan_ends:
            found.insert(0, (0, max(orphan_ends), "pem"))
    for value in sorted(values, key=len, reverse=True):
        begin = text.find(value)
        while begin != -1:
            found.append((begin, begin + len(value), values[value]))
            begin = text.find(value, begin + 1)

    merged: list[list] = []
    ranked = sorted(enumerate(found), key=lambda item: (item[1][0], -item[1][1]))
    for rank, (begin, end, label) in ranked:
        if merged and begin < merged[-1][1]:
            last = merged[-1]
            last[1] = max(last[1], end)
            if rank < last[3]:
                last[2], last[3] = label, rank
        else:
            merged.append([begin, end, label, rank])
    return [(begin, end, label) for begin, end, label, _ in merged]


def redact_diagnostic(text: str, *, cut_window: bool = False) -> str:
    """Replace exact env values and the fixed token shapes in one pass.

    The live process environment is read on each call and never logged.
    """
    parts, cursor = [], 0
    for begin, end, label in _redaction_spans(text, cut_window=cut_window):
        parts.append(text[cursor:begin])
        parts.append(f"[REDACTED:{label}]")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
