# Evidence gate: revision-bound evidence and independent verification

Status: implemented increment 1 of [the evidence-gate design](evidence-gate-design.md)
— the schema-v2 producer and the read-only verifier. Guarded promotion is a
separate increment; nothing here pushes, tags, deploys, or moves a ref.

## What the producer records

`factory.runner` writes `<artifacts-root>/<factory-project>/evidence-manifest.json`
plus a `sha256sum`-format sidecar `evidence-manifest.sha256`. The manifest is
schema `factory-evidence` version 2 and keeps every legacy top-level key
(`git_revision`, `git_status_porcelain`, `entry_point`, `command`, `audit`,
`coverage`, `datamodel`, `gates_run`, `fixtures`, `execution`, `environment`,
`hermes_factory_card`) with its previous meaning, so older readers keep working.

New fields bind the record to what actually ran:

| Field | Meaning |
| --- | --- |
| `schema` | `{"name": "factory-evidence", "version": 2}`; absence means legacy v1 |
| `mode` | `acceptance` or `diagnostic`; diagnostic can never certify |
| `source` | `sha`, `clean`, and the `start`/`end` snapshots captured before gates and after execution |
| `base` | `ref`, `kind`, and the **immutable commit SHA** the coverage gate measured against, captured before any gate ran |
| `engine` | engine name/version/Python plus a `sha256:` fingerprint over every `factory/*.py` file |
| `runtime.teardown` | whether teardown was attempted and whether it succeeded |
| `runtime.safety` | postflight outcome, protected/unexpected changes, collateral flag |
| `artifacts` | required logs (`up.stdout.log`, `up.stderr.log`, `compose.log`) with byte counts and SHA256 |
| `fixtures` | declared fixture paths with SHA256 and Git-tracked status |
| `execution` | actual `exit_code`, `passed`, `stage`, `duration_s` |

`factory-run --mode` defaults to acceptance when gates run and diagnostic when
`--no-gates` is given. `--no-gates --mode acceptance` is a usage error (exit `2`)
reported before any gate or Docker work. A run whose source changed during
execution, whose teardown or postflight was not clean, or whose evidence cannot
be certified by the shared checker fails with stage `evidence_enforcement`.

## Verifying evidence

```sh
factory-verify \
  --artifacts-dir <dir>      # contains evidence-manifest.json + sidecar
  --repo <path>              # object database containing base and candidate
  --candidate <40-hex sha>   # the revision the evidence claims
  --base <40-hex sha>        # the revision it was measured against
  [--frozen <path>]          # repeat for each literal frozen file/directory
  [--json]
# equivalently: python -m factory.verify ...
```

Exit codes: `0` certified, `1` rejected, `2` usage error. The command is
read-only and offline: it writes no receipt, mutates no file and no ref, and runs
only local Git reads with command-scoped hardening (`core.hooksPath=/dev/null`,
`core.fsmonitor=false`, `--no-optional-locks`, `--no-ext-diff`, `--no-textconv`).
No persistent Git configuration is written.

Checks, in order (first failure wins; every executed check is reported):

1. artifacts directory exists; manifest present, ≤ 1 MiB, a JSON object.
2. schema identity (before the sidecar, so real legacy evidence reports
   `legacy_schema_unsupported` rather than a missing digest).
3. sidecar present and matching the manifest bytes.
4. structural completeness and strict types (booleans are not integers).
5. `mode == "acceptance"` and `gates_run is true`.
6. clean source at start, identical start/end snapshots.
7. execution completed with exit `0`.
8. teardown attempted and successful; postflight clean with no protected,
   unexpected or collateral change.
9. gate results against the code-owned policy table.
10. engine fingerprint equality (evidence is only certifiable by the engine
    build that produced it).
11. required artifacts present, unique, inside the directory, with matching byte
    counts and hashes; no symlinks or path escape.
12. `--candidate` equals `source.sha`; `--base` equals `base.sha` and
    `coverage.base_sha`; both objects exist and are commits; base is an ancestor
    of the candidate.
13. fixtures are tracked in the candidate tree and hash-match it.
14. each permitted inapplicable gate is *substantiated from candidate/base
    content*: `audit: no_manifest` requires no dependency manifest in the
    candidate tree, `coverage: skipped:no_added_python` requires the
    base..candidate diff to add no Python lines, and `datamodel:
    skipped:no_data_model` requires no `data-model.yml` in the candidate tree.

Reason codes are a closed set documented in `factory/evidence.py`
(`REASON_CODES`); gate and artifact families carry a `:<gate>` / `:<name>`
suffix, e.g. `gate_unavailable:audit`, `artifact_hash_mismatch:compose.log`.

## Frozen acceptance paths

`--frozen compose.factory.yml --frozen tests/acceptance/` additionally recomputes
`git diff --name-only -z --no-renames <base> <candidate>` in `--repo` using an
argv list, not a shell. Every listed path counts: additions, deletions,
modifications, modes, type changes and gitlinks. Renames count at both ends.
A violation rejects with the closed reason code `frozen_path_modified` in both
text and JSON output (exit 1), even if the evidence is otherwise certifiable.
Without this flag, output and exit behavior are unchanged.

Entries are literal repository-relative POSIX paths: a file has no trailing
slash; a directory ends in `/` and matches everything beneath it. There are no
globs. Matching takes Unicode NFC then `casefold()`: files compare for equality,
directories compare by prefix including the trailing slash. Thus `Tests/x`
matches `tests/`, but `tests_extra/x` does not. Syntax errors (empty/absolute
paths, `.`/`..`/empty segments, backslashes, control characters, globs), more
than 200 entries, or duplicates after NFC/case folding are usage errors (exit 2).
Verification does not re-check base existence; orchestration admission does.

This is a declared-path boundary, not workspace write protection. Unlisted files
that influence frozen files (a root `conftest.py`, unlisted Compose override, or
`.gitattributes` filters) and OS-level write protection are out of scope. The
spec author must list everything that must not change.

## Gate policy

One table in `factory/evidence.py` is the single source of truth for the runner
and the verifier. A gate is accepted only when it matches the required fields;
`fail`/`error` reject as `gate_failed:<gate>`,
`unavailable`/`unsupported`/`disabled` reject as `gate_unavailable:<gate>`, and
any other skip needs a documented inapplicable reason plus substantiation.
Nothing in a manifest can add, relax, or reclassify a gate.

The npm audit gate covers production dependencies only, using `npm audit --json
--audit-level moderate --omit=dev`; its result records `npm_scope: production`
and the exact `command` argv, including in runner evidence. npm 7+ with audit
report v2 is required; older/unsupported reports fail closed with an upgrade
instruction. Dev/build tooling is not vulnerability-gated, so reviewers must
verify deployed images do not execute dev-only packages (such as a Vite dev
server as the production server). Python auditing is unchanged. For object-valued
npm dependency metadata, `audited` counts distinct `name@version` entries (including
local workspaces, excluding the root) from `npm ls --omit=dev --all --json
--package-lock-only`, while `lock_total` retains the whole-lock inventory;
legacy integer counts are unchanged and an unverified production tree blocks the gate.

## Legacy evidence and the old checker

`assert_assembled_evidence` remains importable with its historical v1 semantics
(now expressed over the shared policy table) for old artifacts and readers. It
is **not** the verifier: it does not check schema identity, acceptance mode,
source binding, teardown, safety, engine identity, or artifact hashes. A legacy
v1 manifest — including the pilot's — verifies as
`legacy_schema_unsupported` and is left byte-identical.

## Promoting a verified revision

```sh
factory-promote \
  --repo <path>                    # worktree toplevel; must be clean
  --artifacts-dir <dir>            # schema-v2 acceptance evidence
  --candidate <40-hex sha>         # full candidate commit
  --base <40-hex sha>              # full expected base commit
  --target refs/heads/<name>       # existing branch ref, not checked out anywhere
  [--dry-run] [--json]
# equivalently: python -m factory.promote ...
```

Exit codes: `0` promoted (or dry-run ok), `1` rejected, `2` usage error.
Promotion verifies in-process with `verify_evidence`; it never reads a receipt,
so there is nothing to replay or forge. There are no `--force`, `--skip-gate`,
`--allow-legacy`, `--push`, `--tag` or `--mode` flags.

Preconditions, all checked before anything is written:

- `--target` is a full `refs/heads/<name>` ref that passes
  `git check-ref-format`, exists, and is not symbolic. Tags, `HEAD`, short names
  and symref aliases are refused.
- The target is not checked out in **any** worktree. Git's `update-ref` would
  silently leave such a checkout's files stale, so this is refused even when
  that worktree is clean; detach or remove it first. The promoting worktree must
  be clean (untracked files included).
- The target is exactly `--base` (`target_moved` otherwise), the candidate is a
  commit, `base` is its ancestor, and the candidate is the tip of some branch.
- Evidence certifies the exact candidate and base. Any verifier rejection stops
  the command with `evidence_rejected:<reason>`.

Only the named ref changes: one `update-ref --no-deref <ref> <candidate>
<expected-old>` compare-and-swap, then a read-back that must return the
candidate. No index, worktree, config, remote or tag is touched, and no
push/receive/merge hook can run. `--dry-run` performs every precondition and
the in-process verification, and skips only the ref update.

Concurrency: a lock under the **common** Git dir (`factory-promote.lock`,
`O_CREAT|O_EXCL`) admits one promotion at a time. An existing lock always fails
closed with `lock_busy`; it is never stolen because it looks old, and only a
lock this invocation created (matched by inode) is removed in `finally`.
Occupancy, cleanliness, the target digest and the evidence are re-observed
immediately before the CAS, so a change made after the first verification is
caught.

Repository hooks and fsmonitor are suppressed for every Git call with
command-scoped options (`-c core.hooksPath=/dev/null -c core.fsmonitor=false
--no-optional-locks`). A `reference-transaction` hook therefore cannot observe
or veto a sanctioned promotion, and no persistent configuration is written.

Residual limit (stated, not hidden): a same-UID actor can rewrite the artifacts
directory between the final hash read and the ref update, and raw `git`
(`update-ref`, `push`, `merge`, a hand-edited ref) run outside this command is
not intercepted. The compare-and-swap makes a concurrent *ref* writer lose
(`ref_update_failed`) rather than be overwritten; the artifact window is
bounded, not closed.

## Trust boundary

SHA256 detects corruption, truncation, and uncoordinated edits. It is not
attestation: an actor who can write the artifacts directory can produce a
self-consistent fabricated manifest, sidecar, and logs, and raw Git run outside
the sanctioned commands is not intercepted. Clean start/end snapshots do not
bind ignored files, mutable container images, dependencies, or the inherited
environment, and do not prove that no transient edit was reverted. The engine
fingerprint binds evidence to the engine build that produced it, not to an
installed release. Closing the same-UID gap needs a separate trust authority
(signing key held outside the UID); this increment makes no such claim.
