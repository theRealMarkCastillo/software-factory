"""Offline tests for provenance-checked incident intake (``factory.learning``).

Every case builds its own temporary evidence root, store and record file.
``tests/conftest.py`` already denies Docker and network commands for the whole
suite (``subprocess.run`` and ``subprocess.Popen``); the safety tests below
tighten that further to prove import/propose spawn nothing at all.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.learning import import_incident

CARD = "t_f5aac873"
BASE_SHA = "a" * 40
REPAIRED_SHA = "f90af08906583a5ebe13a7acf0bc65aa18d53a99"


def sha256_of(blob: bytes) -> str:
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def write_evidence(root: Path, name: str, blob: bytes) -> dict:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return {
        "ref": name,
        "kind": "probe",
        "bytes": len(blob),
        "sha256": sha256_of(blob),
    }


def base_record(evidence: dict, **overrides) -> dict:
    """A cited record shaped exactly like the accepted design (section 3)."""
    record = {
        "schema": {"name": "factory-incident", "version": 1},
        "title": "factory-verify certifies a manifest with duplicate JSON keys",
        "symptom": {
            "summary": "Factory-Verify accepts a manifest whose mode key repeats.",
            "observed": (
                "case duplicate_json_keys_last_wins returned verifier.exit=0, ok=true"
            ),
        },
        "category": "tooling_evidence",
        "root_cause": {"status": "unknown", "statement": None, "evidence_refs": []},
        "sources": {
            "repository": "software-factory-public",
            "revisions": [BASE_SHA],
            "repaired_revision": REPAIRED_SHA,
        },
        "evidence": [evidence],
        "hypothesis": "A strict duplicate-key parser rejects the ambiguous manifest.",
        "improvement_class": "regression_test",
        "replay": {
            "suite_id": "duplicate-json-key",
            "target_nodeid": (
                "test_duplicate_json_keys.py::test_duplicate_json_keys_are_rejected"
            ),
            "expect_baseline": "assertion_failure",
            "expect_candidate": "pass",
        },
        "corrects": None,
        "origin": {"kind": "reviewer", "ref": CARD},
    }
    record.update(overrides)
    return record


def write_record(directory: Path, record: dict, name: str = "incident.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def store_snapshot(store: Path) -> dict:
    """Every file under the store, with its hash (order-independent)."""
    return {
        str(path.relative_to(store)): sha256_of(path.read_bytes())
        for path in sorted(store.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def work(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    records = tmp_path / "records"
    records.mkdir()
    return SimpleNamespace(
        evidence=evidence, records=records, store=tmp_path / "store", tmp=tmp_path
    )


def import_fixture(work, record, name="incident.json"):
    path = write_record(work.records, record, name)
    return import_incident(path, evidence_root=work.evidence, store=work.store)


# ---------------------------------------------------------------------------
# A1 -- cited import, stable identity, idempotent re-import
# ---------------------------------------------------------------------------


def test_import_records_a_cited_incident(work):
    evidence = write_evidence(
        work.evidence, "qc-engine/extra-results.json", b'{"results": []}'
    )
    result = import_fixture(work, base_record(evidence))

    assert result.ok is True
    assert result.action == "imported"
    assert result.reason is None
    assert result.fingerprint.startswith("sha256:")
    assert result.content_id.startswith("sha256:")
    stored = json.loads(Path(result.path).read_text(encoding="utf-8"))
    assert stored["schema"] == {"name": "factory-incident", "version": 1}
    assert stored["fingerprint"] == result.fingerprint
    assert stored["content_id"] == result.content_id
    assert stored["evidence"] == [evidence]
    assert Path(result.path).is_relative_to(work.store)


def test_import_twice_is_idempotent_and_leaves_the_store_identical(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    record = base_record(evidence)
    first = import_fixture(work, record)
    after_first = store_snapshot(work.store)

    second = import_fixture(work, record)

    assert second.ok is True
    assert second.action == "duplicate"
    assert second.fingerprint == first.fingerprint
    assert second.content_id == first.content_id
    assert second.path == first.path
    assert store_snapshot(work.store) == after_first


def test_fingerprint_is_stable_but_content_id_tracks_wording(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    first = import_fixture(work, base_record(evidence))
    reworded = base_record(
        evidence,
        title="Another way to say the same defect",
        hypothesis="A different sentence about the same observable.",
    )
    second = import_fixture(work, reworded, name="reworded.json")

    # Same observable, revisions and evidence bytes -> one identity.
    assert second.fingerprint == first.fingerprint
    # Different wording is a different record, never a silent duplicate.
    assert second.content_id != first.content_id
    assert second.action == "conflict"
    assert second.ok is False


# ---------------------------------------------------------------------------
# A2 -- conflicts preserve history; corrections append
# ---------------------------------------------------------------------------


def test_conflict_leaves_prior_record_and_listing_unchanged(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    first = import_fixture(work, base_record(evidence))
    before = store_snapshot(work.store)

    changed = base_record(
        evidence,
        category="implementation",
        hypothesis="A different claim about the same observable.",
    )
    second = import_fixture(work, changed, name="changed.json")

    assert second.ok is False
    assert second.action == "conflict"
    assert second.reason == "conflict"
    assert second.fingerprint == first.fingerprint
    assert first.content_id in second.detail
    assert store_snapshot(work.store) == before

    from factory.learning import list_incidents

    records = list_incidents(work.store)
    assert [record["content_id"] for record in records] == [first.content_id]
    assert records[0]["category"] == "tooling_evidence"


def test_valid_correction_appends_a_linked_record(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    original = import_fixture(work, base_record(evidence))
    original_bytes = Path(original.path).read_bytes()

    correction = base_record(
        evidence,
        category="implementation",
        hypothesis="Clarified: the parser is correct, the writer is ambiguous.",
        root_cause={
            "status": "supported",
            "statement": "The producer emits a repeated mode key.",
            "evidence_refs": [0],
        },
        corrects=original.content_id,
    )
    second = import_fixture(work, correction, name="correction.json")

    assert second.ok is True
    assert second.action == "imported"
    assert second.fingerprint == original.fingerprint
    assert second.content_id != original.content_id
    assert (
        Path(second.path).read_text()
        == json.dumps(
            json.loads(Path(second.path).read_text()), indent=2, sort_keys=True
        )
        + "\n"
    )
    # History is immutable and intact.
    assert Path(original.path).read_bytes() == original_bytes

    from factory.learning import list_incidents

    records = list_incidents(work.store)
    assert {record["content_id"] for record in records} == {
        original.content_id,
        second.content_id,
    }
    stored_correction = next(
        record for record in records if record["content_id"] == second.content_id
    )
    assert stored_correction["corrects"] == original.content_id


def test_correction_citing_unknown_content_is_rejected_without_mutation(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    import_fixture(work, base_record(evidence))
    before = store_snapshot(work.store)

    bogus = base_record(
        evidence,
        hypothesis="A different claim.",
        corrects="sha256:" + "0" * 64,
    )
    result = import_fixture(work, bogus, name="bogus.json")

    assert result.ok is False
    assert result.reason in {"invalid_correction", "conflict"}
    assert store_snapshot(work.store) == before


# ---------------------------------------------------------------------------
# A3/A4 -- malformed records, bounds, path escapes and tampered stores
# ---------------------------------------------------------------------------


def _write_raw(work, blob: bytes, name: str = "incident.json") -> Path:
    path = work.records / name
    path.write_bytes(blob)
    return path


def _raw_import(work, path: Path):
    return import_incident(path, evidence_root=work.evidence, store=work.store)


def test_duplicate_json_keys_are_rejected(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    record = base_record(evidence)
    blob = json.dumps(record).encode()
    ambiguous = blob.replace(
        b'"category": "tooling_evidence"',
        b'"category": "tooling_evidence", "category": "implementation"',
    )
    result = _raw_import(work, _write_raw(work, ambiguous))

    assert result.ok is False
    assert result.reason == "malformed_record"
    assert "repeats" in result.detail
    assert not work.store.exists()


def test_oversized_record_is_rejected_before_parsing(work):
    evidence = write_evidence(work.evidence, "probe.json", b"{}")
    record = base_record(evidence, title="x" * (256 * 1024))
    result = _raw_import(work, _write_raw(work, json.dumps(record).encode()))

    assert result.ok is False
    assert result.reason == "record_too_large"


def test_deeply_nested_record_is_malformed_not_a_crash(work):
    blob = b'{"symptom": ' + b"[" * 2000 + b"]" * 2000 + b"}"
    result = _raw_import(work, _write_raw(work, blob))

    assert result.ok is False
    assert result.reason == "malformed_record"
    assert not work.store.exists()


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda record: record.update({"unexpected": True}), "malformed_record"),
        (
            lambda record: record["symptom"].update({"guess": "cause"}),
            "malformed_record",
        ),
        (lambda record: record.update({"category": "flaky"}), "invalid_category"),
        (
            lambda record: record.update({"improvement_class": "rewrite_everything"}),
            "invalid_improvement_class",
        ),
        (
            lambda record: record.update(
                {
                    "root_cause": {
                        "status": "supported",
                        "statement": None,
                        "evidence_refs": [],
                    }
                }
            ),
            "invalid_root_cause",
        ),
        (
            lambda record: record.update(
                {
                    "root_cause": {
                        "status": "unknown",
                        "statement": "guessed",
                        "evidence_refs": [],
                    }
                }
            ),
            "invalid_root_cause",
        ),
        (
            lambda record: record.update(
                {"schema": {"name": "factory-incident", "version": 2}}
            ),
            "malformed_record",
        ),
        (
            lambda record: record.update(
                {
                    "sources": {
                        "repository": "r",
                        "revisions": ["abc"],
                        "repaired_revision": None,
                    }
                }
            ),
            "malformed_record",
        ),
    ],
)
def test_invalid_schema_values_are_rejected_without_mutation(work, mutate, reason):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    record = base_record(evidence)
    mutate(record)

    result = _raw_import(work, _write_raw(work, json.dumps(record).encode()))

    assert result.ok is False
    assert result.reason == reason
    assert not work.store.exists()


def test_evidence_traversal_is_rejected_without_reading_outside(work):
    sentinel = work.tmp / "outside.txt"
    sentinel.write_bytes(b"do-not-read")
    evidence = {
        "ref": "../outside.txt",
        "kind": "probe",
        "bytes": sentinel.stat().st_size,
        "sha256": sha256_of(sentinel.read_bytes()),
    }
    result = _raw_import(
        work, _write_raw(work, json.dumps(base_record(evidence)).encode())
    )

    assert result.ok is False
    assert result.reason == "path_escape"
    assert sentinel.read_bytes() == b"do-not-read"
    assert not work.store.exists()


@pytest.mark.parametrize(
    "ref",
    [
        "/etc/passwd",
        "https://example.com/probe.json",
        "nested/../../outside.txt",
        "./probe.json",
        "nested//probe.json",
        "probe.json:stream",
    ],
)
def test_absolute_url_and_non_posix_refs_are_rejected(work, ref):
    result = _raw_import(
        work,
        _write_raw(
            work,
            json.dumps(
                base_record(
                    {
                        "ref": ref,
                        "kind": "probe",
                        "bytes": 1,
                        "sha256": "sha256:" + "0" * 64,
                    }
                )
            ).encode(),
        ),
    )

    assert result.ok is False
    assert result.reason in {"path_escape", "malformed_record"}
    assert not work.store.exists()


def test_symlinked_evidence_file_and_directory_are_rejected(work):
    outside = work.tmp / "outside"
    outside.mkdir()
    (outside / "probe.json").write_bytes(b'{"case": "duplicate"}')
    (work.evidence / "linkdir").symlink_to(outside)
    blob = (outside / "probe.json").read_bytes()
    entry = {
        "ref": "linkdir/probe.json",
        "kind": "probe",
        "bytes": len(blob),
        "sha256": sha256_of(blob),
    }
    result = _raw_import(
        work, _write_raw(work, json.dumps(base_record(entry)).encode())
    )

    assert result.ok is False
    assert result.reason == "path_escape"

    (work.evidence / "direct.json").symlink_to(outside / "probe.json")
    entry = {
        "ref": "direct.json",
        "kind": "probe",
        "bytes": len(blob),
        "sha256": sha256_of(blob),
    }
    second = _raw_import(
        work, _write_raw(work, json.dumps(base_record(entry)).encode())
    )

    assert second.ok is False
    assert second.reason == "path_escape"
    assert not work.store.exists()


def test_missing_and_stale_evidence_are_distinct_rejections(work):
    missing = {
        "ref": "gone.json",
        "kind": "probe",
        "bytes": 1,
        "sha256": "sha256:" + "0" * 64,
    }
    first = _raw_import(
        work, _write_raw(work, json.dumps(base_record(missing)).encode())
    )
    assert first.reason == "evidence_missing"

    real = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    stale = dict(real, sha256="sha256:" + "a" * 64)
    second = _raw_import(
        work, _write_raw(work, json.dumps(base_record(stale)).encode())
    )
    assert second.reason == "evidence_hash_mismatch"

    resized = dict(real, bytes=real["bytes"] + 1)
    third = _raw_import(
        work, _write_raw(work, json.dumps(base_record(resized)).encode())
    )
    assert third.reason == "evidence_hash_mismatch"
    assert not work.store.exists()


def test_supplied_ids_must_match_recomputed_values(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    forged = base_record(evidence, fingerprint="sha256:" + "b" * 64)
    first = _raw_import(work, _write_raw(work, json.dumps(forged).encode()))
    assert first.ok is False
    assert first.reason == "invalid_incident_id"

    forged_content = base_record(evidence, content_id="sha256:" + "b" * 64)
    second = _raw_import(work, _write_raw(work, json.dumps(forged_content).encode()))
    assert second.reason == "invalid_incident_id"
    assert not work.store.exists()


def test_planted_store_symlinks_are_never_followed(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    first = import_fixture(work, base_record(evidence))

    outside = work.tmp / "outside"
    outside.mkdir()
    planted = outside / "planted.json"
    planted.write_text("{}")
    record_dir = work.store / "incidents" / first.fingerprint.split(":", 1)[1]
    record_dir.rename(outside / "real-dir")
    record_dir.symlink_to(outside / "real-dir")

    result = import_fixture(work, base_record(evidence), name="again.json")

    assert result.ok is False
    assert result.reason == "store_tampered"
    assert planted.read_text() == "{}"


def test_store_inside_evidence_root_or_evidence_inside_store_is_refused(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    overlap = import_incident(
        write_record(work.records, base_record(evidence)),
        evidence_root=work.evidence,
        store=work.evidence / "store",
    )
    assert overlap.ok is False
    assert overlap.reason == "store_invalid"

    nested = work.store / "evidence"
    nested.mkdir(parents=True)
    (nested / "probe.json").write_bytes(b'{"case": "duplicate"}')
    entry = {
        "ref": "probe.json",
        "kind": "probe",
        "bytes": len(b'{"case": "duplicate"}'),
        "sha256": sha256_of(b'{"case": "duplicate"}'),
    }
    reverse = import_incident(
        write_record(work.records, base_record(entry), name="reverse.json"),
        evidence_root=nested,
        store=work.store,
    )
    assert reverse.ok is False
    assert reverse.reason == "store_invalid"


def test_store_is_a_symlink_or_sensitive_root_is_refused(work):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    record_path = write_record(work.records, base_record(evidence))

    link = work.tmp / "link-store"
    link.symlink_to(work.tmp / "real-store")
    result = import_incident(record_path, evidence_root=work.evidence, store=link)
    assert result.reason == "store_invalid"

    for sensitive in ("/etc", "/proc", "/sys", "/dev", "/root"):
        rejected = import_incident(
            record_path,
            evidence_root=work.evidence,
            store=Path(sensitive) / "factory-store",
        )
        assert rejected.reason in {"store_invalid", "evidence_root_invalid"}
        assert not (Path(sensitive) / "factory-store").exists()


def _deny(monkeypatch, target: Path, method: str) -> None:
    """Make one filesystem method raise ``PermissionError`` for ``target`` only.

    ``/root/factory-store`` cannot be stat'ed by an unprivileged Linux CI runner,
    so the real refusal path must survive ``PermissionError``/``OSError`` from
    any filesystem probe and still answer with a structured ``store_invalid``.
    Mocking keeps the case deterministic on hosts (like macOS) where the path is
    simply absent.
    """

    original = getattr(Path, method)

    def guarded(self, *args, **kwargs):
        if Path(self) == target:
            raise PermissionError(13, "Permission denied", str(target))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, guarded)


@pytest.mark.parametrize("method", ["is_symlink", "resolve", "exists", "mkdir"])
def test_inaccessible_store_path_is_refused_with_a_structured_result(
    work, monkeypatch, method
):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    record_path = write_record(work.records, base_record(evidence))
    blocked = (work.tmp / "unreadable-store").resolve()

    _deny(monkeypatch, blocked, method)

    result = import_incident(record_path, evidence_root=work.evidence, store=blocked)

    assert result.ok is False
    assert result.action == "rejected"
    assert result.reason == "store_invalid"
    assert blocked.name in (result.detail or "")
    assert not (work.tmp / "unreadable-store" / "incidents").exists()
    assert not (work.tmp / "unreadable-store" / "proposals").exists()


def test_ci_permission_denied_sensitive_store_is_refused_not_raised(work, monkeypatch):
    """The exact Linux CI failure: stat below /root raises ``PermissionError``."""
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    record_path = write_record(work.records, base_record(evidence))
    ci_store = Path("/root/factory-store")

    _deny(monkeypatch, ci_store, "is_symlink")

    result = import_incident(record_path, evidence_root=work.evidence, store=ci_store)

    assert result.ok is False
    assert result.reason == "store_invalid"


def test_failed_store_admission_leaves_no_record_or_proposal(work, monkeypatch):
    evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
    record_path = write_record(work.records, base_record(evidence))
    blocked = work.tmp / "unreadable-store"

    _deny(monkeypatch, blocked, "is_symlink")

    import_incident(record_path, evidence_root=work.evidence, store=blocked)

    assert sorted(path.name for path in work.tmp.iterdir()) == ["evidence", "records"]


def test_ordinary_owned_home_subdirectory_is_a_valid_store(work):
    base = Path.home() / ".hermes" / "cache" / "scratch" / f"learning-{work.tmp.name}"
    base.mkdir(parents=True)
    try:
        evidence = write_evidence(work.evidence, "probe.json", b'{"case": "duplicate"}')
        result = import_incident(
            write_record(work.records, base_record(evidence)),
            evidence_root=work.evidence,
            store=base / "store",
        )
        assert result.ok is True
        assert result.action == "imported"
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)
