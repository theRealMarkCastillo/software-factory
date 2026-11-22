"""Advisory prose findings must not weaken deterministic data-model checks."""

import json
from pathlib import Path

import pytest
import yaml
from test_verify import _evidence, _repo

from factory.cli import main as factory_main
from factory.datamodel import main, run_data_model_gate
from factory.evidence import assert_assembled_evidence, verify_evidence


def _model_repo(tmp_path, spec="A correction may change the fact_id field.\n"):
    (tmp_path / "data-model.yml").write_text(
        yaml.safe_dump({"entities": {"fact": {"identity": ["fact_id"]}}})
    )
    (tmp_path / "spec.md").write_text(spec)
    return tmp_path


def test_prose_claim_is_serialized_warning_not_gap(tmp_path, capsys):
    repo = _model_repo(tmp_path)
    result = run_data_model_gate(str(repo))
    assert result.status == "pass"
    assert result.gaps == []
    assert result.warnings == [
        "spec file 'spec.md' line 1 mutates field 'fact_id' of entity 'fact', "
        "but 'fact_id' is not in that entity's mutable-via-operation category "
        "(BLOCKING gap)"
    ]
    assert result.detail == "data model gate passed (1 advisory spec warning(s))"
    assert result.to_dict()["warnings"] == result.warnings
    assert main(["--repo", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out) == result.to_dict()


def test_multiple_claims_count_warnings(tmp_path):
    repo = _model_repo(tmp_path, "Change fact_id.\nEdit fact_id.\n")
    result = run_data_model_gate(str(repo))
    assert result.status == "pass"
    assert result.gaps == []
    assert len(result.warnings) == 2
    assert result.detail == "data model gate passed (2 advisory spec warning(s))"


@pytest.mark.parametrize("failure", ["missing", "unreadable", "structural", "test_ref"])
def test_warnings_do_not_mask_blocking_gaps(tmp_path, monkeypatch, capsys, failure):
    repo = _model_repo(tmp_path)
    doc_path = repo / "data-model.yml"
    doc = yaml.safe_load(doc_path.read_text())
    entity = doc["entities"]["fact"]
    if failure == "missing":
        doc["specs"] = ["spec.md", "missing.md"]
        expected = "spec file 'missing.md' declared but not found"
    elif failure == "unreadable":
        (repo / "unreadable.md").write_text("Change fact_id.\n")
        doc["specs"] = ["spec.md", "unreadable.md"]
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == repo / "unreadable.md":
                raise OSError("read refused")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        expected = "cannot read spec file 'unreadable.md': read refused"
    elif failure == "structural":
        entity["immutable"] = ["created_at"]
        expected = "no 'tests.immutability'"
    else:
        entity["tests"] = {"round_trip": "tests/missing.py::test_round_trip"}
        expected = "references missing file 'tests/missing.py'"
    doc_path.write_text(yaml.safe_dump(doc))
    result = run_data_model_gate(str(repo))
    assert result.status == "fail"
    assert any(expected in gap for gap in result.gaps)
    assert len(result.warnings) == 1
    assert result.detail == f"{len(result.gaps)} gap(s) found"
    assert main(["--repo", str(repo)]) == 1
    assert json.loads(capsys.readouterr().out) == result.to_dict()


@pytest.mark.parametrize(
    "spec", ["Do not change fact_id.\n", "Change throughput.\n", ""]
)
def test_no_claim_keeps_original_pass_detail(tmp_path, spec):
    result = run_data_model_gate(str(_model_repo(tmp_path, spec)))
    assert result.status == "pass"
    assert result.warnings == []
    assert result.to_dict()["warnings"] == []
    assert result.detail == "data model gate passed"


def test_warning_manifest_is_accepted_by_checker_and_factory_verify(tmp_path, capsys):
    model = {
        "entities": {"fact": {"identity": ["fact_id"]}},
    }
    repo, base, candidate = _repo(
        tmp_path,
        candidate_files={
            "data-model.yml": yaml.safe_dump(model),
            "spec.md": "Change fact_id.\n",
        },
    )
    gate = run_data_model_gate(str(repo)).to_dict()
    assert gate["status"] == "pass"
    assert len(gate["warnings"]) == 1
    artifacts = _evidence(tmp_path, repo, base, candidate, datamodel=gate)
    manifest = json.loads((artifacts / "evidence-manifest.json").read_text())
    assert manifest["datamodel"] == gate
    assert_assembled_evidence(manifest)
    result = verify_evidence(artifacts, repo, candidate, base)
    assert result.ok, (result.reason, result.detail)
    assert (
        factory_main(
            [
                "verify",
                "--artifacts-dir",
                str(artifacts),
                "--repo",
                str(repo),
                "--candidate",
                candidate,
                "--base",
                base,
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True
