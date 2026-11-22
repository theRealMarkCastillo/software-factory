"""Advisory prose findings must not weaken deterministic data-model checks."""

import json

import yaml

from factory.datamodel import main, run_data_model_gate


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
