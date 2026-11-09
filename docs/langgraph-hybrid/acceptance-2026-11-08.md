# Acceptance record: LangGraph hybrid orchestration increment

Date: 2026-11-08
Implementation revision under review:
**`2971e86453807acefac5d42831a9936839b7e76d`**
Evidence commit: the commit that adds this file. Its parent is the
implementation revision above; this record and the refreshed evidence are the
only changes it adds.
Branch: `feat/langgraph-hybrid-orchestration` (worktree
`~/software-factory-langgraph`)
Base: `f0d1545706fce719fc2c643ef47d2837b93e7411` (engine `main`, clean at start)

This record reports commands actually run and their observed output. Fixture
coverage is labelled as such; it is not presented as end-to-end production
acceptance.

## Environment

| Component | Identity |
| --- | --- |
| Development Python | 3.12.11 (`.venv` from `uv sync --extra orchestration --locked`) |
| `langgraph` | 1.2.20 (pinned `>=1.2,<2`) |
| `langgraph-checkpoint-sqlite` | 3.1.3 (pinned `>=3.1,<4`) |
| Hermes | `v0.23.0+689.ga9ff979`, source `a9ff9799e273f525b82718cdc69fdec71b1dca5a` |
| Installed launcher | `~/software-factory-ops/bin/factory`; release engine fingerprint `sha256:8c86d3970f922fbade58245d558e87a8b5318c2cf5570f679dbd411949525d33`, host-policy digest `sha256:39c1bba4181254b02129d336d5e95636783c2e50d769d8ff8e3b4365d472d9fc` (complete identity in `evidence/hermes-graph-run/run.json` → `release`) |

The standalone verification engine never imports LangGraph: a regression test
blocks the import and asserts the package has no reference to it.

## Commands and observed results

```sh
.venv/bin/python -m pytest -q tests/test_orchestration_*.py
# 87 passed in 19.94s                      -> evidence/pytest-orchestration.txt
.venv/bin/python -m pytest -q
# 774 passed, 2 skipped, 1 warning in 52.95s -> evidence/pytest-full.txt
.venv/bin/ruff check .
# All checks passed!                      -> evidence/ruff-check.txt
.venv/bin/ruff format --check .
# 101 files already formatted
git diff --check                          # exit 0, no output
```

Host-side acceptance (real Docker, disposable scratch only):

```sh
.venv/bin/python scripts/langgraph_pilot_acceptance.py --run \
  --scratch .../scratch/lg-installed-acceptance-r2
# installed run exit 0; installed verify exit 0 (reason ok, 21/21 checks);
# graph status ready_for_authorized_completion, 10 transitions, 2 s, 0/0
# -> evidence/installed-acceptance.json, evidence/pilot-run.json

.venv/bin/python scripts/langgraph_hermes_acceptance.py --run \
  --scratch .../scratch/lg-hermes-acceptance
# real Hermes worker + attested reviewer + installed gate
# ready_for_authorized_completion, 11 transitions, 41 s, 0 interventions,
# 0 repairs, candidate 73a225b0…, approval sha256:bbef737f…
# -> evidence/hermes-graph-run/

.venv/bin/python scripts/live_review_fixture.py --run \
  --provider-profile factory-runner \
  --out docs/langgraph-hybrid/evidence/reviewer-sandbox
# attestation enforced=True; one real provider review; verdict approve;
# 17 tool_use events; audit violations []
# -> evidence/reviewer-sandbox/live-review-*
```

## T1–T12 status with evidence type

| Test | Status | Evidence type |
| --- | --- | --- |
| T1 one effect owner | PASS | two real child processes; one admitted run and one durable request; loser refuses before effect (`tests/test_orchestration_recovery.py`) |
| T2 inspect/export provenance | PASS | real LangGraph SQLite checkpoint resumed after process death; inspect/export carry attempt, candidate/base, installed release, engine fingerprint and next action (`tests/test_orchestration_flow.py`, `tests/test_orchestration_cli.py`, `evidence/hermes-graph-run/run.json`) |
| T3 crash windows | PASS | process death before submit, after submit, after receipt, after checkpoint, after an acceptance receipt, and after a review receipt; durable directories/SQLite inspected; the review receipt is adopted, never resubmitted (`tests/test_orchestration_recovery.py`) |
| T4 stale/cancelled attempts | PASS | superseded attempt cannot resume or act; cancellation idempotent and blocks resume; dispatcher grant required (`tests/test_orchestration_recovery.py`) |
| T5 evidence negatives | PASS | real `factory.evidence.verify_evidence` reason codes for diagnostic/no-gates, teardown, safety, wrong candidate/base, tampered/missing logs, duplicate JSON keys (`tests/test_orchestration_evidence.py`) |
| T6 independent review | PASS | implementer self-approval refused; inconclusive holds; review bound to a different candidate refused; approval invalidated by a changed candidate (`tests/test_orchestration_flow.py`) |
| T7 repair budget | PASS | original + two repairs exhausts the budget; restart cannot reset it (`tests/test_orchestration_flow.py`) |
| T8 installed identity | PASS | pinned launcher/ready/deployment/policy identity recomputed; installed verifier negative; full installed `factory run` + `verify` on a disposable repo; graph whose acceptance/verification used the installed launcher (`tests/test_orchestration_installed.py`, `evidence/installed-acceptance.json`, `evidence/hermes-graph-run/`) |
| T9 opt-in / rollback | PASS | disabled admission refuses before effect; drain allowed; rollback keeps checkpoints readable and legacy cards operable (`tests/test_orchestration_installed.py`) |
| T10 read-only capability | PASS (tool effects, enforced) | executed attestation: effective config, preflight through the real tool stack, container inspection; `:ro` mounts, pinned image digest, non-root 10001, `--read-only` rootfs, `NetworkMode=none`, `CapEff=0`, no socket/CLI, egress unreachable; write/patch → EROFS; host canaries unreadable; refusal remains the default without `--reviewer-sandbox` (`evidence/reviewer-sandbox/attestation.json`, `tests/test_orchestration_reviewer_sandbox.py`) |
| T11 mismatch holds | PASS | moved base, changed spec, changed graph/schema, changed engine/policy identity and unrecognized release produce holds; no silent migration (`tests/test_orchestration_recovery.py`) |
| T12 metrics | PARTIAL | observed values only: fixture pilot 2 s / 10 transitions / 0 interventions / 0 repairs; real-worker graph 41 s / 11 transitions / 0 interventions / 0 repairs; historical baseline unavailable and reported as a gap (`evidence/pilot-run.json`, `evidence/hermes-graph-run/run.json`) |

## What was actually executed

- Offline pytest suite with the real engine verifier (`factory.evidence`).
- Real LangGraph graph compilation and SQLite checkpoint/resume across process
  death (child processes, durable targets inspected).
- One full installed acceptance on a disposable repository:
  `bin/factory run` started one busybox service (no host ports, no protected
  projects), exited 0, and `bin/factory verify` certified the evidence (exit 0,
  reason `ok`, 21 checks).
- One graph pilot run whose acceptance and verification went through the
  installed launcher; worker/reviewer were deterministic fixtures.
- One graph run with the **real** Hermes worker adapter (`hermes -p
  factory-runner chat`, loopback scripted provider) and the **real** attested
  sandboxed reviewer, through the installed gate. Candidate commit, evidence
  digest and approval reference are recorded; `0` repairs.
- One **real-provider** review through the attested sandbox, reproducible with
  `scripts/live_review_fixture.py`; verdict `approve`, 17 audited tool uses,
  no scope violation. Its attestation receipt (`live-review-attestation.json`)
  is digest-bound to the run summary.
- A standalone executed attestation of the frozen candidate and the selected
  evidence tree (`evidence/reviewer-sandbox/attestation.json`, with explicit
  provenance revision).
- Real installed launcher identity and read-only checks: `bin/factory verify
  --help` (exit 0) and `bin/factory verify` against a nonexistent artifacts
  directory (exit 1, reason `manifest_missing`).
- One live Hermes profile invocation (`hermes -p factory-qa chat -q ... --format
  stream-json`) to prove the supported worker transport and session correlation.

## What was **not** executed

- No acceptance against a production, protected or shared workload. Both
  Compose runs used disposable repositories with a single busybox service, no
  host ports and no mounts.
- No Kanban card was created, claimed, reassigned, completed or blocked by the
  orchestration code; the graph's board access is a read-only projection. No
  board database was written.
- No Hermes profile, credential, cron entry, route or protected workload was
  modified. No release was activated and no default admission changed.
- No production promotion, push of `main`, merge or deployment.

## Known limitations

1. **T10 scope.** The attestation enforces the reviewer's **tool effects**, not
   whole-agent containment: the host launcher/profile, provider egress,
   in-container `execve`, the image and the Docker daemon remain trusted. The
   sandbox is reachable only through the explicit `--reviewer-sandbox` flag;
   every other path refuses with `capability_unenforced`.
2. **T8 scope.** The installed acceptance and the real-worker graph ran on
   disposable repositories. Enrollment on a real card still needs an operator
   decision and a current Hermes attempt.
3. **T12 baseline.** No historical intervention/diagnosis measurements exist on
   this host. The recorded samples are n=1 each; no improvement claim is made
   and no uncertainty interval is computable.
4. **Local durability.** One coordinator and one active graph card per state
   directory; SQLite is local durability, not multi-host coordination.
5. **Scripted model text in the graph acceptance.** The graph run's model is a
   loopback scripted provider (installed Hermes test fixture). Transport, tools,
   git, container, attestation, gate and review audit are real; model reasoning
   is not exercised there. The separate live review covers a real provider.

## Reproduce

See `docs/langgraph-hybrid/operator-runbook.md`. The shortest offline path:

```sh
cd ~/software-factory-langgraph
uv sync --extra orchestration --locked
uv run --extra orchestration python -m pytest -q tests/test_orchestration_*.py
uv run --extra orchestration python -m pytest -q
uv run --extra orchestration python -m ruff check .
uv run --extra orchestration python -m ruff format --check .
git diff --check
```

Host-side acceptance, graph run and live review are documented with their exact
flags in the runbook and `reviewer-sandbox.md`.
