# Failure intake: provenance-checked incidents and inert proposals

Status: Stage A implemented (`factory-learning import|propose`). Deterministic
replay is Stage B and is **not** implemented here; this document covers intake
only. Nothing in this stage runs a command, opens a socket, creates a task, or
edits a skill, test, gate or policy.

Design: [failure-regression-design.md](failure-regression-design.md). Base
revision: `4c7b695` (branch `feat/failure-regression-20261023`).

## Commands

```
factory-learning import --record R.json --evidence-root DIR --store STORE [--json]
factory-learning propose --store STORE [--fingerprint FP | --content-id CID] [--json]
```

Exit codes: `0` imported/duplicate/proposed, `1` rejected or conflict, `2` usage
error. `--json` prints a closed-structure result and never a traceback.

`import` reads exactly one record file and the cited artifacts under
`--evidence-root`; it writes only under `--store` and resolves no URL.
`propose` reads only `--store` and writes one file under `<store>/proposals/`.

## Incident record (`factory-incident` v1)

```json
{"schema": {"name": "factory-incident", "version": 1},
 "title": "...",
 "symptom": {"summary": "...", "observed": "..."},
 "category": "implementation|specification|tooling_evidence|infrastructure|unknown",
 "root_cause": {"status": "unknown|supported", "statement": null, "evidence_refs": [0]},
 "sources": {"repository": "...", "revisions": ["<40hex>"], "repaired_revision": null},
 "evidence": [{"ref": "qc-engine/extra-results.json", "kind": "probe|log|report",
               "bytes": 1771, "sha256": "sha256:<64hex>"}],
 "hypothesis": "...",
 "improvement_class": "regression_test|spec_clarification|tooling_check|infra_change",
 "replay": {"suite_id": "duplicate-json-key",
            "target_nodeid": "test_x.py::test_y",
            "expect_baseline": "assertion_failure", "expect_candidate": "pass"},
 "corrects": null,
 "origin": {"kind": "reviewer|operator|machine", "ref": "t_..."}}
```

`fingerprint` and `content_id` are optional on input; when supplied they are
recomputed and must match (`invalid_incident_id`). They are always written on
the stored record:

| Field | Definition |
| --- | --- |
| `fingerprint` | `sha256:` over canonical JSON of the whitespace-collapsed, casefolded `symptom.summary`, the sorted `sources.revisions`, and the sorted evidence `sha256` set |
| `content_id` | `sha256:` over the canonical record minus both id fields |

Wording (`title`, `hypothesis`, `root_cause`) never moves the fingerprint, so
re-wording can neither fork nor silently merge one observable. It does change
`content_id`, which makes a re-worded record a `conflict` — history is never
rewritten. A record that sets `corrects: <existing content_id>` appends a linked
record under the same fingerprint; the cited id must already exist.

Category is declared, never inferred. `root_cause.status` stays `unknown` unless
a reviewer sets `supported` **with** a statement and in-range evidence refs.

## Storage

```
<store>/incidents/<fingerprint-hex>/<content-id-hex>.json
<store>/proposals/<proposal-id-hex>.json
<store>/locks/<fingerprint-hex>.lock
```

Records and proposals are created `O_CREAT|O_EXCL|O_NOFOLLOW` and never
rewritten. Imports are serialized per fingerprint with a blocking `flock`; the
lock is never stolen, timed out, or age-invalidated. Outcomes: new identity ->
`imported`; identical content -> `duplicate` (exit 0, byte-identical store);
different content without `corrects` -> `conflict` (exit 1, prior record and
listing untouched); valid `corrects` -> append.

## Proposal (`factory-proposal` v1) — not authorization

`propose` requires a `replay` block (`no_executable_criterion` otherwise),
re-validates the stored record's recomputed ids and content, and renders:

* `status: "pending_review"` — never approved, never dispatched;
* `incident` citation (fingerprint, content id, title, category, class);
* `diagnosis` copied verbatim from `root_cause` (never upgraded);
* `hypothesis` verbatim;
* `executable_criteria` from `incident.replay`, including the rule that
  both-passing is not improvement;
* `replay_request` as inert data that needs a separate explicit
  `--approve-execution` (Stage B);
* `authorization: {"grants": [], "note": "no promotion, dispatch, deployment or
  policy change"}` plus `declared_non_actions`.

`proposal_id` is the sha256 of the canonical body, so re-running `propose`
produces the same file and reports `duplicate`.

## Admission and rejection reasons

| Reason | Trigger |
| --- | --- |
| `record_invalid` | record missing, a symlink, or not a regular file |
| `record_too_large` | record exceeds 256 KiB |
| `malformed_record` | invalid UTF-8, duplicate JSON key, deep nesting, wrong type, unknown key, missing key, bad schema, bad enum member/value, bad size/count |
| `invalid_category` / `invalid_improvement_class` | unknown declared category or class |
| `invalid_root_cause` | unknown status, `unknown` carrying a statement, `supported` without refs, out-of-range refs |
| `invalid_incident_id` | supplied or stored id does not match recomputed content; ambiguous selector |
| `path_escape` | absolute, `..`, empty-component, scheme, symlink-crossing or out-of-root evidence ref |
| `evidence_missing` | cited artifact does not exist |
| `evidence_hash_mismatch` | byte count or sha256 differs from the citation |
| `evidence_too_large` | cited artifact exceeds 8 MiB |
| `store_invalid` | store is a symlink or file, sits at `/`, the home directory, an immediate home directory or a sensitive system root, or overlaps the evidence root |
| `store_tampered` | planted symlink, id/filename mismatch, unreadable stored record, or a non-identical existing file at a computed path |
| `invalid_correction` | `corrects` cites a content id absent from this fingerprint |
| `no_executable_criterion` | proposal requested for a record without `replay` |
| `conflict` / `ambiguous_selection` | competing content, or several incidents and no selector |

Every rejection is returned before a write; tests assert the store (or its
absence) is byte-identical after each failure, including concurrent conflicts.

## Limits (stated, not solved)

* Sensitive-content screening is **best effort**: bounded text fields reject
  control characters and PEM/secret-shaped material, but import only reads the
  explicit record and cited artifacts. There is no host secret scan and no
  transcript/env auto-ingest.
* Records are immutable by convention and `O_EXCL`; a same-UID actor who can
  rewrite the store is outside the boundary, exactly as in
  [evidence-gate.md](evidence-gate.md).
* Incident prose is inert data. It is never executed, expanded, or treated as
  instructions; no subprocess, network, Kanban, skill, test, gate or policy
  action exists on the import/propose path.
* Identity covers one observable, not one root cause: a passing replay (Stage B)
  proves coverage of the observable, not that the stated cause is right.
* The store is plain files. There is no database, daemon, scheduler, LLM call,
  deployment or push in this stage.
