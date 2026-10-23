"""Training regression: duplicate JSON object keys must not be silently kept.

This is the executable form of the cited ``evidence-gate-20261023`` F2
observable: ``factory-verify`` parsed the manifest with ``json.loads``, which
keeps the *last* duplicate member, so ``{"mode": "diagnostic", "mode":
"acceptance"}`` certified as acceptance while another conforming reader could
see ``diagnostic``. ``a07521c`` still does that; ``f90af08`` refuses the
ambiguous document.

The assertions below are about the observable, not about a private helper: a
duplicate-key document must be rejected as a *repeated key*, and a document
without repeats must not be rejected for that reason (the control case).
"""

import hashlib

from factory.evidence import verify_evidence

MANIFEST = "evidence-manifest.json"
SIDECAR = "evidence-manifest.sha256"
DUPLICATE_DOCUMENT = (
    b'{"schema": {"name": "factory-evidence", "version": 2}, '
    b'"mode": "diagnostic", "mode": "acceptance"}'
)
CLEAN_DOCUMENT = (
    b'{"schema": {"name": "factory-evidence", "version": 2}, "mode": "acceptance"}'
)


def _artifacts(tmp_path, document: bytes):
    directory = tmp_path / "artifacts"
    directory.mkdir()
    (directory / MANIFEST).write_bytes(document)
    (directory / SIDECAR).write_text(
        f"{hashlib.sha256(document).hexdigest()}  {MANIFEST}\n"
    )
    return directory


def test_duplicate_json_keys_are_rejected(tmp_path):
    result = verify_evidence(
        _artifacts(tmp_path, DUPLICATE_DOCUMENT), tmp_path, "a" * 40, "b" * 40
    )

    assert result.ok is False
    assert "repeats the object key" in result.detail


def test_missing_manifest_is_rejected(tmp_path):
    empty = tmp_path / "artifacts"
    empty.mkdir()

    result = verify_evidence(empty, tmp_path, "a" * 40, "b" * 40)

    assert result.ok is False
    assert result.reason == "manifest_missing"


def test_clean_manifest_is_not_rejected_for_a_repeated_key(tmp_path):
    result = verify_evidence(
        _artifacts(tmp_path, CLEAN_DOCUMENT), tmp_path, "a" * 40, "b" * 40
    )

    assert "repeats the object key" not in result.detail
