"""Structured spec claims: binding examples and public CLI regressions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from factory.datamodel import run_data_model_gate


def make_repo(root, model=None, specs=None, strict=None):
    root.mkdir(parents=True, exist_ok=True)
    if model is None:
        model = {"orders": {"edit": ["value", "revision"]}}
    entities = {
        name: {
            "identity": ["id"],
            "immutable": ["created"],
            "mutable": operations,
            "tests": {"round_trip": "probe.py", "immutability": "probe.py"},
        }
        for name, operations in model.items()
    }
    doc = {"entities": entities}
    if strict is not None:
        doc["spec_mutations"] = strict
    if specs is not None:
        doc["specs"] = list(specs)
        for name, text in specs.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    (root / "probe.py").write_text(
        "# Reference existence only; not storage evidence.\n"
    )
    (root / "data-model.yml").write_text(yaml.safe_dump(doc))
    return root


def block(claims, fence="```", info="yaml factory-mutations"):
    return f"{fence}{info}\n{yaml.safe_dump(claims)}{fence}\n"


def prefixes(result, category="gaps"):
    return {text.split(":", 1)[0] for text in getattr(result, category)}


def test_single_block_records_normalized_declarations(tmp_path):
    repo = make_repo(
        tmp_path,
        specs={"spec.md": block({"orders": {"edit": ["value", "revision", "value"]}})},
    )
    result = run_data_model_gate(str(repo))
    assert result.status == "pass", result.gaps
    assert result.to_dict()["declared_mutations"] == {
        "spec": {"orders": {"edit": ["revision", "value"]}},
        "model": {"orders": {"edit": ["revision", "value"]}},
    }


@pytest.mark.parametrize(
    "text,present,error",
    [
        (block({"orders": {}}), True, False),
        (block({"orders": {}}, "~~~"), True, False),
        ("   ```yaml factory-mutations\norders: {}\n   ```\n", True, False),
        ("    ```yaml factory-mutations\norders: {}\n    ```\n", False, False),
        (
            "````markdown\n```yaml factory-mutations\norders: {}\n```\n````\n",
            False,
            False,
        ),
        ("~~~text\n```yaml factory-mutations\norders: {}\n```\n~~~\n", False, False),
        (block({"orders": {}}, info="yaml factory-mutations title=x"), True, False),
        ("```yaml factory-mutations\norders: {}\n", True, True),
        ("```yaml factory-mutations\norders: [\n```\n", True, True),
        (block(["orders"]), True, True),
        (block({"orders": []}), True, True),
        (block({"orders": {"edit": "value"}}), True, True),
        (block({"orders": {"edit": [42]}}), True, True),
        (block({42: {}}), True, True),
        (block({"orders": {42: []}}), True, True),
        ("```yaml factory-mutations\n```\n", True, True),
        ("```yaml factory-mutations\norders: {}\n````\n", True, False),
        ("````yaml factory-mutations\norders: {}\n```\n", True, True),
        ("```yaml factory-mutations\norders: {}\n~~~\n", True, True),
        ("```yaml factory-mutations\norders: {}\n``` trailing\n", True, True),
        (block({"orders": {}}, info="yaml other factory-mutations"), False, False),
        (block({"orders": {}}, info="yaml factory-mutations`"), False, False),
        ("orders: {}\n", False, False),
    ],
)
def test_fence_and_shape_matrix(tmp_path, text, present, error):
    repo = make_repo(tmp_path, {"orders": {}}, {"spec.md": text})
    result = run_data_model_gate(str(repo))
    assert ("spec-mutations block error" in prefixes(result)) is error
    assert result.status == ("fail" if error else "pass"), result.gaps
    if not error:
        assert result.to_dict()["declared_mutations"]["spec"] == (
            {"orders": {}} if present else None
        )


def test_blocks_merge_across_files_and_normalize_order(tmp_path):
    model = {
        "notes": {},
        "orders": {"revise": ["value"], "edit": ["revision", "value"]},
    }
    specs = {
        "one.md": block({"orders": {"edit": ["value"]}})
        + block({"orders": {"revise": ["value"]}}),
        "two.md": block({"orders": {"edit": ["revision", "value"]}, "notes": {}}),
    }
    result = run_data_model_gate(str(make_repo(tmp_path, model, specs)))
    declarations = result.to_dict()["declared_mutations"]
    assert declarations["spec"] == declarations["model"]
    assert list(declarations["spec"]) == ["notes", "orders"]
    assert list(declarations["spec"]["orders"]) == ["edit", "revise"]
    assert declarations["spec"]["orders"]["edit"] == ["revision", "value"]


@pytest.mark.parametrize(
    "model,claims,expected",
    [
        ({"orders": {}}, {"notes": {}}, {"G1"}),
        ({"orders": {}}, {"orders": {}}, set()),
        ({"orders": {"edit": ["value"]}}, {"orders": {"edit": ["id"]}}, {"G2", "G3"}),
        (
            {"orders": {"edit": ["value"]}},
            {"orders": {"edit": ["created"]}},
            {"G2", "G3"},
        ),
        ({"orders": {"edit": ["value"]}}, {"orders": {"edit": ["value"]}}, set()),
        ({"orders": {"edit": ["value"]}}, {"orders": {"revise": []}}, {"G3"}),
        ({"orders": {"edit": ["value"]}}, {"orders": {"edit": ["extra"]}}, {"G3"}),
        ({"orders": {"edit": ["value"]}}, {"orders": {}}, {"G4"}),
        ({"orders": {"edit": []}}, {"orders": {}}, {"G4"}),
    ],
)
def test_gap_decision_table(tmp_path, model, claims, expected):
    result = run_data_model_gate(
        str(make_repo(tmp_path, model, {"spec.md": block(claims)}))
    )
    assert prefixes(result) == {f"spec-mutations {code}" for code in expected}
    assert result.status == ("fail" if expected else "pass")
    if "G4" in expected:
        assert "spec-mutations W1" not in prefixes(result, "warnings")


@pytest.mark.parametrize(
    "model,claims,warn",
    [
        (
            {"orders": {"edit": ["value", "revision"]}},
            {"orders": {"edit": ["value"]}},
            True,
        ),
        ({"orders": {"edit": []}}, {}, True),
        ({"orders": {"edit": []}}, {"orders": {"edit": []}}, False),
        ({"orders": {"edit": [], "revise": []}}, {"orders": {"edit": []}}, True),
        ({"orders": {}}, {"orders": {}}, False),
    ],
)
def test_unclaimed_model_w1(tmp_path, model, claims, warn):
    result = run_data_model_gate(
        str(make_repo(tmp_path, model, {"spec.md": block(claims)}))
    )
    assert result.status == "pass", result.gaps
    assert ("spec-mutations W1" in prefixes(result, "warnings")) is warn
    assert "spec-mutations W2" not in prefixes(result, "warnings")


@pytest.mark.parametrize("strict", [None, "optional", "required"])
def test_no_blocks_w2_and_opt_in_strictness(tmp_path, strict):
    repo = make_repo(tmp_path, specs={"spec.md": "Update id.\n"}, strict=strict)
    result = run_data_model_gate(str(repo))
    assert result.status == ("fail" if strict == "required" else "pass")
    assert result.to_dict()["declared_mutations"]["spec"] is None
    assert result.gaps == (
        ["spec-mutations W2: spec mutations required but none declared"]
        if strict == "required"
        else []
    )
    assert result.warnings == [
        "spec file 'spec.md' line 1 mutates field 'id' of entity 'orders', "
        "but 'id' is not in that entity's mutable-via-operation category (BLOCKING gap)"
    ]
    assert result.detail == (
        "1 gap(s) found"
        if strict == "required"
        else "data model gate passed (1 advisory spec warning(s))"
    )


def test_no_specs_read_does_not_warn_w2(tmp_path):
    result = run_data_model_gate(str(make_repo(tmp_path)))
    assert result.status == "pass"
    assert not result.warnings


def test_prose_is_advisory_even_with_structured_claims(tmp_path):
    repo = make_repo(
        tmp_path,
        specs={"spec.md": "Update id.\n" + block({"orders": {"edit": ["value"]}})},
    )
    result = run_data_model_gate(str(repo))
    assert result.status == "pass", result.gaps
    assert any(w.startswith("spec file 'spec.md' line 1") for w in result.warnings)


def test_cli_spec_override_and_block_gap(tmp_path, capsys):
    from factory.datamodel import main

    repo = make_repo(tmp_path, specs={"spec.md": block({"orders": {"edit": ["id"]}})})
    (repo / "other.md").write_text(block({"orders": {"edit": ["value"]}}))
    assert main(["--repo", str(repo), "--spec", "other.md"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    assert main(["--repo", str(repo)]) == 1
    assert json.loads(capsys.readouterr().out)["gaps"][0].startswith(
        "spec-mutations G2:"
    )


@pytest.mark.parametrize("text", ["No changes.\n", "Update id.\n"])
def test_absent_block_keeps_legacy_detail_and_warnings(tmp_path, text):
    result = run_data_model_gate(str(make_repo(tmp_path, specs={"spec.md": text})))
    expected = (
        []
        if text == "No changes.\n"
        else [
            "spec file 'spec.md' line 1 mutates field 'id' of entity 'orders', "
            "but 'id' is not in that entity's mutable-via-operation category (BLOCKING gap)"
        ]
    )
    assert result.warnings == expected
    assert result.detail == (
        "data model gate passed (1 advisory spec warning(s))"
        if expected
        else "data model gate passed"
    )
    assert result.to_dict()["declared_mutations"]["spec"] is None


def test_spec_read_once_for_prose_and_block(tmp_path, monkeypatch):
    repo = make_repo(
        tmp_path, specs={"spec.md": block({"orders": {"edit": ["value"]}})}
    )
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path == repo / "spec.md":
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    result = run_data_model_gate(str(repo))
    assert result.status == "pass"
    assert reads == [repo / "spec.md"]
    assert result.to_dict()["declared_mutations"]["spec"] == {
        "orders": {"edit": ["value"]}
    }


def test_manifest_declared_mutations_accepted_by_real_verify_cli(tmp_path):
    from test_verify import _evidence, _repo
    from factory.evidence import assert_assembled_evidence

    repo, base, candidate = _repo(
        tmp_path,
        candidate_files={
            "data-model.yml": yaml.safe_dump(
                {"entities": {"notes": {"identity": ["id"]}}}
            ),
            "spec.md": block({"notes": {}}),
        },
    )
    gate = run_data_model_gate(str(repo)).to_dict()
    assert gate["declared_mutations"] == {"spec": {"notes": {}}, "model": {"notes": {}}}
    artifacts = _evidence(tmp_path, repo, base, candidate, datamodel=gate)
    manifest = json.loads((artifacts / "evidence-manifest.json").read_text())
    assert manifest["datamodel"]["declared_mutations"] == gate["declared_mutations"]
    assert_assembled_evidence(manifest)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory",
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
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["ok"] is True
