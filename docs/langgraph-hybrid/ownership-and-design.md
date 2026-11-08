# LangGraph hybrid orchestration: ownership and design

Status: opt-in pilot increment. Not default admission, not fleet rollout, and
not a replacement for Hermes scheduling or the installed factory gates.

## Ownership (one authority per kind of state)

| State | Owner |
| --- | --- |
| Card identity, dependencies, claims, attempts, worker spawning, heartbeat, review handoff, final transition | **Hermes Kanban** |
| Intra-card stage, checkpoint, requested next bounded operation, interpretation of structured worker/gate outputs | **`factory_orchestration`** |
| Policy admission, gate execution, evidence validity, promotion semantics | **`factory` (engine)** |
| Qualitative review against a frozen candidate | **Independent reviewer profile** |
| Development context/provenance references | **Entire** (not runtime state) |

The graph cannot claim or reassign a card, spawn a Kanban-managed worker, write
the board database, promote a ref or publish anything. Its terminal result is a
*proposal*: `ready_for_authorized_completion`.

## Topology (`factory-card/v1`)

```
admit -> validate_spec -> prepare_workspace -> request_implementation
      -> await_worker -> bind_candidate -> run_acceptance -> verify_evidence
      -> request_independent_review -> await_review -> return_result -> END

failure -> classify -> request_repair | hold_budget | hold_infrastructure
                    | hold_reconciliation | hold_operator

cancellation -> cancel_owned_operations -> reconcile -> END
```

A wait is persisted state: `await_worker` and `await_review` raise a LangGraph
`interrupt()` and the process exits the call. Resume is a new bounded entry
inside a currently authorized Hermes attempt; there is no sleeping subprocess
and no admission daemon.

## State schema

`factory_orchestration/state.py` defines `FactoryRunState` (`FactoryRun v1`,
`state_schema_version = 1`). It stores references and bounded summaries only:

- board/card/run/attempt, owner token reference, graph and schema version;
- spec reference + digest, template id;
- repository, workspace, immutable base, candidate;
- engine release reference, engine fingerprint, host policy digest;
- stage, sequence, status;
- worker profile/session/request, reviewer profile/request/result;
- repair count, max repairs, deadline, cost (unknown is `unknown`, never `0`);
- operation references, artifact references, evidence reference/digest;
- approval reference, Entire references (unresolved is explicit);
- last reason code/detail, cancellation flag.

All identifiers, digests, paths and enums are validated before any effect.
Candidate and base are full 40-hex commit ids; ref names are refused.

## Durable storage

- `orchestration.sqlite`: admission binding, operation journal, approvals,
  cancel requests, bounded event/metrics log.
- `checkpoints.sqlite`: LangGraph checkpoints, written by
  `langgraph-checkpoint-sqlite` (pinned via the optional `orchestration` extra).
- `coordinator.lock` / `coordinator.owner.json`: one local graph evaluation at a
  time. `flock` is released by the kernel on process death; locks are never
  stolen by age.

State directories are created `0700`; database files are `0600`. Runtime storage
never lives in a source repository or a companion profile.

## Effect protocol (crash safety)

Stable key = `sha256(run-id | stage | repair-index | input-digest)`. Intent is
persisted before the effect; the request handle and receipt are persisted after.

| Journal state | Probe | Action |
| --- | --- | --- |
| `prepared` | n/a | submit (no effect was started) |
| `submitted` | absent | submit (target proved no effect) |
| `submitted` | present | adopt the existing effect, never resubmit |
| `submitted` | unknown | HOLD (`effect_unknown`) |
| `confirmed` | n/a | replay the state update only |

The graph marks an operation `submitted` *before* invoking the adapter, so a
crash during submission also lands on the reconcile path rather than on a blind
retry.

## Controls

- **Repair budget:** durable in `runs.repair_count`; consumed atomically with
  `UPDATE ... WHERE repair_count < max_repairs`. A process restart cannot reset
  it. Pilot limit: two repairs after the original implementation.
- **Cancellation:** a durable cancel request closes admission for new effects at
  the next stage boundary; owned subprocesses are terminated by process group;
  recorded effects are reconciled (retained if confirmed, held if unknown).
  Repeated cancellation is idempotent.
- **Approvals:** bound to reviewer, run, candidate, base, spec digest and
  evidence digest. Changing any of them invalidates the approval. An
  implementer's own approval never releases acceptance. A chat string or bare
  boolean is not accepted as authority.
- **Read-only capability:** enforced for offline adapters (mutation raises).
  Live reviewer execution is **blocked** (`capability_unenforced`) because the
  host has no OS-level read-only boundary; see `g0-capabilities.md`.

## Reason codes

`invalid_assignment`, `admission_disabled`, `workspace_conflict`,
`version_mismatch`, `stale_attempt`, `cancellation_pending`, `cancelled`,
`budget_exhausted`, `worker_failed`, `worker_unavailable`, `gate_unavailable`,
`protocol_error`, `effect_unknown`, `evidence_rejected`, `review_changes`,
`review_inconclusive`, `capability_unenforced`.

The verifier's own reason (for example `mode_not_acceptance`,
`teardown_failed`, `artifact_size_mismatch:compose.log`) is preserved in
`last_reason_detail`; structural evidence failures hold for an operator instead
of consuming repair budget.

## Explicit limits

- One local coordinator and one active graph card per state directory (pilot).
- SQLite is local durability, not multi-host coordination.
- A checkpoint is not a transaction across Git, Hermes, Docker and APIs;
  external effects reconcile through their own targets.
- Graph structure is not OS isolation. This is not a sandbox for hostile code.
- Publication, push, merge, deployment and local promotion are not nodes.
- No automatic replay of ambiguous external effects; the graph holds.
