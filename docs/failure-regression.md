# Failure intake and diagnostic replay

Status: Stage A (`factory-learning import|propose`) and Stage B
(`factory-learning replay`) implemented. A proposal remains pending review and
grants nothing. A replay report is **diagnostic, non-promotional** evidence: it
is not acceptance evidence, it is never a certificate, and `factory-verify` /
`factory-promote` reject it.

Replay is *historical reproduction* of one observable with an evaluator-owned
suite. It is not original TDD provenance, not a statement about root cause, and
not evidence of model quality, speed or cost improvement.

Design: [failure-regression-design.md](failure-regression-design.md). Base
revision: `4c7b695` (branch `feat/failure-regression-20261023`).

## Commands

```
factory-learning import --record R.json --evidence-root DIR --store STORE [--json]
factory-learning propose --store STORE [--fingerprint FP | --content-id CID] [--json]
factory-learning replay --repo P --baseline SHA --candidate SHA \
    --suite-dir D --suite-digest sha256:... --work-root R --out O \
    --approve-execution [--timeout-s N] [--json]
```

Import/propose exit codes: `0` imported/duplicate/proposed, `1` rejected or
conflict, `2` usage error. Replay exit codes: `0` reproduced, `1`
not_reproduced, `2` rejected (admission/binding), `3` inconclusive (timeout,
harness/collection failure, malformed or contradictory reports). `--json`
prints a closed-structure object and never a traceback.

`import` reads exactly one record file and the cited artifacts under
`--evidence-root`; it writes only under `--store` and resolves no URL.
`propose` reads only `--store` and writes one file under `<store>/proposals/`.
`replay` reads the local git object database, the external suite and the
explicit work root; it performs no fetch, opens no socket and never invokes
Docker.


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
`imported`; identical content -> `duplicate` (exit 0, no record or proposal is
created or rewritten); different content without `corrects` -> `conflict`
(exit 1, prior record and listing untouched); valid `corrects` -> append.

A rejected import never writes, rewrites or removes a record or a proposal.
One narrow exception to "nothing on disk changed": a rejection that happens
after the per-fingerprint lock is taken (conflict, invalid correction, store
tampering) may leave an empty `<store>/locks/<fingerprint-hex>.lock` file
behind, because the lock directory and lock file are created before the record
directory is touched. Rejections that fail during store admission (for
example a symlinked, sensitive, overlapping or inaccessible store path) create
nothing at all and return a structured `store_invalid`. Duplicate idempotence
is unaffected: a repeated identical import still creates and rewrites no
record or proposal.

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

## Replay: one evaluator-owned suite, two revisions

Admission is explicit and ordered; a refusal happens before any Git read or
process spawn:

* `--approve-execution` is required (without it, exit 2, `approval_required`).
* Both revisions are full 40-hex lowercase SHAs present in the local object
  database, and they differ. No fetch is performed.
* `--suite-digest` is a **required external** `sha256:<64 hex>`.
* `--repo` is admitted by the factory's safe-repo-root policy; `--work-root` is
  an existing empty directory outside the repository, suite and client home
  root; `--out` must not exist and must not overlap them.
* The suite lives outside `--repo` and the work root, is `<= 32` files,
  `<= 256 KiB` each and `<= 1 MiB` total, has no symlinks, and contains only
  `pytest.ini`, `test_*.py` and `expectations.json`.

`expectations.json` (`factory-replay-expectations` v1) declares exactly one
target and one or more controls:

```json
{"schema": {"name": "factory-replay-expectations", "version": 1},
 "suite_id": "duplicate-json-key",
 "suite_digest": "sha256:<64hex>",
 "cases": [{"nodeid": "test_x.py::test_y",
            "baseline": {"outcome": "assertion_failure"},
            "candidate": {"outcome": "pass"}}],
 "controls": [{"nodeid": "test_x.py::test_control",
               "baseline": {"outcome": "pass"},
               "candidate": {"outcome": "pass"}}],
 "timeout_s": 120}
```

v1 replays exactly `assertion_failure -> pass`; a suite that declares anything
else (including pass/pass) is `unsupported_suite`. Each side runs in a
`git worktree add --detach` checkout; generated checkouts are **retained** (no
destructive cleanup). The engine records the requested SHA, the checkout HEAD
before and after, and the suite digest before execution, between sides and after
execution; any drift is `rejected`.

### The digest is non-circular

`suite_digest` is `sha256:` over canonical JSON of the sorted
`[{name, bytes, sha256}]` rows. For `expectations.json` the row hashes the
canonical JSON of the parsed expectation object **without** the `suite_digest`
field, so the file can carry its digest without hashing itself. The suite's own
recorded digest must equal the recomputed one and the recomputed one must equal
the caller's `--suite-digest`; editing expectations cannot authorize a new
oracle.

### Execution and the trusted phase report

Each side runs the engine-owned bootstrap
(`factory/_replay_child.py`), which removes its own source directory from
`sys.path`, puts the checkout first, installs a **checkout import guard**
(`factory/_replay_isolation.py`) that refuses to satisfy a missing `factory`
submodule from the editable engine, loads `factory/replay_plugin.py` by absolute
path, and calls `pytest.main(..., plugins=[recorder])`. Pytest runs with
`-c pytest.ini --noconftest --rootdir . -p no:cacheprovider`, with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, a scrubbed environment (no
`PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, proxies or credentials), and a per-side
`HOME`/`TMPDIR` inside the report directory. A preflight proves
`factory.__file__` belongs to the checkout. Candidate commits therefore cannot
contribute a plugin, conftest or ini option that changes the outcome.

The plugin records pytest's own per-node **setup / call / teardown** outcomes
and the real exception class, writes one nonce-bound JSON report, and is loaded
outside both candidate trees. **Stdout is never parsed for status**; injected
`PASSED`/`FAILED` text changes nothing. A verdict is `reproduced` only when:

* both reports are present, nonce-bound, from the trusted plugin, with an exit
  status that agrees with the process exit;
* the collected node set is exactly the declared target plus controls (no
  missing, duplicate or unexpected node);
* no collection error, skip/xfail, setup/teardown failure or non-`AssertionError`
  call failure occurred;
* the target is call-phase `AssertionError` on baseline and `pass` on candidate;
* every control passes on both sides;
* the suite digest is unchanged before, between and after execution.

Everything else is `not_reproduced` (well-formed but criteria unmet, including
both-pass) or `inconclusive` (timeout, collection/harness failure, malformed or
contradictory report). A nonzero exit alone never counts. On timeout the engine
kills only the child's own process group (`start_new_session` + `SIGKILL`) and
reaps it; no fixture child survives.

### Report and gate interaction

`<out>/replay-report.json` plus `replay-report.sha256` carry the verdict,
reason, both requested SHAs, suite id/digest plus per-side digests, exact
commands, exit codes, log/report hashes, per-node outcomes and the check list.
The report states `"promotional": false` and `"acceptance_evidence": false` and
is **not** an acceptance certificate: `factory-verify --artifacts-dir <out>`
returns `manifest_missing`, and `factory-promote` returns
`evidence_rejected:manifest_missing` without moving the target ref. Baseline RED
remains diagnostic only; no verifier mode accepts it.

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
* Replay is **not a sandbox**. Candidate code runs as the same UID with the host
  filesystem and network reachable; the checkout import guard, `--noconftest`,
  explicit config and scrubbed environment isolate pytest *configuration*, not
  processes. Only reviewed suites at reviewed revisions may be replayed.
* Generated replay checkouts are retained on purpose (v1 does no risky cleanup).
  The work root must be an explicit empty directory; the operator owns its
  later disposal.
* Report hashes bind bytes to digests, not authorship. A same-UID actor who can
  rewrite the report or store is outside the integrity boundary, as in
  [evidence-gate.md](evidence-gate.md).
* Deterministic Python cases only. Service-dependent, stochastic, timing,
  model-quality and cost claims are out of scope; one run proves coverage of one
  observable, not a root cause.
