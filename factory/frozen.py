"""Shared, read-only frozen-path validation and candidate matching.

Entries are literal repository-relative POSIX file names or directory prefixes,
not pathspecs. Matching deliberately folds Unicode normalization and case.
"""

from __future__ import annotations

import json
import os
import subprocess
import unicodedata
from pathlib import Path


def path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def validate_paths(paths: tuple[str, ...]) -> None:
    """Validate declaration syntax without consulting a repository."""
    if not isinstance(paths, tuple) or len(paths) > 200:
        raise ValueError("frozen_paths must be a tuple of at most 200 paths")
    seen = set()
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or any(char in path for char in "\\*?[]")
            or any(unicodedata.category(char) == "Cc" for char in path)
            or any(
                part in {"", ".", ".."} for part in path.removesuffix("/").split("/")
            )
        ):
            raise ValueError(
                "frozen_paths entries must be literal repository-relative POSIX paths"
            )
        key = path_key(path)
        if key in seen:
            raise ValueError("frozen_paths contains duplicate normalized paths")
        seen.add(key)


def _git(repository: str | Path, *args: str) -> bytes:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "--no-optional-locks",
            *args,
        ],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("frozen_paths Git object read failed")
    return result.stdout


def validate_base(repository: str | Path, base: str, paths: tuple[str, ...]) -> None:
    """Require literal blobs/files beneath directory entries at the base."""
    if not paths:
        return
    blobs = set()
    for record in _git(repository, "ls-tree", "-r", "-z", base).split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        if metadata.split()[1] == b"blob":
            blobs.add(os.fsdecode(name))
    for path in paths:
        exists = (
            any(name.startswith(path) for name in blobs)
            if path.endswith("/")
            else path in blobs
        )
        if not exists:
            raise ValueError("frozen_paths entry has no matching base blob")


def offending_paths(
    repository: str | Path, base: str, candidate: str, paths: tuple[str, ...]
) -> list[str]:
    """Count every diff path, including both ends of a rename, as a change."""
    if not paths:
        return []
    changed = _git(
        repository,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        base,
        candidate,
    ).split(b"\0")
    files = {path_key(path) for path in paths if not path.endswith("/")}
    directories = tuple(path_key(path) for path in paths if path.endswith("/"))
    return sorted(
        name
        for raw in changed
        if raw
        for name in [os.fsdecode(raw)]
        if path_key(name) in files or path_key(name).startswith(directories)
    )


def violation_detail(paths: list[str]) -> str:
    """JSON array, first 20 sorted paths and an omitted count when truncated."""
    entries: list = sorted(paths)[:20]
    if len(paths) > 20:
        entries.append({"omitted_count": len(paths) - 20})
    return json.dumps(entries)
