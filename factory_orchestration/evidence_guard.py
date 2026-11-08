"""Manifest byte guards applied before a verifier is trusted.

The historical verifier defect let duplicate JSON keys turn diagnostic evidence
into acceptance. The engine verifier hashes raw bytes, so a duplicate-key
manifest can still be self-consistent; this guard rejects the ambiguity at the
adapter boundary instead of interpreting it.
"""

from __future__ import annotations

import json
from pathlib import Path


def duplicate_json_keys(path: str | Path) -> list[str]:
    """Return dotted paths of every duplicated object key in a JSON file."""

    duplicates: list[str] = []

    def hook(pairs):
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                duplicates.append(str(key))
            seen.add(key)
        return dict(pairs)

    try:
        text = Path(path).read_text()
    except OSError:
        return []
    try:
        json.loads(text, object_pairs_hook=hook)
    except ValueError:
        return []
    return sorted(set(duplicates))


def manifest_guard(artifacts: str | Path) -> tuple[bool, str]:
    """``(ok, reason)`` for the manifest bytes before verification."""

    manifest = Path(artifacts) / "evidence-manifest.json"
    if not manifest.is_file():
        return False, "manifest_missing"
    if manifest.is_symlink():
        return False, "manifest_symlink"
    duplicates = duplicate_json_keys(manifest)
    if duplicates:
        return False, "duplicate_json_keys:" + ",".join(duplicates)
    return True, "ok"
