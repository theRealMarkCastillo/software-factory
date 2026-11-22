"""Seeded exact-set oracle over valid models and arbitrary structured claims."""

import random

from factory.datamodel import run_data_model_gate
from tests.test_datamodel_declared_mutations import block, make_repo, prefixes

SEED = 40621
ITERATIONS = 600


def test_random_claims_fail_iff_a_declared_mutation_gap_exists(tmp_path):
    rng = random.Random(SEED)
    seen = set()
    for case in range(ITERATIONS):
        model = {}
        claims = {}
        for name in ("orders", "notes", "ledger"):
            if rng.choice([True, False]):
                model[name] = {
                    op: rng.sample(["value", "revision"], rng.randrange(3))
                    for op in ("edit", "revise")
                    if rng.choice([True, False])
                }
            if rng.choice([True, False]):
                claims[name] = {
                    op: rng.sample(
                        ["value", "revision", "id", "created", "extra"],
                        rng.randrange(6),
                    )
                    for op in ("edit", "revise", "archive")
                    if rng.choice([True, False])
                }
        if not model:
            model["orders"] = {}
        expected = set()
        for name, ops in claims.items():
            if name not in model:
                expected.add("spec-mutations G1")
                continue
            if not ops and model[name]:
                expected.add("spec-mutations G4")
            for op, fields in ops.items():
                if set(fields) & {"id", "created"}:
                    expected.add("spec-mutations G2")
                if op not in model[name] or set(fields) - set(model[name].get(op, [])):
                    expected.add("spec-mutations G3")
        repo = make_repo(tmp_path, model, {"spec.md": block(claims)})
        result = run_data_model_gate(str(repo))
        context = (SEED, case, model, claims, result.to_dict())
        assert prefixes(result) == expected, context
        assert (result.status == "fail") == bool(expected), context
        seen.add(result.status)
    assert seen == {"pass", "fail"}
