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


def redact_diagnostic(text: str, *, cut_window: bool = False) -> str:
    """Redact exact env values and the fixed token shapes in one pass.

    Every rule is matched against the ORIGINAL text; the matched spans are then
    merged and replaced together. No rule ever sees another rule's output, so a
    replacement cannot hide a shape (for example an environment value that
    occurs inside a PEM delimiter) or leave a fragment of an overlapping match.
    The live process environment is read on each call and never logged.
    Provider name sets are imported lazily to avoid a dependency cycle.
    """
    from .adapters.hermes import PROVIDER_ENV, REVIEW_PROVIDER_KEYS

    provider_names = PROVIDER_ENV | {
        name for names in REVIEW_PROVIDER_KEYS.values() for name in names
    }
    values = {}
    for name, value in sorted(os.environ.items()):
        if len(value) >= 8 and (_ENV_NAME.search(name) or name in provider_names):
            values.setdefault(value, name)

    # (start, end, label); earlier entries win the label of a merged span.
    spans: list[tuple[int, int, str]] = []
    pem_spans = []
    for label, pattern in _SHAPES:
        for match in pattern.finditer(text):
            begin = match.end(1) if label == "bearer" else match.start()
            spans.append((begin, match.end(), label))
            if label == "pem":
                pem_spans.append((begin, match.end()))
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
            spans.insert(0, (0, max(orphan_ends), "pem"))
    if values:
        pattern = re.compile(
            "|".join(
                re.escape(value) for value in sorted(values, key=len, reverse=True)
            )
        )
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), values[match.group()]))
    if not spans:
        return text

    order = {id(span): index for index, span in enumerate(spans)}
    merged: list[list] = []
    for span in sorted(spans, key=lambda item: (item[0], -item[1])):
        if merged and span[0] < merged[-1][1]:
            last = merged[-1]
            last[1] = max(last[1], span[1])
            if order[id(span)] < last[3]:
                last[2], last[3] = span[2], order[id(span)]
        else:
            merged.append([span[0], span[1], span[2], order[id(span)]])
    parts, cursor = [], 0
    for begin, end, label, _ in merged:
        parts.append(text[cursor:begin])
        parts.append(f"[REDACTED:{label}]")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
