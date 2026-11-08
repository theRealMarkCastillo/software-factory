# Acceptance record: LangGraph hybrid orchestration increment

Date: 2026-11-08
Implementation revision under review:
**`ab9f8c55accfa0d88fbf57236e20662de8953017`**
Evidence commit: the commit that adds this file. Its parent is the
implementation revision above; this record is the only change it adds.
Branch: `feat/langgraph-hybrid-orchestration` (worktree
`~/software-factory-langgraph`)
Base: `f0d1545706fce719fc2c643ef47d2837b93e7411` (engine `main`, clean at start)

This record reports commands actually run and their observed output. Fixture
coverage is labelled as such; it is not presented as end-to-end production
acceptance.

## Environment

| Component | Identity |
| --- | --- |
| Development Python | 3.12.11 (`.venv` from `uv sync --locked`) |
| `langgraph` | 1.2.20 |
| `langgraph-checkpoint-sqlite` | 3.1.3 |
| Hermes | `v0.23.0+689.ga9ff979`, source `a9ff9799e273f525b82718cdc69fdec71b1dca5a` |
| Installed launcher | `~/software-factory-ops/bin/factory` (release identity in `evidence/g0-capture.json`) |

## Commands and observed results

```sh
uv sync --extra orchestration --locked                # resolved 72 packages
uv run --extra orchestration python -m pytest -q tests/test_orchestration_*.py
# 59 passed in 18.75s                      -> evidence/pytest-orchestration.txt
uv run --extra orchestration python -m pytest -q
# 741 passed, 2 skipped (live-host checks) -> evidence/pytest-full.txt
uv run --extra orchestration python -m ruff check .
# All checks passed!                      -> evidence/ruff-check.txt
uv run --extra orchestration python -m ruff format --check .
# 95 files already formatted              -> evidence/ruff-format.txt
git diff --check                          # exit 0, no output
```

## T1–T12 status with evidence type

| Test | Status | Evidence type |
| --- | --- | --- |
| T1 one effect owner | PASS | two real child processes, durable request count + single `admitted` event |
| T2 inspect/export provenance | PASS | real LangGraph SQLite checkpoint, attempt, candidate/base, engine release, next action |
| T3 crash windows | PASS | five process-death windows (worker before/after submit, after receipt, after checkpoint; gate after acceptance receipt); durable effect directories inspected |
| T4 stale/cancelled attempts | PASS | superseded attempt refused; cancellation idempotent; resume blocked |
| T5 evidence negatives | PASS | real `factory.evidence.verify_evidence` reason codes; duplicate-key guard |
| T6 independent review | PASS | self-approval refused; inconclusive holds; candidate-bound review refused |
| T7 repair budget | PASS | durable count across a restart; exactly three submissions |
| T8 installed identity | PASS | release/ready/deployment/policy digests, fingerprint recomputation, real installed verifier negative, and a full installed `factory run` acceptance on a disposable repo (run exit 0, verify exit 0) |
| T9 opt-in / rollback | PASS | disabled admission refuses before effect; drain allowed; checkpoints readable after cancel |
| T10 read-only capability | **BLOCKED** | no OS-level boundary; live reviewer refuses explicitly; offline adapters deny mutation |
| T11 mismatch holds | PASS | moved base, changed spec, changed graph version, unrecognized release |
| T12 metrics | PARTIAL | observed pilot metrics recorded (one run: 2 s, 10 transitions, 0 interventions, 0 repairs); historical baseline unavailable and reported |

## What was actually executed

- Offline pytest suite with the real engine verifier (`factory.evidence`).
- Real LangGraph graph compilation and SQLite checkpoint/resume across process
  death (child processes, durable targets inspected).
- A full installed acceptance on a disposable repository
  (`scripts/langgraph_pilot_acceptance.py --run`): `bin/factory run` started one
  busybox service (no host ports, no protected projects), exited 0, and
  `bin/factory verify` certified the evidence (exit 0, reason `ok`). Captured in
  `evidence/installed-acceptance.json` and `evidence/installed-acceptance/`.
- One graph pilot run whose acceptance and verification went through the
  installed launcher; the worker and reviewer were deterministic fixtures, so no
  model call or board mutation occurred. Result: `ready_for_authorized_completion`
  in 2 s over 10 stage transitions with 0 interventions and 0 repairs. Captured
  in `evidence/pilot-run.json` and `evidence/pilot-acceptance/`.
- Real installed launcher identity and read-only checks: `bin/factory verify
  --help` (exit 0) and `bin/factory verify` against a nonexistent artifacts
  directory (exit 1, reason `manifest_missing`). Captured in
  `evidence/g0-capture.json`.
- One live Hermes profile invocation (`hermes -p factory-qa chat -q ... --format
  stream-json`) to prove the supported worker transport and session correlation.
  Captured in `evidence/g0-capture.json`.

## What was **not** executed

- No acceptance against a production, protected or shared workload. The one
  Compose run used a disposable repository with a single busybox service, no host
  ports and no mounts.
- No Kanban card was created, claimed, reassigned, completed or blocked by the
  orchestration code; no board database was written.
- No Hermes profile, credential, cron entry, route or protected workload was
  modified. No release was activated and no default admission changed.
- No production promotion, push of `main`, merge or deployment.

## Known limitations

1. **T10 live read-only review is blocked.** Hermes exposes write-capable tools
   (`write_file`, `patch`, `terminal`, `execute_code`) in the same UID and has no
   read-only toolset or sandbox. The adapter refuses with
   `capability_unenforced` instead of claiming enforcement. Extension proposal:
   card `t_2abae28a`.
2. **The pilot used fixture worker/reviewer adapters.** Installed acceptance and
   verification were real; model work was not executed, because no board card or
   Hermes attempt was enrolled for this increment.
3. **Live worker completion contract** requires `worker-result.json` containing
   the attempt identity and candidate; a zero-exit worker without it is
   `ambiguous` and holds.
4. **T12 baseline** does not exist on this host; the runbook defines the
   measurements and reports the gap rather than a numerical improvement claim.
   The pilot contributes a single observed run (n=1) and no uncertainty interval.
5. The pilot serialises one graph evaluation per state directory; SQLite is
   local durability, not multi-host coordination.

## Reproduce

See `docs/langgraph-hybrid/operator-runbook.md`. The shortest path:

```sh
cd ~/software-factory-langgraph
uv sync --extra orchestration --locked
uv run --extra orchestration python -m pytest -q tests/test_orchestration_*.py
uv run --extra orchestration python -m pytest -q
uv run --extra orchestration python -m ruff check .
uv run --extra orchestration python -m ruff format --check .
git diff --check
```

Installed acceptance + graph pilot (Docker; disposable scratch directory only):

```sh
uv run --extra orchestration python scripts/langgraph_pilot_acceptance.py --run \
  --scratch ~/.hermes/profiles/factory-runner/cache/scratch/lg-installed-acceptance
```
