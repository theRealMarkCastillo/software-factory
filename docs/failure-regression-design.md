# Failure-to-regression design: incident intake and diagnostic replay

Status: accepted by Arx with the binding corrections below; design card `t_9ab43e5e`.

## Binding operator corrections (override conflicting text below)

1. Do NOT classify failures by grepping stdout. An engine-owned pytest reporting
   plugin outside subject trees must capture per-node setup/call/teardown outcomes
   and actual exception class via hooks. Only call-phase AssertionError in the
   expected target with clean setup/teardown and passing controls is reproduction.
   Missing/duplicate/unexpected nodes, skips/xfails, collection failure, malformed
   or incomplete report never reproduce. Exit and report must agree.
2. Disable plugin autoload and inherited pytest options. Use explicit trusted plugin,
   minimal documented environment, scrub credentials/PYTHONPATH/PYTEST_ADDOPTS.
   Preflight checks module.__file__, not bare __file__. This is NOT a hostile-code
   sandbox: explicit approval covers reviewed suite AND both source revisions.
3. Non-circular suite digest: canonical sorted file names/content hashes including
   expectations with only suite_digest omitted. Require external --suite-digest;
   verify before/between/after executions. The suite cannot rewrite expectations
   and its own digest to authorize changes. Target must transition assertion_failure
   to pass; both-pass is not improvement.
4. Serialize imports per fingerprint with exclusive coordination; no age-based lock
   stealing. Concurrent conflicting imports cannot both succeed. Validate supplied
   ids against recomputed values and stored content before proposal. Corrections
   cite existing same-incident content ids; no silent overwrite/history relinking.
5. Do not blanket-reject /Users or /home descendants using SENSITIVE_HOST_ROOTS.
   Reuse safe-root semantics for ordinary subdirectories, reject actual root/secret
   paths and symlink escapes. Validate store ancestors and generated paths; never
   follow planted incidents/proposals symlinks.
6. Use safe git-managed checkouts or validate archive members before extraction.
   Work only under explicit owned empty root. Retain generated replay trees in v1
   rather than risky cleanup; no prune/deleting existing worktrees. Timeout kills
   and reaps only the launched process group. Preserve all diagnostic artifacts.
7. Sensitive-content detection is best effort, not guaranteed redaction. Import
   only explicitly selected bounded artifacts; never auto-ingest transcripts/env.
   Injection-like prose is inert data, never commands or authority.
8. If helpers need public names, retain existing private aliases for compatibility.
   No unrelated evidence refactor. Verifier/promoter must still reject replay output.

Source base
`f90af08906583a5ebe13a7acf0bc65aa18d53a99`; plan
`arx-docs/specs/2026-10-23-factory-failure-regression-plan.md`. Adds no
acceptance path, no second gate, no Docker execution.

## 1. Problem, from code and one real incident

QC finding F2 of `evidence-gate-20261023` (`qc-engine/extra-results.json`, case
`duplicate_json_keys_last_wins`) showed `factory-verify` certifying a manifest
containing `"mode": "diagnostic", "mode": "acceptance"`: `json.loads` keeps the
last member, another reader may keep the first. The cited observable is a
verifier defect, not a product defect. `f90af08` repairs it with a duplicate-key
hook and a `RecursionError` guard (`factory/evidence.py:1177-1215`); `a07521c`
still parses with bare `json.loads` (`a07521c:factory/evidence.py:1227`).

The loop stays small: ingest a cited failure as data, emit a proposal that is
explicitly not authorization, and execution-compare one evaluator-owned
regression suite on two revisions. Nothing infers root cause, edits a skill or
test in place, or turns a failing command into acceptance evidence.

## 2. Components and boundaries

| Component | File | Owns | Does not own |
| --- | --- | --- | --- |
| Incident store | `factory/learning.py` | strict import, dedup/conflict, immutable records, proposal rendering | diagnosis, execution, task creation |
| Replay engine | `factory/replay.py` | suite binding, two disposable checkouts, execution, classification, report | acceptance, promotion, incident mutation |
| CLI | `factory/learning.py:main` | `import`, `propose`, `replay` subcommands | any network or background work |
| Existing gate | `factory/evidence.py`, `verify.py`, `promote.py` | unchanged acceptance semantics | replay reports |

One console script: `factory-learning` (`pyproject.toml`). No new dependency, no
daemon, no database, no LLM call.

Reused controls (not reimplemented): `evidence.py`'s existing
`_parse_manifest_bytes`, `_sha256`, `_run_git` are promoted to public
`parse_strict_json`, `sha256_file`, `git_read` (behaviour identical, one call
site each); `evidence.GIT_SAFETY_ARGUMENTS` (hooks disabled, fsmonitor off);
`evidence.engine_identity`; `evidence.FULL_SHA_RE`; `policy.assert_safe_repo_root`
and `policy.SENSITIVE_HOST_ROOTS` for path admission.

## 3. Incident record (`factory-incident` v1)

```json
{"schema": {"name": "factory-incident", "version": 1},
 "fingerprint": "sha256:<64hex>", "content_id": "sha256:<64hex>",
 "title": "...", "symptom": {"summary": "...", "observed": "..."},
 "category": "implementation|specification|tooling_evidence|infrastructure|unknown",
 "root_cause": {"status": "unknown|supported", "statement": null, "evidence_refs": [0]},
 "sources": {"repository": "...", "revisions": ["<40hex>"], "repaired_revision": null},
 "evidence": [{"ref": "qc-engine/extra-results.json", "kind": "probe", "bytes": 1234, "sha256": "sha256:<64hex>"}],
 "hypothesis": "...", "improvement_class": "regression_test|spec_clarification|tooling_check|infra_change",
 "replay": {"suite_id": "duplicate-json-key", "target_nodeid": "...", "expect_baseline": "assertion_failure", "expect_candidate": "pass"},
 "corrects": null, "origin": {"kind": "reviewer|operator|machine", "ref": "<card-id>"}}
```

`fingerprint` = sha256 of canonical JSON over `symptom.summary` (whitespace
collapsed, casefolded), sorted `sources.revisions`, and the sorted evidence
`sha256` set. `content_id` = sha256 of the whole canonical record minus the two
id fields. `title`, `hypothesis` and `root_cause` never participate in identity,
so re-wording cannot fork or merge incidents.

**Storage.** `<store>/incidents/<fingerprint>/<content_id>.json` and
`<store>/proposals/<proposal_id>.json`, created `O_CREAT|O_EXCL`, never
rewritten: no mutable record, so no lock and no second gate. Outcomes: new
fingerprint -> `imported`; existing identical `content_id` -> `duplicate` (exit
0); existing record with a different `content_id` -> `conflict` (exit 1) unless
the record sets `corrects: <content_id>`, which appends a linked correction and
leaves history intact. A crash between check and create can leave two records;
the next import reports `conflict` with both ids rather than choosing a winner.

**Import guards** (`--record <file> --evidence-root <dir> --store <dir>`):
regular non-symlink file <= 256 KiB, UTF-8, strict JSON (duplicate keys, deep
nesting, unknown top-level keys and enum values -> `malformed_record` /
`invalid_category`); <= 64 evidence entries; each `ref` is a relative POSIX path
matching `[A-Za-z0-9._/-]{1,200}` with no empty, `.` or `..` component and no
scheme; resolved under `--evidence-root` with containment and symlink-component
checks (`path_escape`); must exist, <= 8 MiB, match `bytes`/`sha256`
(`evidence_missing`, `evidence_hash_mismatch`, `evidence_stale`); text fields
<= 4 KiB, control characters rejected, PEM private-key blocks and secret-shaped
keys/values rejected (`sensitive_content`). The store must not live inside the
source repository. Import reads nothing outside `--evidence-root`, writes only
under `--store`, resolves no URL, and executes no field.

## 4. Proposal (`factory-proposal` v1) — proposal only

Deterministic rendering from one incident: `proposal_id` = sha256 of the body,
`status: "pending_review"`, `diagnosis` copied verbatim (`root_cause.status`
never upgraded, `evidence_refs` re-validated), `executable_criteria` from
`incident.replay`, `replay_request` as inert data, and
`authorization: {"grants": [], "note": "no promotion, dispatch, deployment or
policy change"}`. `propose` requires a `replay` block (else exit 1
`no_executable_criterion`) and is idempotent by `proposal_id`. No network, no
subprocess, no Kanban write, no skill/test edit.

## 5. Replay: the executable part

**Suite contract.** `--suite-dir` is caller-supplied and must live outside
`--repo`, the store and `--work-root`. It contains `pytest.ini`, `test_*.py`
files, and `expectations.json` (`factory-replay-expectations` v1:
`suite_digest`, per-case `{nodeid, baseline:{outcome}, candidate:{outcome}}`,
`controls:[...]`, optional `timeout_s`). Bounds: <= 32 files, <= 256 KiB each,
<= 1 MiB total, no symlinks. `suite_digest` = `sha256:` over canonical JSON of
sorted `[relative_name, bytes, sha256]`, recomputed after execution; any change
-> `rejected suite_modified`. The suite is evaluator-owned and committed to
neither revision, so neither side can alter it.

**Execution.** `factory-learning replay --repo P --baseline SHA --candidate SHA
--suite-dir D --work-root R --out O --approve-execution [--timeout-s 300]
[--json]`. Without `--approve-execution`, exit 2 before any Git or process work.
Admission: full 40-hex SHAs present in `P`, `baseline != candidate`, `P` admitted
by `policy.assert_safe_repo_root`, `R` an existing empty directory outside `P`,
`D`, the store and `policy.SENSITIVE_HOST_ROOTS`, `O` non-existent. Per side:
`git -c core.hooksPath=/dev/null -c core.fsmonitor=false worktree add --detach
R/<side> <sha>` (local objects, no fetch, `GIT_TERMINAL_PROMPT=0`), then a
preflight `<engine python> -c "import factory.evidence; print(__file__)"` with
`PYTHONPATH=R/<side>`, `PYTHONNOUSERSITE=1`; a resolved path outside `R/<side>`
is `harness_error` (the missing-dependency / wrong-engine discriminator). Then,
with `cwd=D`:

```
python -m pytest -c D/pytest.ini --noconftest --rootdir D -p no:cacheprovider -q -rA --no-header <suite test files>
```

`--noconftest` and `-c` prevent candidate-committed `conftest.py` or pytest
config from influencing the run; `PYTHONPATH` binds the checkout under test.
`timeout` kills the process group (`start_new_session`, `SIGKILL`) and yields
`timeout`. Afterwards each worktree HEAD must still equal its requested SHA
(`revision_mismatch`); worktrees are removed in `finally`, and removal failure is
recorded, not hidden. No Compose, Docker, port or network work in v1; a suite
needing services fails preflight/collection and is `inconclusive`.

**Classification** (per side, per expected node; pytest exit codes 0/1/2/3/4/5):
`pass` (0, node reported passed), `assertion_failure` (exit 1 with `FAILED
<nodeid> - AssertionError...`), `error_failure` (exit 1 with `ERROR` lines or a
non-`AssertionError` exception), `collection_error` (2 with import/collection
errors), `no_tests` (5), `interrupted` (3/4), `timeout`. Only the exact expected
nodeid and class can match; a bare nonzero exit never counts.

**Verdict.** `reproduced` iff suite digest equal before/after, both revisions
bound, preflight ok both sides, every control `pass` on both sides, the target
matches `expect_baseline` on baseline and `expect_candidate` on candidate, and
every artifact exists. `not_reproduced` when well-formed but the target does not
match. `inconclusive` for `timeout`, `collection_error`, `error_failure`,
`harness_error`, `no_tests`, `interrupted`. `rejected` for digest/SHA/path/
admission failures. The report carries `"promotional": false` and
`"acceptance_evidence": false`.

## 6. Keeping acceptance intact

`factory/evidence.py`, `verify.py` and `promote.py` requirements are unchanged;
no flag is added and no check relaxed. The replay report is
`<out>/replay-report.json` + `replay-report.sha256`, never
`evidence-manifest.json`, so `factory-verify --artifacts-dir <out>` already
rejects it (`manifest_missing`) and `factory-promote` returns
`evidence_rejected:manifest_missing` with the ref unmoved — verified by test, not
prose. Baseline failure is diagnostic evidence only; it can never certify, and no
new verifier mode exists.

## 7. Implementation scopes

**Stage A — incident/proposal** (`factory/learning.py` ~420 lines;
`tests/test_learning_incidents.py` ~340, `tests/test_learning_cli.py` ~100;
`pyproject.toml` script).
A1 cited incident imports `imported`; a second identical import prints
`duplicate`, exit 0, same `fingerprint`, store tree byte-identical.
A2 same fingerprint with changed `category`/`hypothesis` -> `conflict`, exit 1,
prior record and listing unchanged; `corrects` appends a second record and both
remain readable.
A3 duplicate-key JSON, > 256 KiB, unknown field, bad enum -> distinct
`malformed_record` / `invalid_category`, store untouched.
A4 `../`, absolute, symlink, missing and hash-mismatched evidence -> distinct
`path_escape` / `evidence_missing` / `evidence_hash_mismatch`, with a sentinel
outside `--evidence-root` proving no read.
A5 incident text saying "run rm -rf", "create task" or "promote" produces no
subprocess (guard fixture), no write outside `--store`, proposal
`pending_review`, `authorization.grants == []`.
A6 `propose` without a `replay` block exits 1 `no_executable_criterion`; a valid
one is idempotent and cites the incident id and target nodeid.

**Stage B — replay** (`factory/replay.py` ~430 lines;
`tests/test_learning_replay.py` ~420, `tests/test_learning_safety.py` ~120;
`tests/fixtures/replay-suite/`; `docs/failure-regression.md`).
B1 training case: baseline `a07521c` -> `assertion_failure` on
`test_duplicate_json_keys.py::test_duplicate_json_keys_are_rejected`, candidate
`f90af08` -> `pass`, control passes both, equal digest, verdict `reproduced`,
report binds both SHAs, digest, commands, exit codes and log hashes.
B2 a target failing identically on both sides -> `not_reproduced`; a suite whose
import is missing -> `inconclusive harness_error`; never `reproduced`.
B3 timeout -> `inconclusive timeout`, no surviving child, duration recorded.
B4 suite mutated or expectations digest mismatched between sides, candidate
`pytest.ini`/`conftest.py` forcing green, changed SHA, `baseline == candidate`,
no `--approve-execution`, `--work-root` inside repo/non-empty/sensitive -> exit 2
or `rejected` with a distinct reason and no checkout outside `--work-root`.
B5 `factory-verify` on the replay `--out` exits 1 `manifest_missing`;
`factory-promote` returns `evidence_rejected:manifest_missing` with the target
ref byte-identical before/after.
B6 report declares `promotional: false`; `--json` round-trips and human output
names the verdict, both SHAs, digest and per-node outcomes.

**Stage C — ops** (`bin/factory` one `commands` entry + usage line; ops
`README.md`; `tests/test_launcher.py`).
C1 `factory learning --help` resolves through `.runtime/current` exactly like
`run|verify|promote`; no `--artifacts-root` injection for `learning`.
C2 installed replay of the training case reproduces `reproduced` with the same
suite digest, SHAs and command as the dev run; no activation, no new network.
C3 launcher diff is limited to the `commands` mapping, usage string and docs;
existing subcommands' argv/env are unchanged (launcher test).
C4 ops docs state the commands, the non-promotional status and the sandbox
limits; `test_docs.py` passes.

## 8. Residual limits (stated, not solved)

* Not a sandbox: replay runs candidate code as the same UID with host filesystem
  and network reachable; `PYTHONNOUSERSITE`, `--noconftest` and env control are
  isolation of pytest configuration, not containment. Only reviewed suites may
  be run, and the work root is disposable.
* Attestation: report hashes bind bytes, not authorship; same-UID rewriting is
  outside the boundary, exactly as in `docs/evidence-gate.md`.
* `root_cause` stays `unknown` unless a reviewer sets `supported` with valid
  evidence references; replay success proves the observable is covered, not that
  the stated cause is right.
* Deterministic Python cases only. Service-dependent, stochastic, timing,
  model-quality and cost claims are out of scope and must not be inferred from
  one run. No Compose lifecycle exists in v1; any future one must go through
  `policy.run_compose` under the installed `factory run`, not a new runner.
* Git object reads assume both revisions exist locally; no fetch, no remote.
* Held-out evaluation is owned by QC under its own frozen suite; this design
  neither names nor reads it.

## 9. Non-goals

No autonomous diagnosis or LLM call; no Kanban emission or task creation; no
skill/test/gate/repo rewrites from an incident; no auto-remediation; no
scheduler or recurring loop; no policy relaxation or new verifier mode; no
dashboard, database, queue or external service; no deployment, push or install.
