# Evidence gate design: bind run evidence to guarded source promotion

Status: accepted for implementation by Arx with the binding corrections below.
Design card `t_371b42bb`; this document alone is not runtime acceptance.

## Operator review corrections (override conflicting details below)

1. Never steal a promotion lock based on age. An existing lock fails closed;
   manual recovery is outside this command. Release only the lock this invocation
   actually acquired, in finally. No 120-second stale-lock deletion.
2. Disable repository hooks for sanctioned Git operations with command-scoped
   `-c core.hooksPath=/dev/null` (no persistent config change), and disable optional
   fsmonitor execution/optional index refresh for read-only probes. The assertion
   below that disabling hooks requires rewriting configuration is incorrect.
   Test an installed sentinel reference-transaction hook is NOT executed. There
   is no hook_vetoed success-path feature in this increment.
3. Validate targets using git check-ref-format as well as the full refs/heads/
   prefix; reject symbolic target refs explicitly (never update HEAD/tags or a
   symref alias). Recheck worktree occupancy and cleanliness immediately before
   CAS. A same-UID raw Git checkout racing that last check remains outside the
   lock's guarantee and must be documented.
4. Capture the coverage base SHA BEFORE gates, pass that immutable SHA to the
   actual coverage calculation, and record it. Resolving main only at the end
   cannot bind the measured diff. Recheck subject HEAD/status after execution.
5. CLI --mode defaults to automatic resolution: absent mode plus --no-gates is
   diagnostic; absent mode with gates is acceptance. An EXPLICIT acceptance mode
   together with --no-gates fails before lifecycle. Diagnostic never certifies.
6. Inspect schema before requiring the new sidecar so real legacy evidence
   reports legacy_schema_unsupported rather than only digest_missing. Use strict
   types (booleans are not integers), bounded JSON, unique artifact names and
   required artifacts. Reject symlinks for manifest/sidecar/artifacts, traversal,
   and malformed hashes. Git reads must not invoke external diff/textconv/hooks.
7. An allowed inapplicable-gate reason is necessary but insufficient: verify its
   applicability against candidate/base source (dependency manifests, added Python
   lines, data-model declaration); unavailable/error/unsupported never certify.
   All checks use code-owned policy, not provider fields claiming their own scope.
8. Clean start/end snapshots do NOT prove no transient edit was reverted, nor bind
   all ignored files, mutable images, dependencies or inherited environment. State
   this limit plainly. Explicit declared fixtures are checked against Git; this
   increment is not a complete hermetic-build or hostile-same-UID attestation.
9. Promotion failure means no target/index/worktree/config change; the scoped
   coordination lock is an intentional temporary side effect. Offline tests must
   guard all subprocess entry points capable of Docker/network, including Popen
   and finally paths, not just runner.run_compose.

Implementation is staged as evidence producer/verifier first, then guarded
promotion, then independent full QC; ops integration/deployment remain separate.
Base: `3673987e9d4a31de93faed38cef717aa57fde040`, branch `feat/evidence-gate-20261023`.

This design is grounded in the current engine (`factory/evidence.py`,
`factory/runner.py`, `factory/coverage.py`, `factory/audit.py`,
`factory/datamodel.py`, `factory/policy.py`) and in two read-only Git probes run
for this document (results quoted in §6.2).

## 1. Outcome and non-goals

**Outcome:** a runner-produced, versioned evidence record that binds one clean
source revision to one executed command, plus an independent verifier and a
minimal local promotion command that *consumes* that verification for the exact
candidate. Bad evidence, stale evidence and mismatched candidates must fail
mechanically, not by agent discipline. The pilot's finding — "a prose prohibition
did not prevent diagnostic gate bypass" — is the failure this closes.

**Non-goals (deliberate):**

- No TDD/RED-GREEN provenance claims. No event stream, no test-first attestation.
- No new application, service, database, scheduler or dashboard.
- No key infrastructure, signing, certificates or remote attestation.
- No push, tag, deploy, profile, credential or Docker behavior change.
- No rewriting, migration or deletion of existing artifacts (the Task Ledger
  v1/v2 evidence stays as read-only reference).
- No ops-launcher (`software-factory-ops/bin/factory`) or `deploy.py` change in
  this increment; those are a separate scoped card.
- No protection against raw Git run outside the sanctioned command (§8).

## 2. Current state: the actual gaps

| # | Gap | Evidence in code |
|---|---|---|
| G1 | No schema identity. A current manifest, an older pilot manifest and a future incompatible one are structurally indistinguishable. | `write_evidence_manifest` (`factory/evidence.py:51-109`) writes a flat dict; `assert_assembled_evidence` (`:112-161`) validates keys only. |
| G2 | No diagnostic/acceptance mode. `gates_run: bool` is the only signal, and nothing outside the runner reads it. | `runner.py:338-346`, `:501`. |
| G3 | No clean-source binding. `git_revision` is `rev-parse HEAD` and `git_status_porcelain` is recorded but never compared or enforced, so evidence can certify code that was never committed. | `evidence.py:87-88`; coverage explicitly includes the dirty working tree (`coverage.py:179+`). |
| G4 | No start/end state comparison. Source can change between gate evaluation and the Compose run (the exact TOCTOU the architecture doc already admits). | `runner.py:311-347` vs `:358-384`. |
| G5 | No artifact hashes. Three log files are written and never bound to the manifest. | `runner.py:378-384`. |
| G6 | No engine identity. Nothing records which engine revision produced evidence; package metadata is `0.1.0` while releases are tagged, which is precisely the drift the pilot hit. | `pyproject.toml`, no engine field anywhere. |
| G7 | Gate acceptance policy is implicit and not shared with any verifier; `audit: unavailable` passes the runner as a warning but could never certify, and that distinction exists only in `assert_assembled_evidence`. | `runner.py:135-148`; `evidence.py:139-161`. |
| G8 | Teardown result, postflight/safety outcome and collateral classification are computed and then dropped before the manifest is written. | `runner.py:409-471` vs `:481-507`. |
| G9 | Coverage measures the delta against a hardcoded `"main"` and records only the ref name; promotion has no way to prove the gate measured the same base it is advancing from. | `runner.py:152`; `coverage.py:164-177`. |
| G10 | Fixtures are hashed from the working tree at run time and never bound to the candidate revision, so a weakened local fixture can accompany a clean-SHA manifest. | `evidence.py:77-85`. |
| G11 | No independent verifier and **no promotion operation at all**. `grep` for promote/worktree/update-ref across `factory/` returns nothing. QC and promotion are prose steps performed by an agent. | repo-wide. |

What already works and must not be re-implemented: manifest structural checks and
gate/status semantics (`assert_assembled_evidence`), registry-free protected-project
classification and postflight census (`policy.py:100-190`), gate implementations
(`audit.py`, `coverage.py`, `datamodel.py`), and repo-root safety
(`policy.assert_safe_repo_root`). The verifier reuses these; it does not restate them.

## 3. Settled design

Two new modules plus additive changes to one existing module:

- `factory/evidence.py` — schema v2 writer, one code-owned gate policy table, one
  shared acceptance checker, one `verify_evidence()` function (pure, read-only).
- `factory/verify.py` — thin CLI over `verify_evidence` (no policy of its own).
- `factory/promote.py` — thin CLI over `verify_evidence` + one fast-forward-only
  ref update (no policy of its own).
- `factory/runner.py` — records what it already knows (start/end source, artifacts,
  teardown, safety, mode, base SHA) and writes a schema sidecar. No new Docker code.

`assert_assembled_evidence` stays importable with identical semantics (it becomes a
one-line alias for the shared checker), so every existing test and reader keeps
working. The verifier is *not* a second implementation of the gate rules: it calls
the same `GATE_POLICY` table the runner enforces.

### 3.1 Decisions with a recommended default

| # | Fork | Decision (default) | Why |
|---|---|---|---|
| D1 | Verifier/promoter command names | `factory-verify` (`python -m factory.verify`) and `factory-promote` (`python -m factory.promote`); console scripts added to `pyproject.toml`. Ops launcher maps them later. | Matches the existing one-module-one-script convention (`factory-run`, `factory-audit`, …) and keeps engine/ops scopes separate. |
| D2 | Does promotion also update a checked-out working tree? | **No — ref-only.** Advance only `refs/heads/<target>`; refuse if that ref is checked out in *any* worktree (§6.3). | Probe: `update-ref` on a branch checked out elsewhere silently leaves that tree stale (§6.2). Ref-only is the smallest operation with no worktree side effects, and is exactly testable ("advances only the named ref"). Alternative (deferred): a `--refresh-worktree` flag doing a bounded `read-tree -m -u`, after its own tests. |
| D3 | Escape hatches on the verifier | **None.** No `--allow-legacy`, `--skip-gate`, `--mode`, `--force`. | Any such flag recreates the bypass the pilot exposed. |
| D4 | Engine identity enforcement | Exact fingerprint equality; mismatch rejects with "re-run acceptance with this engine". | Fail-closed and unambiguous; consequence documented in §5.4 (old evidence cannot be certified by a newer engine). |
| D5 | Where gate policy lives | One module-level constant in `factory/evidence.py`, consumed by runner and verifier. Never read from the manifest, never overridable from a CLI. | Requirement: "commands cannot smuggle policy through supplied manifest". |
| D6 | Freshness signal | Structural only (candidate/base SHA equality + live target read). `generated_at` is informational and never compared to the clock. | Avoids trusting wall-clock time; stale means *mismatched*, not *old*. |
| D7 | Artifact set | `up.stdout.log`, `up.stderr.log`, `compose.log` required; any extra file may be declared and is then also hashed and checked. | Matches what the runner already writes; no new collection code. |

## 4. Evidence schema v2

Written to `<artifacts-dir>/evidence-manifest.json`, with a sidecar
`evidence-manifest.sha256` in `sha256sum` format.

```json
{
  "schema": {"name": "factory-evidence", "version": 2},
  "mode": "acceptance",
  "generated_at": "2026-10-23T20:41:07Z",
  "card_id": "t_371b42bb",
  "project_name": "factory-t-371b42bb-ab12cd",
  "source": {
    "sha": "<40-hex HEAD at start>",
    "clean": true,
    "start": {"sha": "<40-hex>", "status_porcelain": ""},
    "end":   {"sha": "<40-hex>", "status_porcelain": ""}
  },
  "git_revision": "<40-hex, must equal source.sha>",
  "git_status_porcelain": "",
  "base": {"ref": "main", "kind": "commit", "sha": "<40-hex>"},
  "entry_point": "docker compose test service",
  "command": ["docker", "compose", "up", "--build", "--abort-on-container-exit",
              "--exit-code-from", "test"],
  "engine": {
    "name": "hermes-software-factory",
    "version": "0.1.0",
    "python": "3.12.7",
    "fingerprint": "sha256:<hex>"
  },
  "execution": {"exit_code": 0, "passed": true, "stage": "complete", "duration_s": 61.2},
  "runtime": {
    "teardown": {"attempted": true, "ok": true, "detail": ""},
    "safety": {"postflight": "clean", "protected_changes": [],
               "unexpected_changes": [], "collateral_damage": false}
  },
  "gates_run": true,
  "audit": {"status": "pass", "language": "python", "tool": "pip-audit", "findings": [], "detail": ""},
  "coverage": {"status": "pass", "passed": true, "base": "main",
               "base_sha": "<40-hex>", "base_kind": "commit",
               "files": [], "waivers": [], "detail": "", "test_exit_code": 0},
  "datamodel": {"status": "skipped", "reason": "no_data_model"},
  "artifacts": [
    {"name": "up.stdout.log", "sha256": "<64-hex>", "bytes": 41234},
    {"name": "up.stderr.log", "sha256": "<64-hex>", "bytes": 0},
    {"name": "compose.log",   "sha256": "<64-hex>", "bytes": 90311}
  ],
  "environment": {"runtime_env": {"present": false}, "example_env": {"present": true, "sha256": "<64-hex>"}},
  "fixtures": [{"path": "tests/fixtures/legacy.json", "sha256": "<64-hex>", "tracked": true}],
  "hermes_factory_card": "t_371b42bb"
}
```

Rules:

- `schema.name` must be exactly `factory-evidence`; `schema.version` must be in
  `SUPPORTED_SCHEMA_VERSIONS = {2}`. Absent `schema` = legacy v1 (non-certifying).
- `mode` is `"acceptance"` or `"diagnostic"`; the runner sets it explicitly and
  records `"diagnostic"` whenever gates are disabled.
- `git_revision` / `git_status_porcelain` / `gates_run` / `audit` / `coverage` /
  `datamodel` / `command` / `entry_point` / `environment` / `fixtures` keep their
  current names and meanings (backwards compatibility, §7).
- `engine.fingerprint` = `sha256:` over the canonical text
  `name|version|python_major.minor|` followed by `relpath:sha256` lines sorted by
  path for every `factory/*.py` file, newline-joined. Recomputed by the verifier.
- `artifacts[].name` must be a bare basename (`[A-Za-z0-9._-]+`); hashing resolves
  it inside the artifacts directory and rejects symlink escape (`is_relative_to`
  after `resolve()`), so a manifest cannot make the verifier hash `/etc/passwd`.
- `fixtures[].tracked` is computed at write time; the verifier re-hashes each
  fixture from the candidate commit's tree (`git show <candidate>:<path>`), never
  from the working tree, and rejects untracked fixtures for acceptance.

## 5. Independent verifier

```
python -m factory.verify \
  --artifacts-dir <dir>            # required; contains evidence-manifest.json
  --repo <path>                    # required; object database that must contain base+candidate
  --candidate <40-hex sha>         # required
  --base <40-hex sha>              # required
  [--json]                         # machine output; default human summary
```

Exit codes: `0` certified, `1` rejected, `2` usage error. The command is read-only:
it creates no receipt, writes no file, mutates no ref and no artifact. Promotion
calls `verify_evidence()` in-process, so there is no artifact to replay or reuse —
"fresh verification for the exact candidate" is structural, not procedural.

### 5.1 Check order (fail at the first failure, always report all executed checks)

1. `artifacts_dir` exists, is a directory, resolves inside the operator-supplied path.
2. `evidence-manifest.json` exists, ≤ 1 MiB, parses as a JSON object.
3. `evidence-manifest.sha256` exists and matches the manifest bytes.
4. `schema` present, name/version supported → else `legacy_schema_unsupported` /
   `unsupported_schema_version`.
5. Structural completeness and types: `mode`, `source.{start,end,sha,clean}`,
   `execution`, `runtime.{teardown,safety}`, `engine`, `artifacts`, `gates_run`,
   `base`, `coverage.base_sha`, `git_revision == source.sha`.
6. `mode == "acceptance"` and `gates_run is True` (contradiction is rejected).
7. `source.clean is True`, `start.status_porcelain == ""` and `end == start`
   (same SHA, same empty status) → else `source_dirty` / `source_changed_during_run`.
8. `source.sha == --candidate` → else `source_sha_mismatch` (stale/mismatched candidate).
9. `base.sha == --base` and `coverage.base_sha == --base` → else `base_mismatch`.
10. `execution.passed is True`, `exit_code == 0`, `stage == "complete"` → else
    `execution_incomplete`.
11. `runtime.teardown.attempted is True and ok is True` → else `teardown_failed`.
12. `runtime.safety.postflight == "clean"`, no protected/unexpected changes,
    `collateral_damage is False` → else `safety_incident` / `collateral_change`.
13. Gate results against `GATE_POLICY` (§5.2), per gate.
14. Engine identity: recompute fingerprint, require exact equality → `engine_mismatch`.
15. Candidate object checks: `--base`/`--candidate` resolve in `--repo`; candidate is
    a commit; `merge-base --is-ancestor <base> <candidate>` holds → `unknown_object`,
    `not_a_commit`, `not_fast_forward`.
16. Fixtures: tracked, present in candidate tree, hash match → `fixture_untracked`,
    `fixture_missing`, `fixture_hash_mismatch`.
17. Artifacts: each declared required artifact exists inside the dir, byte count and
    SHA256 match; no declared artifact escapes the directory.

All Git reads are `rev-parse`, `cat-file`, `show`, and `merge-base`; no network,
no writes, no hooks.

### 5.2 Gate policy (single source of truth, code-owned)

```python
GATE_POLICY = {
    # gate: (required fields, permitted inapplicable "status:reason" -> justification)
    "audit":     ({"status": "pass"},
                  {"no_manifest": "no supported dependency manifest in the subject"}),
    "coverage":  ({"status": "pass", "passed": True},
                  {"skipped:no_added_python": "diff vs base adds no python lines"}),
    "datamodel": ({"status": "pass"},
                  {"skipped:no_data_model": "subject has no data-model.yml"}),
}
```

- `status in {"fail", "error"}` → `gate_failed:<gate>`.
- `status in {"unavailable", "unsupported", "disabled"}` → `gate_unavailable:<gate>`;
  this is the deliberate tightening: the runner still treats an unavailable audit as a
  warning (unchanged behavior), but such a run can never produce certifiable acceptance.
- `"status:reason"` not in the permitted map → `gate_not_applicable_unpermitted:<gate>`
  (no blanket pass/skip; a new legitimate reason requires a code change plus a test).
- A gate key missing or not an object → `gate_missing:<gate>`.
- `--no-gates` produces `gates_run: false` + `mode: "diagnostic"`, which fails step 6.
  Additionally, `factory.run --no-gates --mode acceptance` is rejected as a usage error
  before Docker runs, so the contradiction cannot even be recorded.

### 5.3 Output

```json
{"ok": false,
 "reason": "artifact_hash_mismatch:compose.log",
 "detail": "compose.log sha256 3f… != 9c… recorded in manifest",
 "candidate": "<40-hex>", "base": "<40-hex>",
 "checks": [{"name": "schema", "ok": true}, {"name": "artifacts", "ok": false}],
 "evidence": {"schema_version": 2, "mode": "acceptance",
              "engine": {"version": "0.1.0", "fingerprint": "sha256:…"}}}
```

Reason codes are a closed set (documented in the module); tests assert the exact code,
not just the exit status.

### 5.4 Engine identity consequence

Exact fingerprint equality means evidence can only be certified by the engine build
that produced it. After an engine change, older evidence must be regenerated on the
new release — which is already how the pilot's final acceptance was run. The error
message states that explicitly. This is a fail-closed choice, not an oversight.

## 6. Sanctioned promotion

```
python -m factory.promote \
  --repo <path> \
  --artifacts-dir <dir> \
  --candidate <40-hex sha> \
  --base <40-hex sha> \
  --target refs/heads/<name> \
  [--dry-run] [--json]
```

No `--push`, no `--tag`, no `--message`, no `--force`, no remote name. There is no
code path in the module that talks to a remote; `--dry-run` performs every check and
the in-process verification but skips only the ref update.

### 6.1 Algorithm

1. Validate shapes: `--target` matches `^refs/heads/[A-Za-z0-9._/-]+$` (full ref, not
   `HEAD`, not a tag, not a short name); `--candidate`/`--base` are full 40-hex.
2. `repo` must be a Git worktree toplevel; `policy.assert_safe_repo_root(repo)` applies
   (reused, not re-implemented).
3. `target_before = rev-parse --verify <target>^{commit}`; refuse if missing.
4. Worktree state (§6.3): target must not be checked out in any worktree; the
   promoting worktree must be clean (`git status --porcelain` empty).
5. Fast-forward preconditions: `target_before == --base` (`target_moved` otherwise);
   candidate is a commit; `merge-base --is-ancestor base candidate` (`not_fast_forward`);
   candidate equals the tip of at least one `refs/heads/*` ref (`candidate_not_a_branch_tip`).
6. Acquire `<git-common-dir>/factory-promote.lock` (`O_CREAT|O_EXCL`, pid+timestamp,
   stale after 120 s, always removed in `finally`) → `lock_busy`.
7. Run `verify_evidence(artifacts_dir, repo, candidate, base)` in-process; any failure
   ends the command with `evidence_rejected:<reason>` and **zero** mutation.
8. Re-read the target SHA and re-hash the manifest, sidecar and artifacts immediately
   before acting (TOCTOU narrowing).
9. `git update-ref --no-deref <target> <candidate> <target_before>` — the expected-old
   argument makes the update a compare-and-swap; a concurrent writer loses with
   `ref_update_failed` instead of being overwritten. A repo-local
   `reference-transaction` hook fires here and may abort the update (§6.2); that case is
   reported as `hook_vetoed`, never misreported as a race.
10. Re-read `<target>`; it must equal `candidate` (`post_update_mismatch` otherwise, and
    the command fails loudly rather than reporting success).
11. Emit `{"promoted": true, "target": …, "before": …, "after": …, "candidate": …,
    "verification": {"reason": "ok", …}}`.

Only the named ref changes. No index, HEAD, worktree, remote or deployment state is
touched: no push/receive/merge hook can fire, and no other ref moves. The one hook that
does run — `reference-transaction`, which observes and can veto any ref update — is
reported explicitly rather than suppressed; the engine will not disable hooks (that would
mean rewriting repository config).

### 6.2 Git facts established by probe (not assumed)

| Probe | Result |
|---|---|
| `update-ref` on a branch checked out in **another** worktree | **Allowed (rc=0).** The other worktree's HEAD follows the new commit while its files stay at the old content, leaving `M app.py` in an unrelated working tree. |
| `git branch -f` on that same branch | Refused (rc=128): "cannot force update the branch 'main' used by worktree at …". |
| `update-ref <ref> <new> <stale-old>` | Refused (rc=128): "cannot lock ref … is at … but expected …" — CAS works. |
| `update-ref` with a `reference-transaction` hook installed | Hook runs in `preparing`/`prepared`/`committed` phases with the old/new/ref triple on stdin; `exit 1` aborts the update (rc=128, ref unchanged). Push/receive/post-merge hooks did **not** fire. |
| `merge-base --is-ancestor` | rc 0 for base→candidate, 1 for the reverse; usable as the ff precheck. |
| `git status --porcelain` in a linked worktree | Reports `?? dirty.txt` / `M app.py` as expected; usable as the clean check. |

Consequences: Git protects branch-update-while-checked-out for `branch -f` but **not**
for `update-ref`, so that guarantee must live in `factory/promote.py` (§6.3); and ref
updates are observable/vetoable through the local `reference-transaction` hook, so the
command must name that outcome instead of treating every non-zero as a race.

### 6.3 Target checked out in another worktree

`git worktree list --porcelain` is parsed before anything else; for each entry the
`branch refs/heads/<name>` line identifies the checked-out branch and `worktree <path>`
the path.

- Target checked out in **any** worktree → `target_checked_out` naming the path(s), with:
  "detach or remove that checkout (`git -C <path> switch --detach`), or promote from a
  checkout that does not hold the target branch." No worktree is modified by this
  command; the operator decides.
- Promoting worktree dirty (any porcelain entry, untracked included) →
  `worktree_dirty` naming the paths. Refusal, not stashing.
- Target checked out *in a worktree other than `--repo`* is the exact case that the
  probe shows silently corrupts that tree, so it is refused even when clean.

Operational note for this host: `refs/heads/main` is currently checked out at
`/Users/markcastillo/software-factory-public`, so promoting `main` from this workspace
is expected to refuse until the operator detaches that checkout or uses a dedicated
clean checkout. That is the intended, documented behavior — not a defect.

### 6.4 Residual TOCTOU (stated, not hidden)

Verification reads evidence at T1; the ref compare-and-swap happens at T2. Everything
is re-read and re-hashed at T2 minus ε, and `update-ref` re-checks the expected old
value atomically. What remains inside that window: a same-UID process rewriting
artifacts and manifest after the last hash read, or a concurrent non-sanctioned Git
writer winning the CAS race. Neither can be closed without an external trust authority
(keys/attestation), which is out of scope. The window is bounded and reported; it is
not zero.

## 7. Backwards compatibility

| Producer | Verifier result |
|---|---|
| v2 acceptance manifest, unchanged | certified |
| v2 diagnostic manifest (incl. `--no-gates`) | `mode_not_acceptance` |
| v1 manifest (this host's Task Ledger pilot evidence, no `schema` key) | `legacy_schema_unsupported`, non-certifying, file untouched |
| `schema.version` 3+ or unknown `schema.name` | `unsupported_schema_version` / `unsupported_schema_name` |
| manifest with no sidecar / no artifacts list | `manifest_digest_missing` / `malformed_manifest` |

- `assert_assembled_evidence(manifest)` keeps accepting the v1-shaped dicts used by
  `tests/test_evidence.py`; it is re-expressed over the shared `GATE_POLICY` with
  identical accept/reject semantics (existing tests are the regression).
- The runner keeps writing every legacy top-level key with its current meaning, so
  external readers and `filter` scripts continue to work; `git_revision` is retained
  as an alias and the verifier asserts it equals `source.sha`.
- The `factory run` CLI keeps all current flags; it gains `--mode {acceptance,diagnostic}`
  (default `acceptance`) and rejects `--no-gates --mode acceptance`.
- Nothing rewrites, migrates or deletes existing artifacts.

## 8. Trust boundary

Enforced by code and covered by tests:

- Schema identity and version; legacy and unknown schemas never certify.
- Diagnostic vs acceptance; gates disabled can never approve.
- Acceptance requires a clean, unchanged source revision equal to the candidate, and a
  base equal to both the recorded diff base and the promotion's expected base.
- Required logs and fixtures present and SHA256-matched; manifest sidecar digest matched;
  no path escape from the artifacts directory.
- Gate results checked against a code-owned table; manifest content cannot add,
  relax or reclassify a gate.
- Teardown and postflight safety must be clean.
- Promotion is fast-forward-only, single-ref, CAS-protected, concurrency-locked and
  refuses dirty worktrees and targets checked out elsewhere.
- Failed verification performs no mutation.

Explicitly **not** prevented (documented in the module docstring, the CLI `--help` and
the release notes):

1. **Same-UID coordinated evidence rewrite.** SHA256 detects corruption, truncation and
   uncoordinated edits. An actor who can write the artifacts directory can produce a
   self-consistent fabricated manifest, sidecar and logs. Closing this needs a separate
   trust authority (signing key held outside the UID, append-only store). No such claim
   is made and no key infrastructure is added here.
2. **Raw Git outside this interface.** `git update-ref`, `git push`, `git merge` or a
   hand-edited ref are not intercepted. The sanctioned command is the only path the
   factory models; it is not a repository lock.
3. **Repository-local hooks.** A `reference-transaction` hook can observe the ref update
   and veto it (reported as `hook_vetoed`). The engine does not disable or rewrite hooks,
   because that would mean mutating repository configuration; hook trust is the
   operator's.
4. **Engine replacement before a run.** The fingerprint binds evidence to the engine
   that wrote it; it does not prove that engine was the installed release.
5. **Docker/daemon-level behavior.** Postflight is a container-state census; it is not
   proof of isolation, and it does not constrain what a container did inside itself.
6. **Filesystem-level races after the final re-read** (§6.4) and clock trust (no
   freshness decision uses timestamps).

## 9. Falsifiable acceptance criteria

Each is a test that fails on a plausible wrong implementation.

- **C1 — Legacy and unknown schemas never certify.** A v1-shaped manifest (the pilot's
  real one, copied read-only) and a `version: 3` manifest both exit `1` with
  `legacy_schema_unsupported` / `unsupported_schema_version`; nothing is written.
- **C2 — Diagnostic evidence can never approve.** A mocked `--no-gates` runner result
  records `mode: "diagnostic"`, `gates_run: false`; `verify` → `mode_not_acceptance`;
  a hand-crafted `gates_run: false` + `mode: "acceptance"` → `gates_disabled`; and
  `factory.run --no-gates --mode acceptance` exits `2` before any gate or Docker call.
- **C3 — One-at-a-time mutation is rejected.** Ten single-field variants (log byte,
  source SHA, start≠end, dirty start, audit `pass`→`unavailable`, coverage
  `passed: true`→`false`, coverage `base_sha` changed, artifact removed, teardown
  `ok: false`, sidecar digest changed) each exit `1` with a distinct, asserted reason
  code; the unchanged control exits `0`.
- **C4 — Success is exactly one fast-forward.** Offline temp repo (base → candidate,
  candidate on a branch tip), valid v2 evidence: exit `0`, `refs/heads/<target>` moves
  base→candidate, every other ref and the index/worktree are byte-identical;
  `--dry-run` moves nothing.
- **C5 — Failure is zero mutation.** Wrong `--base`, moved target, non-ancestor
  candidate, dirty promoting worktree, target checked out in another worktree, and a
  tampered manifest each exit `1` with the named reason, and the target ref SHA is
  identical before and after (asserted by re-reading refs).
- **C6 — Verification is pure and engine-bound.** Running `verify` twice leaves the
  artifacts directory tree hash and every ref unchanged and creates no file (no
  receipt); a manifest whose `engine.fingerprint` differs → `engine_mismatch`.

## 10. Regression matrix → test files

| Case | Test file | Assertion |
|---|---|---|
| valid acceptance passes | `tests/test_verify.py` | exit 0, `ok: true`, all checks ok |
| changed source SHA / changed log / missing artifact | `tests/test_verify.py` | `source_sha_mismatch`, `artifact_hash_mismatch:<n>`, `artifact_missing:<n>` |
| missing gate / gate unavailable / unpermitted skip | `tests/test_verify.py` | `gate_missing:*`, `gate_unavailable:*`, `gate_not_applicable_unpermitted:*` |
| diagnostic substitution | `tests/test_verify.py` | `mode_not_acceptance` |
| dirty source, start≠end | `tests/test_verify.py` + `tests/test_runner.py` | `source_dirty`, `source_changed_during_run` |
| incomplete/failed execution, teardown fail, safety incident | `tests/test_verify.py` | `execution_incomplete`, `teardown_failed`, `safety_incident` |
| legacy / future schema | `tests/test_verify.py` | `legacy_schema_unsupported`, `unsupported_schema_version` |
| expected-base change rejects promotion | `tests/test_promote.py` | `base_mismatch` / `target_moved`, ref unchanged |
| success performs exact fast-forward | `tests/test_promote.py` | only named ref moves; dry run no-op |
| dirty / checked-out-elsewhere target | `tests/test_promote.py` | `worktree_dirty`, `target_checked_out` (paths named) |
| hook veto and CAS race | `tests/test_promote.py` | `hook_vetoed` (ref unchanged), `ref_update_failed` |
| schema v2 writer, engine fingerprint, policy table | `tests/test_evidence.py` (extend) | v2 keys; v1 dicts still accepted by `assert_assembled_evidence` |

Test constraints: every test is offline and uses a temporary Git repository or a
temporary artifacts directory — no Docker, no network, no live host state. A local
autouse fixture wraps `subprocess.run` and raises on `docker`, `docker compose`,
`git push`, `git fetch`, `git ls-remote` and `git clone`, so a future edit cannot
silently reach outside the test sandbox. Existing runner tests keep their
`monkeypatch` pattern. The pilot's real manifest is copied read-only as a fixture for
the legacy case and never modified.

## 11. Implementation scope

| File | Change | Approx. |
|---|---|---|
| `factory/evidence.py` | `SCHEMA_*`, `GATE_POLICY`, `engine_identity()`, `artifact_records()`, `check_evidence()`, `verify_evidence()`; extend `write_evidence_manifest` with keyword-only `mode`, `source_start`, `source_end`, `runtime`, `artifacts`, `base`; sidecar write; `assert_assembled_evidence` becomes an alias | +260 lines |
| `factory/verify.py` | CLI: parse, call, render, exit codes | +110 |
| `factory/promote.py` | CLI + `promote_ref()`: worktree precheck, ff/CAS, lock, re-read | +210 |
| `factory/runner.py` | capture source start/end, base SHA, artifacts, teardown, safety; `--mode`; reject `--no-gates --mode acceptance`; write v2 | +90 / −20 |
| `pyproject.toml` | `factory-verify`, `factory-promote` entry points | +2 |
| `tests/test_verify.py`, `tests/test_promote.py`, `tests/test_evidence.py` | new/×matrix above | +450 |
| `docs/architecture.md` | one short section naming the two commands and the trust boundary | +15 (optional, P2) |

Explicitly out of scope for the implementation card: ops launcher, `deploy.py`,
`deployment.json`, profiles, skills, Docker/Compose runs, push/tag/deploy, artifact
migration, TDD provenance.

Order of work inside the card (fail-first, per `CONTRIBUTING.md`):

1. `tests/test_evidence.py` additions for schema v2 + policy table (red).
2. `factory/evidence.py` writer/policy/verifier core (green).
3. `tests/test_verify.py` full matrix over fixtures, including the real legacy manifest (red → green).
4. `factory/verify.py` CLI.
5. `tests/test_promote.py` offline temp-repo matrix (red → green) before `factory/promote.py`.
6. `factory/runner.py` recording changes + `tests/test_runner.py` additions.
7. `ruff check`, `ruff format --check`, full `pytest -q`, `git diff --check`.

## 12. Recommended sequence

1. **This card:** design + verdict artifact (done).
2. **Engine implementation** (Arx-dispatched, goal mode, same branch): scope of §11 only.
3. **Independent QC at a frozen SHA** (separate profile, read-only): full suite, ruff,
   the §9 negative controls, mutation sweep, and a check that no ops/deploy/profile file
   changed.
4. **Ops integration** (separate card): expose `factory verify` / `factory promote` in
   the launcher mapping and record it in the release manifest; no behavior change in the
   engine.
5. **Installed acceptance**: isolate the Task Ledger fixture, regenerate v2 evidence with
   the installed release, verify, and promote with `--dry-run` first, then for real from
   a checkout that does not hold the target branch. Report exact SHAs.

## 13. Limits of this document

This is a design, not evidence. It contains no acceptance claim about unbuilt code.
The three Git probes it cites (worktree/update-ref behavior, worktree staleness, hook
firing and veto) were run offline against temporary repositories for this document;
every other claim about current behavior is read from the named source lines, and the
pilot manifest shape was read from the real Task Ledger artifacts. Rewriting evidence by
a same-UID actor, raw Git outside the sanctioned command, and engine replacement remain
outside the boundary by construction (§8).
