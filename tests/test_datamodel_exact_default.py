"""Exact implicit default matching, independent of host filesystem casing."""

import json
from pathlib import Path

import pytest
import yaml

from factory.datamodel import main, run_data_model_gate


@pytest.fixture
def case_insensitive_repo(tmp_path, monkeypatch):
    repo = tmp_path.resolve()
    original_is_file = Path.is_file
    original_read_text = Path.read_text
    spec_reads = []

    def resolve_alias(path):
        if path == repo / "spec.md" and original_is_file(repo / "SPEC.md"):
            return repo / "SPEC.md"
        return path

    def is_file(path):
        return original_is_file(resolve_alias(path))

    def read_text(path, *args, **kwargs):
        if path.parent == repo and path.name.lower() == "spec.md":
            spec_reads.append(path.name)
        return original_read_text(resolve_alias(path), *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "read_text", read_text)
    return repo, spec_reads


@pytest.mark.parametrize(
    "filename,declared,via_cli,expected_status,expected_reads,expected_gap",
    [
        ("SPEC.md", None, False, "pass", [], None),
        ("spec.md", None, False, "fail", ["spec.md"], "mutates field 'fact_id'"),
        (None, None, False, "pass", [], None),
        ("SPEC.md", "SPEC.md", False, "fail", ["SPEC.md"], "mutates field 'fact_id'"),
        ("SPEC.md", "SPEC.md", True, "fail", ["SPEC.md"], "mutates field 'fact_id'"),
        ("SPEC.md", "spec.md", False, "fail", ["spec.md"], "mutates field 'fact_id'"),
        ("SPEC.md", "spec.md", True, "fail", ["spec.md"], "mutates field 'fact_id'"),
        (None, "missing.md", False, "fail", [], "declared but not found"),
        (None, "missing.md", True, "fail", [], "declared but not found"),
    ],
    ids=[
        "implicit-uppercase-skipped",
        "implicit-lowercase-scanned",
        "implicit-missing-skipped",
        "document-uppercase-scanned",
        "cli-uppercase-scanned",
        "document-case-insensitive-resolution-preserved",
        "cli-case-insensitive-resolution-preserved",
        "document-missing-gap",
        "cli-missing-gap",
    ],
)
def test_exact_default_at_gate_entry_points(
    case_insensitive_repo,
    capsys,
    filename,
    declared,
    via_cli,
    expected_status,
    expected_reads,
    expected_gap,
):
    repo, spec_reads = case_insensitive_repo
    doc = {"entities": {"fact": {"identity": ["fact_id"]}}}
    if declared is not None and not via_cli:
        doc["specs"] = [declared]
    (repo / "data-model.yml").write_text(yaml.safe_dump(doc))
    if filename is not None:
        (repo / filename).write_text("A correction may change the fact_id field.\n")

    result = run_data_model_gate(str(repo), spec_files=[declared] if via_cli else None)
    assert result.status == expected_status, result.gaps
    assert spec_reads == expected_reads
    if expected_gap is None:
        assert result.gaps == []
    else:
        assert len(result.gaps) == 1
        assert expected_gap in result.gaps[0]

    spec_reads.clear()
    argv = ["--repo", str(repo)]
    if via_cli:
        argv.extend(["--spec", declared])
    assert main(argv) == (0 if expected_status == "pass" else 1)
    assert json.loads(capsys.readouterr().out) == result.to_dict()
    assert spec_reads == expected_reads
