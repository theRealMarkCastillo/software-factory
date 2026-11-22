"""Structured spec claims: binding examples and public CLI regressions."""

import json
import os
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
        (block({"orders": {"edit": [[42]]}}), True, True),
        (block({42: []}), True, True),
        (block({"orders": {42: "value"}}), True, True),
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


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("with_prose", [False, True])
@pytest.mark.parametrize(
    "model_yaml",
    [
        "switches: {toggle: [label, on]}",
        "yes: {toggle: [label]}\nswitches: {toggle: [label]}",
        "orders: {edit: [value, 2]}",
    ],
    ids=["boolean-field", "boolean-entity", "integer-field"],
)
def test_yaml_scalars_without_block_keep_legacy_result(
    tmp_path, entrypoint, with_prose, model_yaml
):
    repo = make_repo(
        tmp_path,
        yaml.safe_load(model_yaml),
        {"spec.md": "A catalog of records.\n"} if with_prose else None,
    )
    if entrypoint == "api":
        result = run_data_model_gate(str(repo)).to_dict()
    else:
        process = subprocess.run(
            [sys.executable, "-m", "factory.datamodel", "--repo", str(repo)],
            capture_output=True,
            text=True,
        )
        assert process.returncode == 0, (process.stdout, process.stderr)
        result = json.loads(process.stdout)
    assert result["status"] == "pass"
    assert result["warnings"] == []
    assert result["detail"] == "data model gate passed"
    assert result["declared_mutations"]["spec"] is None
    assert result["gaps"] == []
    json.dumps(result)


@pytest.mark.parametrize("claims", [{"switches": {"toggle": ["label"]}}, {}])
def test_yaml_scalars_with_block_are_deterministic(tmp_path, claims):
    repo = make_repo(
        tmp_path,
        yaml.safe_load("switches: {toggle: [label, on]}"),
        {"spec.md": block(claims)},
    )
    result = run_data_model_gate(str(repo)).to_dict()
    assert result["status"] == "pass"
    assert result["gaps"] == []
    assert result["declared_mutations"] == {
        "spec": claims,
        "model": {"switches": {"toggle": ["label", "true"]}},
    }
    assert result["warnings"] == [
        "spec-mutations W1: model operation 'switches.toggle' or fields "
        + ("['true']" if claims else "['label', 'true']")
        + " are not claimed by the spec"
    ]
    expected = json.dumps(result)
    assert json.dumps(run_data_model_gate(str(repo)).to_dict()) == expected
    for seed in ("0", "73"):
        process = subprocess.run(
            [sys.executable, "-m", "factory.datamodel", "--repo", str(repo)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert process.returncode == 0, (process.stdout, process.stderr)
        assert json.loads(process.stdout) == result


@pytest.mark.parametrize("spec_kind", ["none", "prose", "empty", "matching"])
@pytest.mark.parametrize(
    "model_yaml,canonical",
    [
        (
            "switches: {toggle: [label, on]}",
            {"switches": {"toggle": ["label", "true"]}},
        ),
        (
            "yes: {edit: [value]}\norders: {edit: [value]}",
            {"true": {"edit": ["value"]}, "orders": {"edit": ["value"]}},
        ),
        (
            "orders: {edit: [value, 2026-01-01]}",
            {"orders": {"edit": ["2026-01-01", "value"]}},
        ),
        (
            "orders: {edit: [value, 2026-01-01 10:00:00]}",
            {"orders": {"edit": ["2026-01-01T10:00:00", "value"]}},
        ),
        (
            "orders: {edit: [value, !!binary aGVsbG8=]}",
            {"orders": {"edit": ["base64:aGVsbG8=", "value"]}},
        ),
    ],
)
def test_canonical_scalars_json_api_cli(tmp_path, spec_kind, model_yaml, canonical):
    model = yaml.safe_load(model_yaml)
    specs = {
        "none": None,
        "prose": {"spec.md": "A catalog of records.\n"},
        "empty": {"spec.md": block({})},
        # Escape the operation spelling to avoid BASE's out-of-scope prose
        # re.escape crash on non-string fields next to the verb "edit".
        "matching": {"spec.md": block(model).replace("edit:", '"\\u0065dit":')},
    }
    repo = make_repo(tmp_path, model, specs[spec_kind])
    result = run_data_model_gate(str(repo)).to_dict()
    assert json.loads(json.dumps(result, sort_keys=True)) == result
    assert json.loads(json.dumps(result)) == result
    assert result["status"] == "pass", result["gaps"]
    assert result["gaps"] == []
    assert result["declared_mutations"]["model"] == canonical
    if spec_kind == "empty":
        assert result["declared_mutations"]["spec"] == {}
        assert result["warnings"] == [
            f"spec-mutations W1: model operation '{name}.{op}' or fields "
            f"{fields} are not claimed by the spec"
            for name, ops in sorted(canonical.items())
            for op, fields in sorted(ops.items())
        ]
    else:
        assert result["warnings"] == []
        assert result["detail"] == "data model gate passed"
        assert result["declared_mutations"]["spec"] == (
            canonical if spec_kind == "matching" else None
        )
    outputs = []
    for seed in ("0", "73"):
        process = subprocess.run(
            [sys.executable, "-m", "factory.datamodel", "--repo", str(repo)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert process.returncode == 0, (process.stdout, process.stderr)
        assert json.loads(process.stdout) == result
        outputs.append(process.stdout)
    assert outputs[0] == outputs[1]


def test_canonical_collisions_merge_before_comparison(tmp_path):
    model = yaml.safe_load(
        'yes: {toggle: [on, value]}\n"true": {toggle: ["true", stamp]}'
    )
    claims = yaml.safe_load('yes: {toggle: [on]}\n"true": {toggle: [value, stamp]}')
    result = run_data_model_gate(
        str(make_repo(tmp_path, model, {"spec.md": block(claims)}))
    )
    assert result.status == "pass", result.gaps
    assert result.warnings == []
    expected = {"true": {"toggle": ["stamp", "true", "value"]}}
    assert result.to_dict()["declared_mutations"] == {
        "model": expected,
        "spec": expected,
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, "true"),
        (False, "false"),
        (None, "null"),
        (2, "2"),
        (2.5, "2.5"),
        (b"hello", "base64:aGVsbG8="),
    ],
)
def test_canonical_operation_and_protected_names(tmp_path, raw, expected):
    # Non-string model operations retain BASE's self-check limitation; use
    # a canonical string operation on D and a raw scalar operation on S.
    repo = make_repo(
        tmp_path,
        {"orders": {expected: ["value"]}},
        {"spec.md": block({"orders": {raw: ["value"]}})},
    )
    result = run_data_model_gate(str(repo))
    assert result.status == "pass", result.gaps
    assert result.warnings == []
    assert result.to_dict()["declared_mutations"]["spec"] == {
        "orders": {expected: ["value"]}
    }


def test_canonical_protected_fields_are_compared(tmp_path):
    repo = make_repo(
        tmp_path,
        {"orders": {"toggle": ["value"]}},
        {"spec.md": block({"orders": {"toggle": ["true"]}})},
    )
    doc = yaml.safe_load((repo / "data-model.yml").read_text())
    doc["entities"]["orders"]["immutable"] = [True]
    (repo / "data-model.yml").write_text(yaml.safe_dump(doc))
    result = run_data_model_gate(str(repo))
    assert prefixes(result) == {"spec-mutations G2", "spec-mutations G3"}


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
            "factory.cli",
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
