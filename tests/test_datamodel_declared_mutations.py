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
    (root / "probe.py").write_text("# Reference existence only; not storage evidence.\n")
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
