"""Unit tests for the factory Data Model Gate. Deterministic; no Docker, no
network. Exercises the self-contained data-model.yml validation and the
required-test reference checks."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory.datamodel import run_data_model_gate, self_check_question


def _repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return repo


def _valid_doc():
    return {
        "entities": {
            "fact": {
                "identity": ["fact_id"],
                "mutable": {"revision": ["value"]},
                "immutable": ["created_at", "original_submitter"],
                "append_only": {"evidence": True},
                "tests": {
                    "round_trip": "tests/test_fact.py::test_revision_round_trip",
                    "immutability": "tests/test_fact.py::test_immutable_rejected",
                    "concurrent_append": "tests/test_fact.py::test_concurrent_evidence",
                },
            }
        }
    }


def test_no_document_is_blocking(tmp_path):
    repo = _repo(tmp_path, {})
    r = run_data_model_gate(str(repo))
    assert r.status == "no_document"


def test_valid_document_passes(tmp_path):
    files = {
        "data-model.yml": _yaml(_valid_doc()),
        "tests/test_fact.py": "def test_revision_round_trip():\n    pass\n"
        "def test_immutable_rejected():\n    pass\n"
        "def test_concurrent_evidence():\n    pass\n",
    }
    repo = _repo(tmp_path, files)
    r = run_data_model_gate(str(repo))
    assert r.status == "pass", r.gaps


def test_identity_also_mutable_is_blocking(tmp_path):
    doc = _valid_doc()
    doc["entities"]["fact"]["mutable"]["revision"].append("fact_id")
    repo = _repo(tmp_path, {"data-model.yml": _yaml(doc)})
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("identity" in g for g in r.gaps)


def test_immutable_also_mutable_is_blocking(tmp_path):
    doc = _valid_doc()
    doc["entities"]["fact"]["mutable"]["revision"].append("created_at")
    repo = _repo(tmp_path, {"data-model.yml": _yaml(doc)})
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("immutable" in g for g in r.gaps)


def test_missing_identity_is_gap(tmp_path):
    doc = _valid_doc()
    del doc["entities"]["fact"]["identity"]
    repo = _repo(tmp_path, {"data-model.yml": _yaml(doc)})
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("identity" in g for g in r.gaps)


def test_missing_round_trip_test_is_gap(tmp_path):
    doc = _valid_doc()
    del doc["entities"]["fact"]["tests"]["round_trip"]
    repo = _repo(tmp_path, {"data-model.yml": _yaml(doc)})
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("round_trip" in g for g in r.gaps)


def test_referenced_test_file_missing(tmp_path):
    doc = _valid_doc()
    doc["entities"]["fact"]["tests"]["round_trip"] = "tests/nope.py::test_x"
    repo = _repo(tmp_path, {"data-model.yml": _yaml(doc)})
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("missing file" in g for g in r.gaps)


def test_referenced_test_function_missing(tmp_path):
    files = {
        "data-model.yml": _yaml(_valid_doc()),
        "tests/test_fact.py": "def test_revision_round_trip():\n    pass\n",
    }
    repo = _repo(tmp_path, files)
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("not found" in g for g in r.gaps)


def test_no_entities_is_fail(tmp_path):
    repo = _repo(tmp_path, {"data-model.yml": "entities: {}\n"})
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"


def test_malformed_yaml_is_unavailable(tmp_path):
    repo = _repo(tmp_path, {"data-model.yml": "entities: [unclosed\n"})
    r = run_data_model_gate(str(repo))
    assert r.status == "unavailable"


def test_self_check_question_names_operations():
    q = self_check_question("fact", {"mutable": {"revision": ["value"]}})
    assert "revision" in q
    assert "read" in q


def _valid_files(spec_text=None, specs_declared=None):
    doc = _valid_doc()
    if specs_declared is not None:
        doc["specs"] = specs_declared
    files = {
        "data-model.yml": _yaml(doc),
        "tests/test_fact.py": (
            "def test_revision_round_trip():\n    pass\n"
            "def test_immutable_rejected():\n    pass\n"
            "def test_concurrent_evidence():\n    pass\n"
        ),
    }
    if spec_text is not None:
        files["spec.md"] = spec_text
    return files


def test_spec_mutating_identity_field_is_blocking(tmp_path):
    repo = _repo(
        tmp_path, _valid_files(spec_text="A correction may change the fact_id field.\n")
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("fact_id" in g and "mutable-via-operation" in g for g in r.gaps)


def test_spec_mutating_immutable_field_is_blocking(tmp_path):
    repo = _repo(
        tmp_path, _valid_files(spec_text="Users may edit the created_at field.\n")
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("created_at" in g for g in r.gaps)


def test_spec_mutating_mutable_field_is_ok(tmp_path):
    repo = _repo(
        tmp_path, _valid_files(spec_text="A revision updates the value field.\n")
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "pass", r.gaps


def test_spec_negated_change_is_not_flagged(tmp_path):
    repo = _repo(
        tmp_path,
        _valid_files(spec_text="Identity fields do not change during revision.\n"),
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "pass", r.gaps


def test_spec_without_known_field_is_ignored(tmp_path):
    repo = _repo(
        tmp_path, _valid_files(spec_text="The system updates the throughput metric.\n")
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "pass", r.gaps


def test_declared_spec_missing_is_gap(tmp_path):
    repo = _repo(tmp_path, _valid_files(specs_declared=["docs/behavior.md"]))
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("declared but not found" in g for g in r.gaps)


def test_declared_spec_file_is_cross_checked(tmp_path):
    files = _valid_files(specs_declared=["docs/behavior.md"])
    files["docs/behavior.md"] = "A revision may change the fact_id field.\n"
    repo = _repo(tmp_path, files)
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("fact_id" in g and "docs/behavior.md" in g for g in r.gaps)


def test_no_spec_present_still_passes(tmp_path):
    repo = _repo(tmp_path, _valid_files())
    r = run_data_model_gate(str(repo))
    assert r.status == "pass", r.gaps


def test_spec_via_cli_flag(tmp_path):
    files = _valid_files()
    files["docs/behavior.md"] = "A revision may change the fact_id field.\n"
    repo = _repo(tmp_path, files)
    r = run_data_model_gate(str(repo), spec_files=["docs/behavior.md"])
    assert r.status == "fail"
    assert any("fact_id" in g for g in r.gaps)


def test_spec_mixed_polarity_sentence_flags_neither_half(tmp_path):
    """The common "X may change but must not change Y" wording must not
    false-flag its negated half."""
    repo = _repo(
        tmp_path,
        _valid_files(
            spec_text="A revision may change the value, but must not change fact_id.\n"
        ),
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "pass", r.gaps


def test_spec_flags_identity_when_verb_follows_field(tmp_path):
    repo = _repo(
        tmp_path,
        _valid_files(spec_text="The fact_id may be changed by a correction.\n"),
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("fact_id" in g for g in r.gaps)


def test_spec_plural_nouns_are_matched(tmp_path):
    repo = _repo(
        tmp_path, _valid_files(spec_text="Corrections rewrite the created_at column.\n")
    )
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("created_at" in g for g in r.gaps)


def test_unknown_test_kind_is_flagged(tmp_path):
    doc = _valid_doc()
    doc["entities"]["fact"]["tests"]["roundtrip"] = "tests/test_fact.py::test_x"
    files = {
        "data-model.yml": _yaml(doc),
        "tests/test_fact.py": (
            "def test_revision_round_trip():\n    pass\n"
            "def test_immutable_rejected():\n    pass\n"
            "def test_concurrent_evidence():\n    pass\n"
        ),
    }
    repo = _repo(tmp_path, files)
    r = run_data_model_gate(str(repo))
    assert r.status == "fail"
    assert any("unknown test kind" in g for g in r.gaps)


def test_result_carries_self_check_questions(tmp_path):
    repo = _repo(tmp_path, _valid_files())
    r = run_data_model_gate(str(repo))
    assert r.status == "pass", r.gaps
    assert r.self_checks and "write-then-read" in r.self_checks[0]


def test_cli_exit_codes(tmp_path, capsys):
    from factory.datamodel import main

    ok = _repo(tmp_path / "ok", _valid_files())
    assert main(["--repo", str(ok)]) == 0
    capsys.readouterr()

    bad = _repo(tmp_path / "bad", {"data-model.yml": "entities: {}\n"})
    assert main(["--repo", str(bad)]) == 1
    out = capsys.readouterr().out
    assert '"status": "fail"' in out


def _yaml(doc):
    import yaml

    return yaml.safe_dump(doc, sort_keys=False)
