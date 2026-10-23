"""
Data Model Gate for the Software Factory.

Pre-build gate for any spec that describes entities with a lifecycle
(versions, revisions, edits, corrections, approvals, states). A behavioral
spec can be complete, internally consistent, and fully fixture-covered while
remaining silent on where each field physically lives in storage: which fields
define a row's identity, and which fields a "revision"/"correction" is actually
allowed to change. When that is left silent the build must infer it, and a
coherent, passable inference can still be structurally incompatible with the
spec -- e.g. a "revision" that is supposed to change a value, built on a schema
where that value is part of what makes two rows "the same record", so no
revision can ever actually change it. Every fixture that tests revision
*semantics* in isolation passes while the write path is incapable of a value
ever changing.

This gate is self-contained: it reads a ``data-model.yml`` at the repo root
that declares, per entity with a lifecycle:

    identity       fields that define which logical record a row belongs to.
    mutable        operation -> list of fields that operation may change while
                   staying the same record (revision/edit/correction/update).
    immutable      fields set once, never touched by any later operation.
    append_only    child collections added as new rows, never updated in place.

and enforces:

    1. identity fields are never also mutable (the structural incompatibility
       above is a BLOCKING gap, not an implicit default).
    2. immutable fields are never also mutable.
    3. for every entity with a lifecycle, the required test entry points are
       declared and the referenced test files/functions actually exist:
         - round_trip        (write a mutable field, read it back via a separate
                              read call, confirm the value changed and identity
                              fields did not)
         - immutability      (a write to a never-mutated field is rejected at
                              the storage layer, not merely absent from code)
         - concurrent_append (two near-simultaneous appends to an append-only
                              child both survive; neither clobbers the other)

Like factory.audit, this module returns a flat result and never raises on
non-policy conditions, so the orchestrator can read the result without
exception handling.

Optional behavioral-spec cross-check: if a ``spec.md`` (or files listed in a
top-level ``specs:`` key, or passed via ``--spec``) names a mutation of a known
field that is identity/immutable rather than mutable, the gate raises a
BLOCKING gap instead of letting the build silently infer a schema where the
write path cannot change the value. The scan is a proximity heuristic: for each
field mention it evaluates the *governing* mutation verb (nearest at or before
it), so a mixed-polarity sentence -- "a revision may change the value, but must
not change fact_id" -- flags neither half incorrectly. It is still a heuristic:
generic field names ("value", "status") in unrelated prose can over-flag, and
every gap should be human-reviewed.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DATA_MODEL_FILENAME = "data-model.yml"

# Required test kinds and the precondition that makes each mandatory for an
# entity. round_trip: entity has sanctioned mutations; immutability: entity
# declares never-mutated fields; concurrent_append: entity has append-only
# children.
REQUIRED_TEST_KINDS = ("round_trip", "immutability", "concurrent_append")

# Behavioral-spec cross-check. If a spec file names a mutation of a known
# field that the data model does NOT place in a mutable-via-operation category
# (i.e. it is identity or immutable), that is a BLOCKING gap -- the build must
# not silently infer a schema where the write path cannot actually change the
# value. The scan pairs each field with its governing mutation verb on the same
# line. It is deliberately conservative (may over-flag, never under-flags the
# identity/immutable case), and every gap it raises should be human-reviewed.
DEFAULT_SPEC_FILENAME = "spec.md"

MUTATION_VERB_PATTERN = re.compile(
    r"\b(change(?:s|d)?|changing|"
    r"correct(?:s|ed|ing|ions?)|"
    r"revis(?:e|es|ed|ing|ions?)|"
    r"update(?:s|d)?|updating|"
    r"edit(?:s|ed|ing)?|"
    r"modif(?:y|ies|ied|ying|ications?)|"
    r"replace(?:s|d)?|replacing|"
    r"amend(?:s|ed|ing|ments?)?)\b",
    re.IGNORECASE,
)

# Negation tokens that, when they immediately precede a mutation verb, mean the
# spec is asserting a field does NOT change (e.g. "identity fields did not
# change"), so it must not be treated as a mutation claim.
NEGATION_WORDS = {
    "not",
    "never",
    "no",
    "without",
    "unchanged",
    "can't",
    "cannot",
    "cant",
    "won't",
    "wont",
    "shouldn't",
    "shouldnt",
    "mustn't",
    "mustnt",
    "isn't",
    "isnt",
    "doesn't",
    "doesnt",
    "don't",
    "dont",
}


@dataclass
class DataModelGateResult:
    status: str  # "pass" | "fail" | "no_document" | "unavailable"
    entities: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    detail: str = ""
    self_checks: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "entities": self.entities,
            "gaps": self.gaps,
            "detail": self.detail,
            "self_checks": self.self_checks,
        }


def _as_list(value) -> list | None:
    """Normalize a single scalar to a list; return None if not list/scalar."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return None


def _validate_entity(name: str, ent: dict, gaps: list[str]) -> None:
    if not isinstance(ent, dict):
        gaps.append(
            f"entity '{name}': must be a mapping of fields, got {type(ent).__name__}"
        )
        return

    identity = _as_list(ent.get("identity"))
    if identity is None:
        gaps.append(f"entity '{name}': 'identity' must be a list of field names")
        identity = []
    if not identity:
        gaps.append(f"entity '{name}': missing 'identity' fields")

    immutable = _as_list(ent.get("immutable"))
    if immutable is None:
        gaps.append(f"entity '{name}': 'immutable' must be a list of field names")
        immutable = []

    mutable = ent.get("mutable") or {}
    if not isinstance(mutable, dict):
        gaps.append(
            f"entity '{name}': 'mutable' must be a mapping of operation -> fields "
            f"(e.g. revision: [value])"
        )
        mutable = {}

    all_mutable: set[str] = set()
    for op, fields in mutable.items():
        fields_list = _as_list(fields)
        if fields_list is None:
            gaps.append(f"entity '{name}': operation '{op}' fields must be a list")
            continue
        all_mutable |= set(fields_list)

    # Core invariant 1: identity fields must never be mutable. This is the
    # structural incompatibility a fixture-driven build silently infers wrong.
    identity_overlap = set(identity) & all_mutable
    if identity_overlap:
        gaps.append(
            f"entity '{name}': identity field(s) {sorted(identity_overlap)} are also "
            f"mutable-via-operation -- no revision/correction can ever change them "
            f"(BLOCKING gap: revise the schema so identity and mutable fields are disjoint)"
        )

    # Core invariant 2: never-mutated fields must never be mutable.
    immutable_overlap = set(immutable) & all_mutable
    if immutable_overlap:
        gaps.append(
            f"entity '{name}': immutable field(s) {sorted(immutable_overlap)} are also "
            f"mutable-via-operation -- violates the never-mutated invariant"
        )

    append_only = ent.get("append_only") or {}
    if isinstance(append_only, list):
        append_only = {c: True for c in append_only}
    if not isinstance(append_only, dict):
        gaps.append(
            f"entity '{name}': 'append_only' must be a mapping of collection -> true "
            f"(or a list of collection names)"
        )
        append_only = {}

    # Required tests, driven by which lifecycle features the entity declares.
    tests = ent.get("tests") or {}
    if not isinstance(tests, dict):
        gaps.append(
            f"entity '{name}': 'tests' must be a mapping of test kind -> reference"
        )
        tests = {}

    if mutable and "round_trip" not in tests:
        gaps.append(
            f"entity '{name}': declares mutable operations but no 'tests.round_trip' "
            f"(a write-then-read probe is the only evidence a write took effect)"
        )
    if immutable and "immutability" not in tests:
        gaps.append(
            f"entity '{name}': declares immutable fields but no 'tests.immutability' "
            f"(rejection must be proven at the storage layer, not by code absence)"
        )
    if append_only and "concurrent_append" not in tests:
        gaps.append(
            f"entity '{name}': declares append-only children but no "
            f"'tests.concurrent_append' (two appends must both survive)"
        )


def _verify_test_refs(
    repo: Path, entity_name: str, tests: dict, gaps: list[str]
) -> None:
    for kind, ref in tests.items():
        if kind not in REQUIRED_TEST_KINDS:
            gaps.append(
                f"entity '{entity_name}': unknown test kind '{kind}' "
                f"(expected one of {', '.join(REQUIRED_TEST_KINDS)}) -- a typo here "
                f"silently leaves a required test undeclared"
            )
            continue
        if not isinstance(ref, str) or not ref:
            gaps.append(
                f"entity '{entity_name}': test '{kind}' reference must be a path::function string"
            )
            continue
        path_part, _, node = ref.partition("::")
        test_file = repo / path_part
        if not test_file.is_file():
            gaps.append(
                f"entity '{entity_name}': test '{kind}' references missing file '{path_part}'"
            )
            continue
        if node:
            try:
                text = test_file.read_text()
            except OSError as exc:  # pragma: no cover - defensive
                gaps.append(
                    f"entity '{entity_name}': cannot read test file '{path_part}': {exc}"
                )
                continue
            if f"def {node}" not in text:
                gaps.append(
                    f"entity '{entity_name}': test '{kind}' function '{node}' not found in '{path_part}'"
                )


def _mutation_verbs(line: str) -> list[tuple[int, bool]]:
    """Return (position, negated) for every mutation verb on a line.

    Negated means a negation token sits within the three words before the verb
    ("identity fields did not change"), so the clause is asserting the field
    does NOT change.
    """
    out: list[tuple[int, bool]] = []
    for m in MUTATION_VERB_PATTERN.finditer(line):
        prev = line[: m.start()].lower().split()
        negated = any(w.rstrip(".,;:!?") in NEGATION_WORDS for w in prev[-3:])
        out.append((m.start(), negated))
    return out


def _governing_verb(
    verbs: list[tuple[int, bool]], field_pos: int
) -> tuple[int, bool] | None:
    """The mutation verb that governs a field mention: the nearest one at or
    before it, else the nearest one after it (for "the fact_id may be changed").

    Evaluating the *governing* verb rather than the whole line is what keeps a
    mixed-polarity sentence -- "a revision may change the value, but must not
    change fact_id" -- from false-flagging the negated half.
    """
    before = [v for v in verbs if v[0] <= field_pos]
    if before:
        return max(before, key=lambda v: v[0])
    return min(verbs, key=lambda v: v[0]) if verbs else None


def _resolve_spec_files(doc: dict, spec_files) -> tuple[list[str], bool]:
    """Return (relative spec paths, explicit).

    ``explicit`` is True when the caller or the document declared the files
    (a declared-but-missing file is a gap); False for the implicit default
    ``spec.md`` (missing is silently skipped).
    """
    if spec_files is not None:
        raw, explicit = spec_files, True
    else:
        raw = doc.get("specs")
        explicit = raw is not None
        if raw is None:
            return [DEFAULT_SPEC_FILENAME], False
    if isinstance(raw, str):
        return [raw], explicit
    if isinstance(raw, list):
        return [str(x) for x in raw], explicit
    return [], explicit


def _cross_check_specs(
    repo: Path, entities: dict, spec_files: list[str], explicit: bool, gaps: list[str]
) -> None:
    """Flag spec statements that mutate a field the data model does not allow
    to change (identity or immutable), per the Data Model Gate's escalation."""
    vocab: dict[str, dict[str, bool]] = {}
    for name, ent in entities.items():
        if not isinstance(ent, dict):
            continue
        identity = set(_as_list(ent.get("identity")) or [])
        immutable = set(_as_list(ent.get("immutable")) or [])
        mutable = ent.get("mutable") or {}
        all_mutable: set[str] = set()
        if isinstance(mutable, dict):
            for fields in mutable.values():
                all_mutable |= set(_as_list(fields) or [])
        for f in identity | immutable | all_mutable:
            vocab.setdefault(f, {})[name] = f in all_mutable

    for rel in spec_files:
        spec_path = repo / rel
        if not spec_path.is_file():
            if explicit:
                gaps.append(f"spec file '{rel}' declared but not found")
            continue
        try:
            text = spec_path.read_text()
        except OSError as exc:  # pragma: no cover - defensive
            gaps.append(f"cannot read spec file '{rel}': {exc}")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            verbs = _mutation_verbs(line)
            if not verbs:
                continue
            for field_name, per_entity in vocab.items():
                match = re.search(rf"\b{re.escape(field_name)}\b", line, re.IGNORECASE)
                if not match:
                    continue
                governing = _governing_verb(verbs, match.start())
                if governing is None or governing[1]:
                    continue  # no governing verb, or the clause is negated
                offenders = sorted(n for n, is_mut in per_entity.items() if not is_mut)
                for n in offenders:
                    gaps.append(
                        f"spec file '{rel}' line {lineno} mutates field '{field_name}' of "
                        f"entity '{n}', but '{field_name}' is not in that entity's "
                        f"mutable-via-operation category (BLOCKING gap)"
                    )


def run_data_model_gate(
    repo_path: str,
    filename: str = DATA_MODEL_FILENAME,
    spec_files=None,
) -> DataModelGateResult:
    """Validate a repo's data-model.yml against the Data Model Gate rules.

    Never raises on non-policy conditions; reports status instead.
    """
    repo = Path(repo_path).expanduser().resolve()
    doc_path = repo / filename
    if not doc_path.is_file():
        return DataModelGateResult(
            "no_document",
            detail=f"no '{filename}' at {doc_path} -- a data model document is required "
            f"before implementation begins",
        )

    if yaml is None:
        return DataModelGateResult(
            "unavailable", detail="pyyaml is required to parse the data model document"
        )

    try:
        doc = yaml.safe_load(doc_path.read_text()) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return DataModelGateResult(
            "unavailable", detail=f"could not parse '{filename}': {exc}"
        )

    if not isinstance(doc, dict):
        return DataModelGateResult(
            "unavailable",
            detail=f"'{filename}' must contain a mapping at the top level",
        )

    entities = doc.get("entities") or {}
    if not isinstance(entities, dict):
        return DataModelGateResult(
            "unavailable",
            detail="'entities' must be a mapping of entity name -> fields",
        )
    if not entities:
        return DataModelGateResult(
            "fail", detail="data model declares no entities -- nothing to gate"
        )

    spec_list, spec_explicit = _resolve_spec_files(doc, spec_files)

    gaps: list[str] = []
    for name, ent in entities.items():
        _validate_entity(name, ent, gaps)
        _verify_test_refs(repo, name, (ent or {}).get("tests") or {}, gaps)
    _cross_check_specs(repo, entities, spec_list, spec_explicit, gaps)

    # The gate's closing question, per entity: a green suite is not evidence a
    # write took effect; only a write-then-read probe is.
    self_checks = [self_check_question(n, e or {}) for n, e in entities.items()]

    if gaps:
        return DataModelGateResult(
            "fail",
            entities=list(entities),
            gaps=gaps,
            detail=f"{len(gaps)} gap(s) found",
            self_checks=self_checks,
        )
    return DataModelGateResult(
        "pass",
        entities=list(entities),
        detail="data model gate passed",
        self_checks=self_checks,
    )


def self_check_question(entity_name: str, entity: dict) -> str:
    """The final build-complete question for one entity, per the gate's self-check."""
    ops = ", ".join((entity or {}).get("mutable", {}).keys()) or "N/A"
    return (
        f"entity '{entity_name}': if you write a new value through the sanctioned "
        f"({ops}) path and then read the entity back through a completely separate "
        f"read call, does the new value come back? (verified only by an actual "
        f"write-then-read probe, not by test coverage or a green CI run)"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the factory Data Model Gate on a repo."
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file", default=DATA_MODEL_FILENAME)
    parser.add_argument(
        "--spec",
        action="append",
        default=None,
        dest="spec_files",
        help="Behavioral spec file to cross-check (repeatable; overrides data-model.yml 'specs').",
    )
    args = parser.parse_args(argv)

    result = run_data_model_gate(
        args.repo, filename=args.file, spec_files=args.spec_files
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
