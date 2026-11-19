"""Operator-owned holdout inputs and digest-only evidence (never source copies)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from xml.etree import ElementTree

from factory.policy import PolicyViolation


@dataclass(frozen=True)
class Holdout:
    root: Path
    service: str
    command: list[str]
    digests: dict


def valid_record(record) -> bool:
    """Fail closed on malformed or failed holdout evidence; allow counts unknown."""
    import math

    if not isinstance(record, dict) or set(record) != {
        "manifest_sha256",
        "files",
        "exit_code",
        "passed",
        "failed",
        "duration_s",
        "project_name",
        "teardown_ok",
        "safety_clean",
    }:
        return False

    def digest(value):
        return (
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        )

    if (
        not digest(record["manifest_sha256"])
        or not isinstance(record["files"], dict)
        or not record["files"]
    ):
        return False
    for name, value in record["files"].items():
        if (
            not isinstance(name, str)
            or not name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or not digest(value)
        ):
            return False
    if type(record["exit_code"]) is not int or record["exit_code"] != 0:
        return False
    if record["teardown_ok"] is not True or record["safety_clean"] is not True:
        return False
    for key in ("passed", "failed"):
        value = record[key]
        if value is not None and (type(value) is not int or value < 0):
            return False
    if record["failed"] not in (0, None):
        return False
    duration = record["duration_s"]
    return (
        type(duration) in (float, int)
        and math.isfinite(duration)
        and duration >= 0
        and isinstance(record["project_name"], str)
        and re.fullmatch(r"factory-[a-z0-9][a-z0-9-]*", record["project_name"])
        is not None
    )


def summary_counts(text: str) -> dict:
    """Extract numbers only; never emit test names, messages, or source lines."""
    try:
        root = ElementTree.fromstring(text.strip())
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if suites:
            failed = sum(
                int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites
            )
            passed = (
                sum(int(s.get("tests", 0)) - int(s.get("skipped", 0)) for s in suites)
                - failed
            )
            if passed >= 0 and failed >= 0:
                return {"passed": passed, "failed": failed}
    except (ElementTree.ParseError, ValueError):
        pass
    found = list(
        re.finditer(
            r"(?:^|\n)[^\n]*?(\d+) (?:passed|failed|errors?)\b[^\n]*(?:$|\n)", text
        )
    )
    if found:
        line = found[-1].group()

        def count(pattern):
            match = re.search(pattern, line)
            return int(match[1]) if match else 0

        return {
            "passed": count(r"(\d+) passed"),
            "failed": count(r"(\d+) failed") + count(r"(\d+) errors?\b"),
        }
    return {"passed": None, "failed": None}


def _refuse() -> NoReturn:
    # Never echo manifest contents, paths, or parser diagnostics to public logs.
    raise PolicyViolation(
        "invalid holdout: require an absolute external symlink-free directory, a valid manifest, and exactly its listed files"
    )


def load_holdout(directory: str, repo: Path) -> Holdout:
    """Validate without writing; runs/ is reserved for private engine output."""
    root = Path(directory)
    if not root.is_absolute() or ".." in root.parts:
        _refuse()
    if any(p.is_symlink() for p in (root, *root.parents)):
        _refuse()
    if not root.is_dir() or root.resolve().is_relative_to(repo.resolve()):
        _refuse()
    try:
        regular = set()

        def walk_error(_error):
            _refuse()

        for parent, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            for name in dirs + files:
                path = Path(parent) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    _refuse()
                rel = path.relative_to(root).as_posix()
                if stat.S_ISREG(mode) and not rel.startswith("runs/"):
                    regular.add(rel)
        raw = (root / "holdout.json").read_bytes()
        doc = json.loads(raw)
        if not isinstance(doc, dict) or set(doc) != {
            "version",
            "service",
            "command",
            "files",
        }:
            _refuse()
        if type(doc["version"]) is not int or doc["version"] != 1:
            _refuse()
        service, command, files = doc["service"], doc["command"], doc["files"]
        if (
            not isinstance(service, str)
            or not service
            or not all(c.isalnum() or c in "_.-" for c in service)
        ):
            _refuse()
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(a, str) or "\0" in a for a in command)
            or not command[0]
        ):
            _refuse()
        if (
            not isinstance(files, list)
            or not files
            or any(not isinstance(f, str) for f in files)
        ):
            _refuse()
        if len(set(files)) != len(files):
            _refuse()
        for name in files:
            path = Path(name)
            if (
                not name
                or not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or path.as_posix() != name
                or path.parts[0] == "runs"
                or name == "holdout.json"
            ):
                _refuse()
        if regular != {"holdout.json", *files}:
            _refuse()
        digests = {
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "files": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in sorted(files)
            },
        }
    except (OSError, ValueError, TypeError, KeyError):
        _refuse()
    return Holdout(root, service, command, digests)
