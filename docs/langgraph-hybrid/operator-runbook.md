# Operator runbook: opt-in LangGraph orchestration

This increment is **off by default**. Hermes Kanban remains the scheduler and
lifecycle owner. Nothing here promotes, pushes, merges, deploys, changes default
admissions, or touches protected workloads.

## Install (development / pilot host)

```sh
cd ~/software-factory-langgraph   # feature worktree
uv sync --extra orchestration --locked
```

The `orchestration` extra pins `langgraph>=1.2,<2` and
`langgraph-checkpoint-sqlite>=3.1,<4`; versions are resolved in `uv.lock`. The
standalone verification engine (`factory`) never imports LangGraph: a test
blocks the import and asserts the package has no reference to it.

## Tests

```sh
uv run --extra orchestration python -m pytest -q
uv run --extra orchestration python -m pytest -q tests/test_orchestration_*.py
uv run --extra orchestration python -m ruff check .
uv run --extra orchestration python -m ruff format --check .
git diff --check
```

Offline by construction: `tests/conftest.py` blocks Docker and network
subprocesses. Tests that need the installed launcher skip when it is absent.

## Enrolment (explicit, per host)

```sh
factory-orchestrate --state-dir ~/.hermes/factory-orchestration admission \
  --enable --enroll t_deadbeef
```

`admission.json` holds `{"enabled": bool, "enrolled_cards": [...]}`. Missing file
means disabled. `--disable` stops new admissions; runs already paused can still
be inspected, drained or cancelled.

## Run one card (inside the claimed worker)

The graph executes **in the process that already owns the Hermes attempt**. Its
attempt identity comes from the dispatcher-granted environment
(`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_PROFILE`). There is no
separate scheduler to start.

```sh
factory-orchestrate --state-dir ~/.hermes/factory-orchestration run \
  --assignment card.json
```

`card.json` is a reviewed assignment (never generated from prose):

```json
{
  "board_id": "software-factory",
  "card_id": "t_deadbeef",
  "repository": "/abs/repo",
  "workspace_path": "/abs/repo-worktree",
  "immutable_base_sha": "<40-hex>",
  "spec_reference": "/abs/spec.md",
  "spec_digest": "sha256:<64 hex>",
  "template_id": "bounded-card-v1",
  "worker_profile": "factory-runner",
  "reviewer_profile": "factory-qa",
  "acceptance_argv": ["~/software-factory-ops/bin/factory", "run", "..."],
  "acceptance_artifacts": "/abs/artifacts",
  "engine_release_reference": "installed:/path@sha256:...",
  "engine_fingerprint": "sha256:<64 hex>",
  "host_policy_digest": "sha256:<64 hex>",
  "max_repairs": 2
}
```

The command prints a JSON `DriveResult` (`status`, `stage`, `awaiting`). An
`awaiting` result is a persisted wait: the process has exited.

Independent review is refusal-by-default. To use the enforced R2-A reviewer,
pass `--reviewer-sandbox` (plus `--reviewer-root`, and either
`--review-model/--review-base-url` or a readable `--review-provider-profile`);
otherwise the graph holds at `request_independent_review` with
`capability_unenforced`. `scripts/live_review_fixture.py` is the reproducible
host-side exercise of that path.

## Inspect / resume / cancel / export

```sh
factory-orchestrate --state-dir <dir> inspect t_deadbeef
factory-orchestrate --state-dir <dir> resume t_deadbeef \
  --resolved-hold "worker request awaited" --artifacts <dir>
factory-orchestrate --state-dir <dir> resume t_deadbeef \
  --payload /abs/operator-receipt.json --resolved-hold "worker request awaited"
factory-orchestrate --state-dir <dir> cancel t_deadbeef --by operator --reason "..."
factory-orchestrate --state-dir <dir> export t_deadbeef --output /abs/run.json
```

`resume` without `--payload` reconciles the pending operation from the
adapter's durable artifacts and adopts that receipt. A worker or reviewer is
adopted only when its own `stream.jsonl` carries the child-written `result`
event; the worker receipt then comes from its result file or its committed
workspace HEAD (a dirty or unchanged workspace is `failed`), and the reviewer
receipt is re-audited from the transcript and bound to the candidate, base,
spec digest, evidence digest and graph version. A transcript without a `result`
event is `effect_unknown` and holds: it may still be running, so nothing is
replayed. Delete no artifact to make a run look recoverable.

`inspect` shows: board/card/run/attempt, graph and schema version, stage and last
reason, worker/session, candidate/base, installed release, engine fingerprint,
pending effect, repair budget, evidence path, checkpoint state, observed metrics
and the permitted next action. Checkpoint state and reconciled external state
are reported separately.

`resume` requires the current Hermes attempt and must name the hold it resolves.
A changed base, spec, graph version, schema version, engine fingerprint or host
policy digest fails closed with `version_mismatch`; there is no silent migration.
`resume` is refused after a cancellation request.

## Metrics (T12)

`inspect`/`export` record observed values only:

- `stage_transitions`, `elapsed_s`, `interventions` (holds + cancel requests),
  `repairs`, `diagnosis.current_stage`, `diagnosis.last_reason_code`,
  `diagnosis.pending_effects`.

`metrics.baseline.available` is `false` with the reason that no historical
intervention/diagnosis measurements were recorded on this host. No improvement
claim is made; the pilot produces the first observed values.

## Storage, retention, export

- Default state directory in this runbook: `~/.hermes/factory-orchestration`
  (override with `--state-dir`; never inside a source repository).
- `orchestration.sqlite`, `checkpoints.sqlite`, `coordinator.lock`,
  `coordinator.owner.json`, `admission.json`.
- Permissions `0700` directory / `0600` files; checkpoints contain references
  and bounded summaries, never credentials or transcripts.
- External tracing is not configured. No automatic pruning in this increment:
  `export` a run before any manual archival. Deleting checkpoints to make a card
  look recoverable is prohibited — it destroys the audit trail.

## Rollback

1. `admission --disable` (no new graphs).
2. Do not hand an active card to the legacy path while a worker or acceptance
   operation may still be running.
3. `cancel <card>` and reconcile: owned subprocesses are terminated by process
   group; recorded effects are retained if confirmed; unknown effects hold with
   `cancellation_pending`.
4. `inspect`/`export` the run, record outstanding effects, then release the card
   through the owning Hermes lifecycle.
5. Ordinary cards continue through unchanged Hermes dispatch.

## Acceptance traceability (T1–T12)

| Test | Evidence | Where |
| --- | --- | --- |
| T1 one effect owner | two concurrent child processes; exactly one request and one `admitted` event; loser refuses | `tests/test_orchestration_recovery.py::test_two_concurrent_admissions_have_one_effect_owner` |
| T2 inspect/export provenance | real checkpoint + attempt + candidate/base + engine release + next action | `tests/test_orchestration_flow.py::test_happy_path_returns_a_proposal_with_full_provenance`, `tests/test_orchestration_cli.py::test_inspect_and_export_expose_provenance` |
| T3 crash windows | process death before submit / after submit / after receipt / after checkpoint, after an acceptance receipt, and after a review receipt (durable review result adopted, no resubmission) | `tests/test_orchestration_recovery.py::test_crash_*`, `::test_crash_after_checkpoint_does_not_repeat_confirmed_work`, `::test_crash_after_acceptance_receipt_does_not_rerun_the_gate`, `::test_crash_after_review_receipt_adopts_without_resubmitting` |
| T4 stale/cancelled attempts | superseded attempt refused; cancellation idempotent; resume blocked | `tests/test_orchestration_recovery.py::test_superseded_attempt_cannot_resume_or_act`, `::test_cancellation_is_idempotent_and_blocks_resume` |
| T5 evidence negatives | real engine verifier reasons for diagnostic, teardown, safety, candidate, log tamper, missing log, duplicate keys | `tests/test_orchestration_evidence.py` |
| T6 independent review | implementer self-approval refused; inconclusive review holds; review bound to another candidate refused | `tests/test_orchestration_flow.py::test_implementer_cannot_approve_its_own_candidate`, `::test_review_bound_to_a_different_candidate_is_rejected`, `::test_inconclusive_review_holds_without_repair` |
| T7 repair budget | three submissions, two repairs, restart does not reset | `tests/test_orchestration_flow.py::test_review_changes_cycle_through_bounded_repairs`, `::test_restart_cannot_reset_the_repair_budget` |
| T8 installed identity | launcher/ready/deployment/policy hashes, engine fingerprint recomputation, installed verifier negative, a full installed `factory run` acceptance on a disposable repo, and a graph run (real Hermes worker + attested reviewer) whose acceptance and verification go through the installed launcher | `tests/test_orchestration_installed.py`, `scripts/langgraph_pilot_acceptance.py`, `scripts/langgraph_hermes_acceptance.py`, `evidence/installed-acceptance.json`, `evidence/hermes-graph-run/run.json`, `evidence/g0-capture.json` |
| T9 opt-in/rollback | admission disabled refuses before any effect; active graph drains; rollback keeps checkpoints readable | `tests/test_orchestration_installed.py` |
| T10 read-only capability | **PASS for reviewer tool effects, enforced by an executed attestation** (mounts, image, uid, network, capabilities, tool surface); refusal remains the default without `--reviewer-sandbox`; not a claim of whole-agent containment | `evidence/reviewer-sandbox/attestation.json`, `tests/test_orchestration_reviewer_sandbox.py`, `tests/test_orchestration_hermes.py::test_live_reviewer_refuses_unenforced_readonly`, `reviewer-sandbox.md` |
| T11 mismatch holds | moved base, changed spec, changed graph version, unrecognized release | `tests/test_orchestration_recovery.py::test_moved_base_and_changed_spec_fail_closed`, `::test_unrecognized_release_and_graph_version_hold` |
| T12 metrics | observed intervention/diagnosis measurements on two runs (fixture pilot and real-worker graph run); explicit baseline gap | `tests/test_orchestration_cli.py::test_metrics_report_observed_values_without_a_baseline`, `evidence/pilot-run.json`, `evidence/hermes-graph-run/run.json` |

## Pilot observation (T8/T12)

```sh
uv run --extra orchestration python scripts/langgraph_pilot_acceptance.py --run \
  --scratch ~/.hermes/profiles/factory-runner/cache/scratch/lg-installed-acceptance
```

The script builds a disposable repository (one busybox service, no host ports,
no mounts), runs `bin/factory run` and `bin/factory verify`, then drives one
graph whose acceptance goes through the installed launcher. Observed on
2026-11-08: installed run exit `0`, installed verify exit `0` (`ok`), graph
status `ready_for_authorized_completion`, 10 stage transitions, 1 s elapsed,
0 interventions, 0 repairs. Captured in `evidence/installed-acceptance.json`,
`evidence/pilot-run.json` and the two `evidence/*-acceptance/` directories.

The installed runner writes evidence into `<artifacts-root>/<factory-project>/`;
`InstalledLauncherGate` discovers exactly one current project directory and
reports `inconclusive` when the choice is ambiguous — it never guesses.

The worker/reviewer adapters in that pilot are deterministic fixtures. The
separate acceptance below uses the real adapters:

```sh
uv run --extra orchestration python scripts/langgraph_hermes_acceptance.py --run \
  --scratch ~/.hermes/profiles/factory-runner/cache/scratch/lg-hermes-acceptance
```

It executes one `factory-card/v1` graph whose implementation step is a real
`hermes -p factory-runner chat` child process (committing the candidate) and
whose independent review runs in the attested R2-A container; acceptance and
verification go through the installed `bin/factory`. The model behind both
agents is a loopback scripted provider from the installed Hermes test fixtures,
so transport, tools, git, container, attestation and gate are real while model
text is deterministic. Result: `ready_for_authorized_completion`, candidate
commit, approval reference, 0 repairs; receipts in `evidence/hermes-graph-run/`.

A real-provider review is exercised separately and reproducibly:

```sh
.venv/bin/python scripts/live_review_fixture.py --run \
  --provider-profile factory-runner \
  --out docs/langgraph-hybrid/evidence/reviewer-sandbox \
  --scratch ~/.hermes/profiles/factory-runner/cache/scratch/lg-live-review
```

The script builds a disposable candidate/evidence fixture and runs the
attestation and the review in the same adapter instance, so the receipt and the
transcript cannot drift; the attestation digest is recorded in the run summary.
It requires Docker and the named profile's provider credential.

## Pilot harness (installed, frozen)

`scripts/langgraph_rollout_pilot.py` is part of the release and is the only
sanctioned way to drive a real-card pilot:

```sh
<release>/engine/.venv/bin/python <release>/engine/scripts/langgraph_rollout_pilot.py \
  --run-root <new unique dir> --launcher <release>/bin/factory \
  --card <card> --attempt-run-id <current run> --attempt-profile factory-runner \
  --claim-lock <current claim>
<release>/engine/.venv/bin/python <release>/engine/scripts/langgraph_rollout_pilot.py \
  --status --run-root <dir>
```

It refuses an existing run root, creates the disposable repository/workspace and
acceptance wrapper under it, drives `run`/`resume` (including a duplicate-run
refusal and a premature-resume refusal while the worker is live), waits only on
the operation key the graph is actually awaiting, and stops with
`effect_unknown` as a safety hold. A deliberate one-shot acceptance failure
exercises exactly one bounded repair. `pilot-summary.json` always carries an
`exit` and `final_status`.

## Known gaps in this increment

- **T10 scope:** enforced for the reviewer's tool effects, not whole-agent
  containment. The host launcher/profile, provider egress, in-container
  `execve`, the image and the Docker daemon remain trusted; see
  `reviewer-sandbox.md` for the full residual list.
- **T8 production acceptance:** the installed acceptance and the real-worker
  graph both ran on disposable repositories. Enrollment on a real card still
  needs an operator decision and a current Hermes attempt.
- **T12 baseline:** no historical measurements exist; the runs contribute two
  observed samples (n=1 fixture pilot, n=1 real-worker graph) and report the gap
  rather than an improvement claim.
- **Live worker completions:** the live worker contract derives the candidate
  from the workspace HEAD when no `worker-result.json` is written; a dirty
  workspace or no new commit is `failed`, never success.
- No production admission, release activation or default change is included.
