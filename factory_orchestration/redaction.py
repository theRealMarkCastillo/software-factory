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
    """Redact exact env values (longest first), then the fixed token shapes.

    Read the live process environment on each call, without logging it. Provider
    name sets are imported lazily to avoid a dependency cycle through errors.
    A single substitution pass prevents replacements from changing other markers.
    """
    from .adapters.hermes import PROVIDER_ENV, REVIEW_PROVIDER_KEYS

    provider_names = PROVIDER_ENV | {
        name for names in REVIEW_PROVIDER_KEYS.values() for name in names
    }
    values = {}
    for name, value in sorted(os.environ.items()):
        if len(value) >= 8 and (_ENV_NAME.search(name) or name in provider_names):
            values.setdefault(value, name)
    if values:
        pattern = re.compile(
            "|".join(
                re.escape(value) for value in sorted(values, key=len, reverse=True)
            )
        )
        text = pattern.sub(lambda match: f"[REDACTED:{values[match.group()]}]", text)
    for label, pattern in _SHAPES:
        text = pattern.sub(
            lambda match, label=label: (
                (match.group(1) if label == "bearer" else "") + f"[REDACTED:{label}]"
            ),
            text,
        )
    if cut_window:
        # Complete PEM blocks are already gone. An END line left in a cut
        # window therefore belongs to a BEGIN outside the readable window.
        orphan_ends = list(
            re.finditer(r"^-----END [A-Z ]*PRIVATE KEY-----\r?$", text, re.M)
        )
        if orphan_ends:
            text = "[REDACTED:pem]" + text[orphan_ends[-1].end() :]
    return text
