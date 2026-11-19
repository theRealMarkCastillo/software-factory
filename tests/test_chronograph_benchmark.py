"""Portable integrity checks for the published, frozen benchmark kit."""

import hashlib
import json
import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "benchmarks" / "chronograph"


ORIGINAL_SHA256 = "4b00410e74d5d07f500680f6d598e8e3899803207af750ea2d562cf2c062e256"
# Construct the historical name only for the baseline Git lookup and absence check.
HISTORICAL_NAME = "00-" + "master-spec-as-received.md"


def test_original_is_byte_identical_to_owner_handoff():
    original = KIT / "spec/00-original.md"
    assert original.is_file()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == ORIGINAL_SHA256
    assert (KIT / "spec/SHA256SUMS").read_text() == (
        f"{ORIGINAL_SHA256}  spec/00-original.md\n"
    )


def test_condensed_body_preserves_baseline_bytes_after_eight_line_preamble():
    baseline = subprocess.check_output(
        ["git", "show", f"4961360:benchmarks/chronograph/spec/{HISTORICAL_NAME}"],
        cwd=ROOT,
    )
    condensed = KIT / "spec/00-condensed-spec.md"
    assert condensed.is_file()
    lines = condensed.read_bytes().splitlines(keepends=True)
    assert b"".join(lines[8:]) == b"".join(baseline.splitlines(keepends=True)[8:])
    preamble = b"".join(lines[:8]).decode()
    for phrase in (
        "condensed working copy",
        "00-original.md",
        "reference run",
        "amendments interleaved",
        "01-ratified-decisions.md",
        "overrides both",
    ):
        assert phrase in preamble


def test_spec_provenance_matches_both_files():
    path = KIT / "spec/provenance.json"
    assert path.is_file()
    provenance = json.loads(path.read_text())
    assert provenance["original"] == {
        "file": "00-original.md",
        "source_name": "ChronoGraph_ Master Handoff & Architecture Specification.md",
        "sha256": ORIGINAL_SHA256,
        "source_url": "https://github.com/eidolonlabs-ai/hermes-factory/blob/"
        "c2021211962ab3def7598cdcf75d7b844b3ac5a0/benchmarks/chronograph/input/original.md",
        "source_commit": "c2021211962ab3def7598cdcf75d7b844b3ac5a0",
    }
    assert provenance["condensed"] == {
        "file": "00-condensed-spec.md",
        "sha256": hashlib.sha256(
            (KIT / "spec/00-condensed-spec.md").read_bytes()
        ).hexdigest(),
        "derived_from": "00-original.md",
        "relationship": "Condensed working copy used to drive the reference run; "
        "amendments interleaved; 01-ratified-decisions.md overrides both.",
    }


def test_no_obsolete_spec_filename_references_remain():
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    for name in tracked.decode().split("\0"):
        path = ROOT / name
        if name and path.is_file():
            assert HISTORICAL_NAME.encode() not in path.read_bytes(), name
    assert not (KIT / "spec" / HISTORICAL_NAME).exists()


def test_chronograph_benchmark_is_complete_consistent_and_public():
    expected = {
        "README.md",
        "spec/00-original.md",
        "spec/00-condensed-spec.md",
        "spec/.gitattributes",
        "spec/provenance.json",
        "spec/SHA256SUMS",
        "spec/01-ratified-decisions.md",
        "spec/02-ui-contract.md",
        "contracts/openapi.yaml",
        "fixtures/demo-seed.json",
        "fixtures/demo-seed-oracle.json",
        "fixtures/SHA256SUMS",
    }
    assert KIT.is_dir(), "ChronoGraph benchmark kit is missing"
    assert {str(p.relative_to(KIT)) for p in KIT.rglob("*") if p.is_file()} == expected

    preserved_spec = "benchmarks/chronograph/spec/00-original.md"
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["ruff"].get("extend-exclude") == [preserved_spec]
    assert (KIT / "spec/.gitattributes").read_bytes() == b"00-original.md -text\n"

    sums = {}
    for line in (KIT / "fixtures/SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert name in expected
        assert hashlib.sha256((KIT / name).read_bytes()).hexdigest() == digest
        sums[name] = digest
    assert set(sums) == {"fixtures/demo-seed.json", "fixtures/demo-seed-oracle.json"}

    seed = json.loads((KIT / "fixtures/demo-seed.json").read_text())
    oracle = json.loads((KIT / "fixtures/demo-seed-oracle.json").read_text())
    # This oracle records counts, not a seed hash; SHA256SUMS binds both files.
    facts = []
    extracted = []
    functional = set(seed["functional_predicates"])
    for episode in seed["episodes"]:
        replacements = 0
        lines = episode["raw_text"].splitlines()
        for line in lines:
            subject, predicate, obj = (part.strip() for part in line[2:].split("|"))
            fact = {
                "key": f"F{len(facts) + 1}",
                "episode": episode["key"],
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "valid_from": episode["observed_at"],
                "valid_until": None,
                "status": "active",
                "superseded_by": None,
            }
            for old in facts:
                if (
                    predicate in functional
                    and (old["subject"], old["predicate"]) == (subject, predicate)
                    and old["status"] == "active"
                    and old["object"] != obj
                ):
                    old.update(
                        status="superseded",
                        valid_until=fact["valid_from"],
                        superseded_by=fact["key"],
                    )
                    replacements += 1
            facts.append(fact)
        extracted.append(
            {
                "episode": episode["key"],
                "extracted_count": len(lines),
                "conflicts_resolved_count": replacements,
            }
        )
    assert oracle["ledger_final"] == facts
    assert oracle["per_episode"] == extracted
    statuses = Counter(fact["status"] for fact in facts)
    entities = sorted({f[k] for f in facts for k in ("subject", "object")})
    chains = {
        (f["subject"], f["predicate"]) for f in facts if f["status"] == "superseded"
    }
    counts = {
        "episodes": len(seed["episodes"]),
        "facts": len(facts),
        "active": statuses["active"],
        "superseded": statuses["superseded"],
        "chains": len(chains),
        "entities": len(entities),
    }
    assert oracle["entities"] == entities
    assert oracle["seed_response"] == {"status": "ok", **counts}
    assert oracle["counts"] == {
        **counts,
        "neo4j": {
            "entity_nodes": len(entities),
            "relation_edges": len(facts),
            "fact_nodes": len(facts),
            "superseded_by_edges": statuses["superseded"],
        },
    }
    assert len(oracle["conflict_chains"]) == len(chains)

    api = yaml.safe_load((KIT / "contracts/openapi.yaml").read_text())
    assert api["openapi"] == "3.0.3"
    assert set(api["paths"]) == {
        "/api/health",
        "/api/episodes",
        "/api/graph",
        "/api/facts",
        "/api/conflicts",
        "/api/demo/seed",
    }

    # Character classes keep private identifiers out of public test literals.
    private = re.compile(
        r"/[U]sers/|/[h]ome/|[m]arkcastillo|[m]ac-mini|192[.]168[.]|"
        r"\b[r]eef\b|\b[A]rx\b|[a]rx-docs|[S]amantha|[E]lena|[R]achel|"
        r"[t]elegram|8788[2]36757|\b[\w-]+[.]local\b",
        re.IGNORECASE,
    )
    for path in (ROOT / "benchmarks").rglob("*"):
        if path.is_file():
            assert not private.search(path.read_text()), path.relative_to(ROOT)
