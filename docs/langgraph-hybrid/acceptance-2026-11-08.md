# Acceptance record: LangGraph hybrid orchestration increment

Date: 2026-11-08
Implementation revision under review:
**`bfa7608c020555fdf6a10e4f45e9be9de74a7a41`**
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
# 57 passed in 17.34s                      -> evidence/pytest-orchestration.txt
uv run --extra orchestration python -m pytest -q
# 739 passed, 2 skipped (live-host checks) -> evidence/pytest-full.txt
uv run --extra orchestration python -m ruff check .
# All checks passed!                      -> evidence/ruff-check.txt
uv run --extra orchestration python -m ruff format --check .
# 94 files already formatted              -> evidence/ruff-format.txt
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
| T8 installed identity | PARTIAL | release/ready/deployment/policy digests, fingerprint recomputation, real installed verifier negative; full Docker acceptance **not run** |
| T9 opt-in / rollback | PASS | disabled admission refuses before effect; drain allowed; checkpoints readable after cancel |
| T10 read-only capability | **BLOCKED** | no OS-level boundary; live reviewer refuses explicitly; offline adapters deny mutation |
| T11 mismatch holds | PASS | moved base, changed spec, changed graph version, unrecognized release |
| T12 metrics | PARTIAL | observed intervention/diagnosis metrics; historical baseline unavailable and reported |

## What was actually executed

- Offline pytest suite with the real engine verifier (`factory.evidence`).
- Real LangGraph graph compilation and SQLite checkpoint/resume across process
  death (child processes, durable targets inspected).
- Real installed launcher invocations, read-only and offline:
  `bin/factory verify --help` (exit 0) and `bin/factory verify` against a
  nonexistent artifacts directory (exit 1, reason `manifest_missing`).
  Captured in `evidence/g0-capture.json`.
- One live Hermes profile invocation (`hermes -p factory-qa chat -q ... --format
  stream-json`) to prove the supported worker transport and session correlation.
  Captured in `evidence/g0-capture.json`.

## What was **not** executed

- No Docker / Compose acceptance run; no container was started.
- No Kanban card was created, claimed, reassigned, completed or blocked by the
  orchestration code; no board database was written.
- No Hermes profile, credential, cron entry, route or protected workload was
  modified. No release was activated and no default admission changed.
- No production promotion, push, merge or deployment.

## Known limitations (unchanged by this increment)

1. **T10 live read-only review is blocked.** Hermes exposes write-capable tools
   (`write_file`, `patch`, `terminal`, `execute_code`) in the same UID and has no
   read-only toolset or sandbox. The adapter refuses with
   `capability_unenforced` instead of claiming enforcement.
2. **Full installed acceptance is not demonstrated.** It needs Docker and an
   authorized disposable project; that authorization is not part of this card.
3. **Live worker completion contract** requires `worker-result.json` containing
   the attempt identity and candidate; a zero-exit worker without it is
   `ambiguous` and holds.
4. **T12 baseline** does not exist on this host; the runbook defines the
   measurements and reports the gap rather than a numerical improvement claim.
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
