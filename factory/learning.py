"""Provenance-checked incident intake and inert improvement proposals.

``factory-learning import`` accepts one explicitly supplied, schema-versioned
failure record, verifies every cited evidence artifact under an explicit
evidence root, and stores an immutable record under a fingerprint/content-id
pair. ``factory-learning propose`` renders a deterministic, non-authorizing
proposal from such a record.

The module is deliberately small and inert: it reads local files, writes only
under ``--store``, spawns no subprocess, opens no socket and resolves no URL.
Incident prose is data. Nothing in a record is executed, dispatched, or applied
to a skill, test, gate, task or policy.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.evidence import FULL_SHA_RE, parse_strict_json, sha256_file
from factory.policy import SENSITIVE_SYSTEM_ROOTS

SCHEMA_NAME = "factory-incident"
SCHEMA_VERSION = 1
PROPOSAL_SCHEMA_NAME = "factory-proposal"
PROPOSAL_SCHEMA_VERSION = 1

INCIDENT_DIR = "incidents"
PROPOSAL_DIR = "proposals"
LOCK_DIR = "locks"

RECORD_MAX_BYTES = 256 * 1024
EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE = 64
TEXT_MAX_CHARS = 4096

CATEGORIES = (
    "implementation",
    "specification",
    "tooling_evidence",
    "infrastructure",
    "unknown",
)
IMPROVEMENT_CLASSES = (
    "regression_test",
    "spec_clarification",
    "tooling_check",
    "infra_change",
)
ROOT_CAUSE_STATUSES = ("unknown", "supported")
EVIDENCE_KINDS = ("probe", "log", "report")
ORIGIN_KINDS = ("reviewer", "operator", "machine")
OUTCOMES = (
    "pass",
    "assertion_failure",
    "error_failure",
    "collection_error",
    "no_tests",
    "interrupted",
    "timeout",
)
PROPOSAL_STATUS = "pending_review"
AUTHORIZATION_NOTE = "no promotion, dispatch, deployment or policy change"

REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

RECORD_KEYS = frozenset(
    {
        "schema",
        "fingerprint",
        "content_id",
        "title",
        "symptom",
        "category",
        "root_cause",
        "sources",
        "evidence",
        "hypothesis",
        "improvement_class",
        "replay",
        "corrects",
        "origin",
    }
)
REQUIRED_KEYS = frozenset(
    {
        "schema",
        "title",
        "symptom",
        "category",
        "root_cause",
        "sources",
        "evidence",
        "hypothesis",
        "improvement_class",
        "origin",
    }
)
SYMPTOM_KEYS = frozenset({"summary", "observed"})
ROOT_CAUSE_KEYS = frozenset({"status", "statement", "evidence_refs"})
SOURCES_KEYS = frozenset({"repository", "revisions", "repaired_revision"})
EVIDENCE_KEYS = frozenset({"ref", "kind", "bytes", "sha256"})
REPLAY_KEYS = frozenset(
    {"suite_id", "target_nodeid", "expect_baseline", "expect_candidate"}
)
ORIGIN_KEYS = frozenset({"kind", "ref"})

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class _Rejected(Exception):
    """Internal: a record, path or store is not admissible."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class LearningResult:
    """One import/propose outcome; never raises for a rejected input."""

    ok: bool
    action: str
    reason: str | None = None
    detail: str = ""
    fingerprint: str | None = None
    content_id: str | None = None
    path: str | None = None
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        proposal = self.payload.get("proposal") if self.payload else None
        return {
            "ok": self.ok,
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
            "content_id": self.content_id,
            "path": self.path,
            **({"status": proposal["status"]} if proposal else {}),
            **({"payload": self.payload} if self.payload else {}),
        }


# ---------------------------------------------------------------------------
# canonical identity
# ---------------------------------------------------------------------------


def canonical_json_bytes(value: object) -> bytes:
    """Stable UTF-8 JSON used for every digest in this module."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def normalized_summary(record: dict) -> str:
    return " ".join(record["symptom"]["summary"].split()).casefold()


def compute_fingerprint(record: dict) -> str:
    """Identity over symptom, revisions and evidence bytes -- never wording."""
    return _digest(
        {
            "symptom_summary": normalized_summary(record),
            "revisions": sorted(set(record["sources"]["revisions"])),
            "evidence_sha256": sorted(entry["sha256"] for entry in record["evidence"]),
        }
    )


def compute_content_id(record: dict) -> str:
    """Identity of one exact record, excluding the two computed id fields."""
    body = {
        key: value
        for key, value in record.items()
        if key not in ("fingerprint", "content_id")
    }
    return _digest(body)


# ---------------------------------------------------------------------------
# record validation
# ---------------------------------------------------------------------------


def _text(value: object, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise _Rejected("malformed_record", f"{label} must be a string")
    if len(value) > TEXT_MAX_CHARS:
        raise _Rejected("malformed_record", f"{label} exceeds {TEXT_MAX_CHARS} chars")
    if _CONTROL_CHARS.search(value):
        raise _Rejected("malformed_record", f"{label} contains control characters")
    return value


def _object(value: object, label: str, keys: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        raise _Rejected("malformed_record", f"{label} must be an object")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise _Rejected("malformed_record", f"{label} has unknown key(s) {unknown[:5]}")
    return value


def _require_keys(value: dict, keys: set[str], label: str) -> None:
    missing = sorted(keys - set(value))
    if missing:
        raise _Rejected("malformed_record", f"{label} is missing key(s) {missing[:5]}")


def _sha_field(value: object, label: str) -> str:
    if not isinstance(value, str) or not FINGERPRINT_RE.match(value):
        raise _Rejected("malformed_record", f"{label} must be sha256:<64 hex>")
    return value


def _admit_evidence_root(evidence_root: Path) -> Path:
    root = Path(evidence_root).expanduser()
    if root.is_symlink():
        raise _Rejected("path_escape", f"--evidence-root is a symlink: {root}")
    root = root.resolve()
    if not root.is_dir():
        raise _Rejected("evidence_root_invalid", f"no evidence root: {root}")
    return root


def _check_relative_ref(ref: object) -> str:
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise _Rejected(
            "malformed_record", f"evidence ref is not a bounded path: {ref!r}"
        )
    if ref.startswith("/") or "\\" in ref:
        raise _Rejected("path_escape", f"evidence ref is not relative POSIX: {ref!r}")
    parts = ref.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _Rejected(
            "path_escape", f"evidence ref has an empty or '..' component: {ref!r}"
        )
    if ":" in ref:
        raise _Rejected("path_escape", f"evidence ref carries a scheme: {ref!r}")
    return ref


def _resolve_evidence(root: Path, ref: str) -> Path:
    candidate = root / ref
    probe = root
    for part in ref.split("/"):
        probe = probe / part
        if probe.is_symlink():
            raise _Rejected("path_escape", f"evidence ref crosses a symlink: {ref!r}")
    resolved = candidate.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise _Rejected("path_escape", f"evidence ref escapes the root: {ref!r}")
    return candidate


def _verify_evidence_entry(entry: object, root: Path) -> dict:
    entry = _object(entry, "evidence entry", EVIDENCE_KEYS)
    _require_keys(entry, EVIDENCE_KEYS, "evidence entry")
    ref = _check_relative_ref(entry["ref"])
    kind = entry["kind"]
    if kind not in EVIDENCE_KINDS:
        raise _Rejected("malformed_record", f"unknown evidence kind: {kind!r}")
    size = entry["bytes"]
    if type(size) is not int or size < 0 or size > EVIDENCE_MAX_BYTES:
        raise _Rejected("malformed_record", "evidence bytes is out of bounds")
    declared = _sha_field(entry["sha256"], "evidence sha256")

    path = _resolve_evidence(root, ref)
    if not path.is_file():
        raise _Rejected("evidence_missing", f"evidence file does not exist: {ref}")
    actual_size = path.stat().st_size
    if actual_size > EVIDENCE_MAX_BYTES:
        raise _Rejected("evidence_too_large", f"evidence file exceeds 8 MiB: {ref}")
    if actual_size != size:
        raise _Rejected(
            "evidence_hash_mismatch",
            f"evidence size changed for {ref}: {actual_size} != {size}",
        )
    actual = "sha256:" + sha256_file(path)
    if actual != declared:
        raise _Rejected("evidence_hash_mismatch", f"evidence hash changed for {ref}")
    return {**entry, "ref": ref, "sha256": declared}


def _validate_replay(value: object) -> dict | None:
    if value is None:
        return None
    replay = _object(value, "replay", REPLAY_KEYS)
    _require_keys(replay, REPLAY_KEYS, "replay")
    suite_id = _text(replay["suite_id"], "replay.suite_id")
    if not suite_id:
        raise _Rejected("malformed_record", "replay.suite_id is empty")
    target = _text(replay["target_nodeid"], "replay.target_nodeid")
    if not target or "::" not in target:
        raise _Rejected(
            "malformed_record", "replay.target_nodeid must be a pytest node id"
        )
    for field_name in ("expect_baseline", "expect_candidate"):
        outcome = replay[field_name]
        if outcome not in OUTCOMES:
            raise _Rejected(
                "malformed_record", f"replay.{field_name} is not a known outcome"
            )
    if replay["expect_baseline"] == replay["expect_candidate"]:
        raise _Rejected(
            "malformed_record", "replay expectations must differ between revisions"
        )
    return {
        "suite_id": suite_id,
        "target_nodeid": target,
        "expect_baseline": replay["expect_baseline"],
        "expect_candidate": replay["expect_candidate"],
    }


def validate_record(record: object, evidence_root: Path) -> dict:
    """Validate one incident record and every cited artifact; return a copy."""
    record = _object(record, "record", RECORD_KEYS)
    _require_keys(record, REQUIRED_KEYS, "record")

    schema = record["schema"]
    if not isinstance(schema, dict) or set(schema) != {"name", "version"}:
        raise _Rejected("malformed_record", "schema must be {name, version}")
    if schema["name"] != SCHEMA_NAME or schema["version"] != SCHEMA_VERSION:
        raise _Rejected(
            "malformed_record",
            f"unsupported schema {schema['name']!r} v{schema['version']!r}",
        )

    title = _text(record["title"], "title")
    if not title:
        raise _Rejected("malformed_record", "title is empty")
    hypothesis = _text(record["hypothesis"], "hypothesis")
    if not hypothesis:
        raise _Rejected("malformed_record", "hypothesis is empty")

    symptom = _object(record["symptom"], "symptom", SYMPTOM_KEYS)
    _require_keys(symptom, SYMPTOM_KEYS, "symptom")
    summary = _text(symptom["summary"], "symptom.summary")
    observed = _text(symptom["observed"], "symptom.observed")
    if not summary.strip() or not observed.strip():
        raise _Rejected("malformed_record", "symptom summary/observed is empty")

    category = record["category"]
    if not isinstance(category, str) or category not in CATEGORIES:
        raise _Rejected("invalid_category", f"unknown category: {category!r}")

    improvement = record["improvement_class"]
    if not isinstance(improvement, str) or improvement not in IMPROVEMENT_CLASSES:
        raise _Rejected(
            "invalid_improvement_class",
            f"unknown improvement_class: {improvement!r}",
        )

    root_cause = _object(record["root_cause"], "root_cause", ROOT_CAUSE_KEYS)
    _require_keys(root_cause, ROOT_CAUSE_KEYS, "root_cause")
    status = root_cause["status"]
    if status not in ROOT_CAUSE_STATUSES:
        raise _Rejected("invalid_root_cause", f"unknown root_cause.status: {status!r}")
    statement = _text(root_cause["statement"], "root_cause.statement", allow_none=True)
    refs = root_cause["evidence_refs"]
    if not isinstance(refs, list) or not all(type(i) is int for i in refs):
        raise _Rejected("invalid_root_cause", "evidence_refs must be integers")
    if status == "unknown" and (statement is not None or refs):
        raise _Rejected(
            "invalid_root_cause",
            "root_cause.status 'unknown' cannot carry a statement or refs",
        )
    if status == "supported" and (not statement or not refs):
        raise _Rejected(
            "invalid_root_cause",
            "root_cause.status 'supported' needs a statement and evidence refs",
        )

    sources = _object(record["sources"], "sources", SOURCES_KEYS)
    _require_keys(sources, SOURCES_KEYS, "sources")
    repository = _text(sources["repository"], "sources.repository")
    if not repository:
        raise _Rejected("malformed_record", "sources.repository is empty")
    revisions = sources["revisions"]
    if (
        not isinstance(revisions, list)
        or not revisions
        or len(revisions) > 8
        or not all(isinstance(rev, str) and FULL_SHA_RE.match(rev) for rev in revisions)
    ):
        raise _Rejected(
            "malformed_record", "sources.revisions must be 1-8 full 40-hex SHAs"
        )
    repaired = sources["repaired_revision"]
    if repaired is not None and (
        not isinstance(repaired, str) or not FULL_SHA_RE.match(repaired)
    ):
        raise _Rejected(
            "malformed_record", "sources.repaired_revision must be null or 40-hex"
        )

    evidence = record["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE:
        raise _Rejected(
            "malformed_record", f"evidence must hold 1-{MAX_EVIDENCE} entries"
        )
    checked = [_verify_evidence_entry(entry, evidence_root) for entry in evidence]
    if len({entry["ref"] for entry in checked}) != len(checked):
        raise _Rejected("malformed_record", "evidence repeats a ref")
    if any(ref >= len(checked) for ref in refs):
        raise _Rejected(
            "invalid_root_cause", "evidence_refs point outside the evidence list"
        )

    origin = _object(record["origin"], "origin", ORIGIN_KEYS)
    _require_keys(origin, ORIGIN_KEYS, "origin")
    if origin["kind"] not in ORIGIN_KINDS:
        raise _Rejected("malformed_record", f"unknown origin.kind: {origin['kind']!r}")
    origin_ref = _text(origin["ref"], "origin.ref")
    if not origin_ref:
        raise _Rejected("malformed_record", "origin.ref is empty")

    corrects = record.get("corrects")
    if corrects is not None:
        corrects = _sha_field(corrects, "corrects")

    supplied_fingerprint = record.get("fingerprint")
    supplied_content = record.get("content_id")
    if supplied_fingerprint is not None:
        _sha_field(supplied_fingerprint, "fingerprint")
    if supplied_content is not None:
        _sha_field(supplied_content, "content_id")

    validated = {
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
        "title": title,
        "symptom": {"summary": summary, "observed": observed},
        "category": category,
        "root_cause": {
            "status": status,
            "statement": statement,
            "evidence_refs": list(refs),
        },
        "sources": {
            "repository": repository,
            "revisions": list(revisions),
            "repaired_revision": repaired,
        },
        "evidence": checked,
        "hypothesis": hypothesis,
        "improvement_class": improvement,
        "replay": _validate_replay(record.get("replay")),
        "corrects": corrects,
        "origin": {"kind": origin["kind"], "ref": origin_ref},
    }

    fingerprint = compute_fingerprint(validated)
    content_id = compute_content_id(validated)
    if supplied_fingerprint is not None and supplied_fingerprint != fingerprint:
        raise _Rejected(
            "invalid_incident_id", "supplied fingerprint does not match the content"
        )
    if supplied_content is not None and supplied_content != content_id:
        raise _Rejected(
            "invalid_incident_id", "supplied content_id does not match the content"
        )
    validated["fingerprint"] = fingerprint
    validated["content_id"] = content_id
    return validated


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def _admit_store(
    store: Path | str, evidence_root: Path | None = None, *, create: bool = True
) -> Path:
    """Resolve and admit a store root, refusing anything unsafe or unreadable.

    Filesystem probes are fail-closed: an inaccessible path (for example
    ``/root/factory-store`` for an unprivileged CI runner) is refused with a
    structured ``store_invalid`` instead of leaking ``PermissionError``. The
    authoritative location checks below run on the resolved path, so refusing an
    unreadable path never weakens the sensitive-root protections.
    """
    raw = Path(store).expanduser()
    try:
        if raw.is_symlink():
            raise _Rejected("store_invalid", f"store is a symlink: {raw}")
        root = raw.resolve()
    except OSError as exc:
        raise _Rejected(
            "store_invalid",
            f"store path is not accessible: {raw} ({exc.strerror or exc})",
        ) from exc
    _assert_store_location(root)
    if evidence_root is not None:
        _assert_disjoint(evidence_root, root)
    try:
        if root.exists() and not root.is_dir():
            raise _Rejected("store_invalid", f"store is not a directory: {root}")
        if create:
            root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _Rejected(
            "store_invalid",
            f"store path is not accessible: {root} ({exc.strerror or exc})",
        ) from exc
    return root


def _assert_store_location(root: Path) -> None:
    """Refuse the host root, system roots and the operator home itself.

    Ordinary owned subdirectories -- including below ``/Users`` or ``/home`` --
    are allowed, matching the factory's own safe-root semantics. This is not a
    general secret scan: it rejects the paths that must never hold a store.
    """
    home = Path.home().resolve()
    if root == Path("/"):
        raise _Rejected("store_invalid", "store must not be the host root")
    if root == home:
        raise _Rejected("store_invalid", "store must not be the home directory itself")
    for prefix in ("/Users", "/home"):
        base = Path(prefix)
        if root != base and root.is_relative_to(base):
            if len(root.relative_to(base).parts) == 1:
                raise _Rejected(
                    "store_invalid",
                    f"store must be nested below the home directory, not {root}",
                )
    for sensitive in SENSITIVE_SYSTEM_ROOTS:
        base = Path(sensitive)
        if root == base or root.is_relative_to(base):
            raise _Rejected(
                "store_invalid", f"store is inside sensitive host path {sensitive}"
            )


def _assert_disjoint(evidence_root: Path, store_root: Path) -> None:
    if (
        store_root == evidence_root
        or store_root.is_relative_to(evidence_root)
        or evidence_root.is_relative_to(store_root)
    ):
        raise _Rejected(
            "store_invalid",
            "store and evidence root must not contain one another",
        )


def _fingerprint_hex(fingerprint: str) -> str:
    return fingerprint.split(":", 1)[1]


def _record_dir(store: Path, fingerprint: str) -> Path:
    return store / INCIDENT_DIR / _fingerprint_hex(fingerprint)


@contextlib.contextmanager
def _fingerprint_lock(store: Path, fingerprint: str):
    """Exclusive, blocking coordination per fingerprint; never age-stolen."""
    lock_dir = store / LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{_fingerprint_hex(fingerprint)}.lock"
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_strict(path: Path) -> object:
    raw = path.read_bytes()
    return parse_strict_json(raw)


def _existing_records(store: Path, fingerprint: str) -> dict[str, dict]:
    directory = _record_dir(store, fingerprint)
    incidents = store / INCIDENT_DIR
    if incidents.is_symlink():
        raise _Rejected(
            "store_tampered", f"incidents directory is a symlink: {incidents}"
        )
    if directory.is_symlink():
        raise _Rejected("store_tampered", f"record directory is a symlink: {directory}")
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise _Rejected(
            "store_tampered", f"record path is not a directory: {directory}"
        )
    records: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            raise _Rejected("store_tampered", f"stored record is a symlink: {path}")
        record = _read_strict(path)
        if not isinstance(record, dict):
            raise _Rejected("store_tampered", f"stored record is not an object: {path}")
        if record.get("fingerprint") != fingerprint:
            raise _Rejected(
                "store_tampered", f"stored fingerprint does not match its path: {path}"
            )
        if record.get("content_id") != "sha256:" + path.stem:
            raise _Rejected(
                "store_tampered", f"stored content_id does not match its name: {path}"
            )
        records[record["content_id"]] = record
    return records


def _write_exclusive(path: Path, payload: dict) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    try:
        os.write(descriptor, data.encode("utf-8"))
    finally:
        os.close(descriptor)


def _reject(
    reason: str, detail: str, fingerprint=None, content_id=None
) -> LearningResult:
    return LearningResult(
        False,
        "rejected",
        reason=reason,
        detail=detail,
        fingerprint=fingerprint,
        content_id=content_id,
    )


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def import_incident(
    record_path: str | Path, *, evidence_root: str | Path, store: str | Path
) -> LearningResult:
    """Verify and store one incident record; never rewrite existing history."""
    path = Path(record_path).expanduser()
    if path.is_symlink():
        return _reject("record_invalid", f"record is a symlink: {path}")
    if not path.is_file():
        return _reject("record_invalid", f"no record file: {path}")
    if path.stat().st_size > RECORD_MAX_BYTES:
        return _reject("record_too_large", f"record exceeds {RECORD_MAX_BYTES} bytes")
    raw = path.read_bytes()

    try:
        record = parse_strict_json(raw)
    except ValueError as exc:
        return _reject("malformed_record", str(exc))
    if not isinstance(record, dict):
        return _reject("malformed_record", "record must be a JSON object")

    root = _admit_evidence_root(evidence_root)
    try:
        validated = validate_record(record, root)
        store_root = _admit_store(store, evidence_root=root)
    except _Rejected as exc:
        return _reject(exc.reason, exc.detail)

    fingerprint = validated["fingerprint"]
    content_id = validated["content_id"]
    stored = {
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
        **{key: value for key, value in validated.items() if key != "schema"},
    }
    directory = _record_dir(store_root, fingerprint)
    target = directory / f"{_fingerprint_hex(content_id)}.json"

    try:
        with _fingerprint_lock(store_root, fingerprint):
            existing = _existing_records(store_root, fingerprint)
            if content_id in existing:
                return LearningResult(
                    True,
                    "duplicate",
                    fingerprint=fingerprint,
                    content_id=content_id,
                    path=str(target),
                    payload={"record": existing[content_id]},
                )
            if existing and stored.get("corrects") is None:
                return LearningResult(
                    False,
                    "conflict",
                    reason="conflict",
                    detail=(
                        "fingerprint already has different content: "
                        + ", ".join(sorted(existing))
                    ),
                    fingerprint=fingerprint,
                    content_id=content_id,
                    payload={"existing": sorted(existing)},
                )
            corrects = stored.get("corrects")
            if corrects is not None and corrects not in existing:
                raise _Rejected(
                    "invalid_correction",
                    f"corrects cites content that is not stored under this "
                    f"fingerprint: {corrects}",
                )
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise _Rejected(
                    "store_tampered", f"incident directory is a symlink: {directory}"
                )
            _write_exclusive(target, stored)
    except _Rejected as exc:
        return _reject(exc.reason, exc.detail, fingerprint, content_id)
    except FileExistsError:
        return _reject(
            "store_tampered", f"refusing to overwrite an existing record: {target}"
        )

    return LearningResult(
        True,
        "imported",
        fingerprint=fingerprint,
        content_id=content_id,
        path=str(target),
        payload={"record": stored},
    )


def list_incidents(store: str | Path) -> list[dict]:
    """All readable stored records, sorted by fingerprint and content id."""
    root = _admit_store(store, create=False)
    directory = root / INCIDENT_DIR
    if directory.is_symlink() or not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("*/*.json")):
        if path.is_symlink():
            continue
        record = _read_strict(path)
        if isinstance(record, dict):
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# proposal (never authorization)
# ---------------------------------------------------------------------------


def render_proposal(record: dict) -> dict:
    """Deterministically render one non-authorizing proposal body."""
    replay = record["replay"]
    criteria = {
        "suite_id": replay["suite_id"],
        "target_nodeid": replay["target_nodeid"],
        "expect_baseline": replay["expect_baseline"],
        "expect_candidate": replay["expect_candidate"],
        "comparison": (
            "baseline must match expect_baseline and candidate must match "
            "expect_candidate; both-passing is not improvement"
        ),
    }
    body = {
        "schema": {"name": PROPOSAL_SCHEMA_NAME, "version": PROPOSAL_SCHEMA_VERSION},
        "status": PROPOSAL_STATUS,
        "incident": {
            "fingerprint": record["fingerprint"],
            "content_id": record["content_id"],
            "title": record["title"],
            "category": record["category"],
            "improvement_class": record["improvement_class"],
        },
        "diagnosis": dict(record["root_cause"]),
        "hypothesis": record["hypothesis"],
        "executable_criteria": criteria,
        "replay_request": {
            "command": "factory-learning replay",
            "requires": ["--approve-execution"],
            "suite_id": replay["suite_id"],
            "target_nodeid": replay["target_nodeid"],
            "note": "inert request only; Stage A executes nothing",
        },
        "authorization": {"grants": [], "note": AUTHORIZATION_NOTE},
        "declared_non_actions": [
            "no task created",
            "no skill, test, gate or repository edited",
            "no promotion, dispatch, deployment or policy change",
        ],
    }
    proposal_id = "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return {"proposal_id": proposal_id, **body}


def _stored_records(store: Path) -> list[dict]:
    directory = store / INCIDENT_DIR
    if directory.is_symlink():
        raise _Rejected(
            "store_tampered", f"incidents directory is a symlink: {directory}"
        )
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("*/*.json")):
        if path.is_symlink():
            raise _Rejected("store_tampered", f"stored record is a symlink: {path}")
        record = _read_strict(path)
        if not isinstance(record, dict):
            raise _Rejected("store_tampered", f"stored record is not an object: {path}")
        records.append(record)
    return records


def _select_incident(
    records: list[dict], fingerprint: str | None, content_id: str | None
) -> dict:
    if fingerprint is not None:
        _sha_field(fingerprint, "fingerprint")
    if content_id is not None:
        _sha_field(content_id, "content_id")

    if content_id is not None:
        matches = [r for r in records if r.get("content_id") == content_id]
        if not matches:
            raise _Rejected(
                "invalid_incident_id", f"no stored incident has content id {content_id}"
            )
        if len(matches) > 1:
            raise _Rejected("store_tampered", "content id is stored more than once")
        selected = matches[0]
        if fingerprint is not None and selected.get("fingerprint") != fingerprint:
            raise _Rejected(
                "invalid_incident_id",
                "fingerprint and content id name different incidents",
            )
    elif fingerprint is not None:
        matches = [r for r in records if r.get("fingerprint") == fingerprint]
        if not matches:
            raise _Rejected(
                "invalid_incident_id",
                f"no stored incident has fingerprint {fingerprint}",
            )
        if len(matches) > 1:
            raise _Rejected(
                "ambiguous_selection",
                "fingerprint has several stored contents; pass --content-id",
            )
        selected = matches[0]
    elif not records:
        raise _Rejected("invalid_incident_id", "the store holds no incident")
    elif len(records) > 1:
        raise _Rejected(
            "ambiguous_selection",
            "several incidents are stored; pass --fingerprint or --content-id",
        )
    else:
        selected = records[0]

    # Stored content is re-validated, never trusted because it sits in the store.
    recomputed_fingerprint = compute_fingerprint(selected)
    recomputed_content = compute_content_id(selected)
    if selected.get("fingerprint") != recomputed_fingerprint:
        raise _Rejected(
            "invalid_incident_id", "stored fingerprint does not match its content"
        )
    if selected.get("content_id") != recomputed_content:
        raise _Rejected(
            "invalid_incident_id", "stored content_id does not match its content"
        )
    return selected


def propose(
    *, store: str | Path, fingerprint: str | None = None, content_id: str | None = None
) -> LearningResult:
    """Render and store one deterministic, explicitly non-authorizing proposal."""
    try:
        store_root = _admit_store(store, create=False)
        records = _stored_records(store_root)
        selected = _select_incident(records, fingerprint, content_id)
        if selected.get("replay") is None:
            raise _Rejected(
                "no_executable_criterion",
                "the incident has no replay block; a proposal needs an executable "
                "criterion",
            )
        proposal = render_proposal(selected)
    except _Rejected as exc:
        return _reject(exc.reason, exc.detail, fingerprint, content_id)

    proposal_id = proposal["proposal_id"]
    directory = store_root / PROPOSAL_DIR
    target = directory / f"{proposal_id.split(':', 1)[1]}.json"
    try:
        with _fingerprint_lock(store_root, proposal_id):
            if target.exists():
                existing = _read_strict(target)
                if existing == proposal:
                    return LearningResult(
                        True,
                        "duplicate",
                        fingerprint=selected["fingerprint"],
                        content_id=selected["content_id"],
                        path=str(target),
                        payload={"proposal": existing},
                    )
                raise _Rejected(
                    "store_tampered",
                    f"an unrelated proposal already occupies {target}",
                )
            if directory.is_symlink():
                raise _Rejected(
                    "store_tampered", f"proposals directory is a symlink: {directory}"
                )
            directory.mkdir(parents=True, exist_ok=True)
            _write_exclusive(target, proposal)
    except _Rejected as exc:
        return _reject(
            exc.reason,
            exc.detail,
            selected.get("fingerprint"),
            selected.get("content_id"),
        )
    except FileExistsError:
        return _reject("store_tampered", f"refusing to overwrite {target}")

    return LearningResult(
        True,
        "proposed",
        fingerprint=selected["fingerprint"],
        content_id=selected["content_id"],
        path=str(target),
        payload={"proposal": proposal},
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-learning",
        description=(
            "Import cited factory incidents as data and render proposals that "
            "grant no authority. Offline; no subprocess, network or dispatch."
        ),
        epilog=(
            "A proposal is pending review: it is not acceptance approval, not a "
            "task, and it changes no skill, test, gate or policy."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    importer = subparsers.add_parser(
        "import", help="verify and store one incident record"
    )
    importer.add_argument("--record", required=True, help="incident JSON file")
    importer.add_argument(
        "--evidence-root",
        required=True,
        dest="evidence_root",
        help="directory every cited evidence ref resolves under",
    )
    importer.add_argument("--store", required=True, help="incident store directory")
    importer.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )

    proposer = subparsers.add_parser(
        "propose", help="render a non-authorizing pending-review proposal"
    )
    proposer.add_argument("--store", required=True, help="incident store directory")
    proposer.add_argument("--fingerprint", help="incident fingerprint to cite")
    proposer.add_argument("--content-id", dest="content_id", help="exact content id")
    proposer.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )

    replayer = subparsers.add_parser(
        "replay",
        help="execute one approved, non-promotional baseline/candidate replay",
        description=(
            "Run one evaluator-owned pytest suite against an explicit baseline "
            "and candidate commit. Non-promotional: the report is diagnostic "
            "evidence only and factory-verify/factory-promote reject it."
        ),
    )
    replayer.add_argument("--repo", required=True, help="local git work-tree root")
    replayer.add_argument("--baseline", required=True, help="full 40-hex commit SHA")
    replayer.add_argument("--candidate", required=True, help="full 40-hex commit SHA")
    replayer.add_argument(
        "--suite-dir", required=True, dest="suite_dir", help="evaluator suite directory"
    )
    replayer.add_argument(
        "--suite-digest",
        required=True,
        dest="suite_digest",
        help="external expected suite digest (sha256:<64 hex>)",
    )
    replayer.add_argument(
        "--work-root",
        required=True,
        dest="work_root",
        help="existing empty directory that receives the retained checkouts",
    )
    replayer.add_argument("--out", required=True, help="non-existent report directory")
    replayer.add_argument(
        "--approve-execution",
        action="store_true",
        dest="approve_execution",
        help="explicit approval to execute the reviewed suite and both revisions",
    )
    replayer.add_argument(
        "--timeout-s",
        type=int,
        dest="timeout_s",
        help="per-side timeout in seconds (default from the suite, max 3600)",
    )
    replayer.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )
    return parser


def render_human(result: LearningResult) -> str:
    if result.ok:
        if result.action == "proposed":
            return (
                f"factory-learning: PROPOSED {result.payload['proposal']['proposal_id']} "
                f"status=pending_review incident={result.content_id}"
            )
        if result.action == "duplicate":
            return (
                f"factory-learning: DUPLICATE incident={result.content_id} "
                f"fingerprint={result.fingerprint} (exit 0, nothing rewritten)"
            )
        return (
            f"factory-learning: IMPORTED {result.content_id} "
            f"fingerprint={result.fingerprint}"
        )
    return f"factory-learning: REJECTED {result.reason}: {result.detail}"


def render_replay_human(result) -> str:
    """Human rendering: the verdict, both SHAs, the digest and node outcomes."""
    lines = [
        f"factory-learning replay: {result.verdict.upper()} ({result.reason})",
    ]
    if result.detail:
        lines.append(f"  detail: {result.detail}")
    report = result.report
    if report:
        lines.append(
            f"  baseline {report['sources']['baseline']['requested_sha']} -> "
            f"candidate {report['sources']['candidate']['requested_sha']}"
        )
        lines.append(
            f"  suite {report['suite']['suite_id']} digest {report['suite']['digest']}"
        )
        for side in ("baseline", "candidate"):
            node = (
                report["sides"][side]
                .get("nodes", {})
                .get(report["expectations"]["target"]["nodeid"], {})
            )
            call = node.get("call") or {}
            lines.append(
                f"  {side}: target call={call.get('outcome')} "
                f"exception={call.get('exception_type')}"
            )
        lines.append("  non-promotional: promotional=false acceptance_evidence=false")
        lines.append(f"  report: {result.report_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = build_parser().parse_args(argv)
    if args.command == "replay":
        from factory.replay import replay

        result = replay(
            repo=args.repo,
            baseline=args.baseline,
            candidate=args.candidate,
            suite_dir=args.suite_dir,
            suite_digest=args.suite_digest,
            work_root=args.work_root,
            out=args.out,
            approve_execution=args.approve_execution,
            timeout_s=args.timeout_s,
        )
        if args.as_json:
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        else:
            print(render_replay_human(result))
        return result.exit_code

    if args.command == "import":
        result = import_incident(
            args.record, evidence_root=args.evidence_root, store=args.store
        )
    else:
        result = propose(
            store=args.store,
            fingerprint=args.fingerprint,
            content_id=args.content_id,
        )
    if args.as_json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(render_human(result))
    if result.ok:
        return 0
    print(
        f"factory-learning: {'conflict' if result.action == 'conflict' else 'rejected'}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
