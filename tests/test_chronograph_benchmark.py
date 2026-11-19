"""Portable integrity checks for the published, frozen benchmark kit."""

import hashlib
import json
import re
import tomllib
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "benchmarks" / "chronograph"


def test_chronograph_benchmark_is_complete_consistent_and_public():
    expected = {
        "README.md",
        "spec/00-master-spec-as-received.md",
        "spec/01-ratified-decisions.md",
        "spec/02-ui-contract.md",
        "contracts/openapi.yaml",
        "fixtures/demo-seed.json",
        "fixtures/demo-seed-oracle.json",
        "fixtures/SHA256SUMS",
    }
    assert KIT.is_dir(), "ChronoGraph benchmark kit is missing"
    assert {str(p.relative_to(KIT)) for p in KIT.rglob("*") if p.is_file()} == expected

    # Source commit 2697d343b0b6005a1b0c5e9575a1702e1173885b:
    # docs/spec/00-master-spec-as-received.md, with no sanitization needed.
    preserved_spec = "benchmarks/chronograph/spec/00-master-spec-as-received.md"
    assert hashlib.sha256((ROOT / preserved_spec).read_bytes()).hexdigest() == (
        "959c4e2ce2004401b97901c533eb8c1c8e50a0eaabf690c81c1395c4b922fd96"
    )
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["ruff"].get("extend-exclude") == [preserved_spec]

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
