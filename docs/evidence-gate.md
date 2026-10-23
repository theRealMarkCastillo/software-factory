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

## Gate policy

One table in `factory/evidence.py` is the single source of truth for the runner
and the verifier. A gate is accepted only when it matches the required fields;
`fail`/`error` reject as `gate_failed:<gate>`,
`unavailable`/`unsupported`/`disabled` reject as `gate_unavailable:<gate>`, and
any other skip needs a documented inapplicable reason plus substantiation.
Nothing in a manifest can add, relax, or reclassify a gate.

## Legacy evidence and the old checker

`assert_assembled_evidence` remains importable with its historical v1 semantics
(now expressed over the shared policy table) for old artifacts and readers. It
is **not** the verifier: it does not check schema identity, acceptance mode,
source binding, teardown, safety, engine identity, or artifact hashes. A legacy
v1 manifest — including the pilot's — verifies as
`legacy_schema_unsupported` and is left byte-identical.

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
