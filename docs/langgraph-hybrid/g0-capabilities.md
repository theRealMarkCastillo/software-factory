# G0 capability discovery: supported Hermes integration surface

Status: discovery complete on this host, 2026-11-08. Runtime behaviour was read
from the installed Hermes checkout and exercised read-only where possible. No
Hermes profile, credential, cron entry, board route, runtime database or
protected workload was modified.

Provenance:

- Hermes source: `~/.hermes/hermes-agent` at
  `a9ff9799e273f525b82718cdc69fdec71b1dca5a` (`Hermes Agent v0.23.0+689.ga9ff979`).
- Factory engine: `f0d1545706fce719fc2c643ef47d2837b93e7411` (start of this work).
- Ops launcher: `~/software-factory-ops/bin/factory` at
  `5d9b65d96fcf7de51b66c798392b4bea17ca7fbf`.
- Captured command output: `docs/langgraph-hybrid/evidence/g0-capture.json`.

## 1. Current attempt identity

The dispatcher grants each worker an attempt through environment variables in
`hermes_cli/kanban_db_dispatch.py:2754-2860` (`_default_spawn`):

- `HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_CLAIM_LOCK`,
  `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_DB`, `HERMES_KANBAN_WORKSPACE`,
  `HERMES_KANBAN_BRANCH`, `HERMES_PROFILE`, `HERMES_SESSION_SOURCE=kanban`.

The attempt is opened atomically by `_claim_and_open_run`
(`hermes_cli/kanban_db.py:2124`) from a `ready` (or `review`) status only, so a
`running` card is never re-claimed by another dispatcher tick.

Read-only observation is `hermes kanban --board <slug> show <card> --json`.
Captured output (`g0-capture.json` → `kanban_show.stdout`) shows a live shape
that matters to the adapter:

```
task.status = "running", task.claim_lock = null, task.current_run_id = null
runs = [{id: 356, profile: "factory-runner", status: "running", outcome: null}]
```

**Finding:** `show --json` deliberately does not expose `claim_lock` or
`current_run_id`, so those fields cannot be used as the read-only ownership
check. Ownership is validated from the dispatcher grant itself
(`HERMES_KANBAN_TASK` + `HERMES_KANBAN_RUN_ID` + `HERMES_KANBAN_CLAIM_LOCK`,
held by this process) plus the board's live `running` run ids for the card. The
run-scoped heartbeat is a **liveness** signal only; it authorizes nothing and a
failed heartbeat never substitutes for missing ownership evidence.
`claim_lock` is carried only for diagnostics.

## 2. Worker profile invocation with durable correlation

Supported CLI surface (pre-argparse global flag, `hermes_cli/_parser.py:16`):

```sh
hermes -p <profile> chat -q <prompt> --oneshot --format stream-json \
  [--max-turns N] [--in <workspace>]
```

Captured execution (`g0-capture.json` → `live_profile_invocation`) exited `0`
and emitted:

```json
{"type":"system","subtype":"init","session_id":"local-f3a6...","model":""}
{"type":"result","session_id":"local-f3a6...","exit_code":0,"text":"OK",...}
```

**Finding:** `session_id` plus the terminal `result.exit_code` is a durable,
correlatable outcome. The adapter records the session id and the child process
group it owns, and never inherits dispatcher ownership variables (they are
scrubbed before spawn; see `adapters/hermes.py::OWNERSHIP_ENV`).

The dispatcher itself no longer launches `hermes -p ... chat`; it launches
`hermes_cli.kanban_worker_client` (`kanban_db_dispatch.py:2697`), which asks the
profile owner's gateway to admit the claim. That path is what schedules cards,
and this adapter does not use or emulate it.

## 3. Heartbeat and cancellation

- `hermes kanban heartbeat <card>` is run-scoped: `_cmd_heartbeat`
  (`hermes_cli/kanban.py:312`) reads `HERMES_KANBAN_RUN_ID`
  (`_worker_run_id_for`, line 815) and `heartbeat_worker`
  (`kanban_db_dispatch.py:605`) updates only when the run id is the card's
  current run. Exit non-zero means the attempt is superseded. **Supported.**
- Cancelling a *graph-owned* worker is `os.killpg` on the process group the
  adapter created, followed by wait and reconciliation. **Supported.**
- Releasing a card claim is `hermes kanban reclaim` (`kanban_db.py:2475`). The
  graph does not call it: card lifecycle belongs to the owning worker, and
  reclaiming a live claim would be exactly the "second scheduler" this design
  forbids.
- There is no Hermes API that lets an unrelated process cancel a dispatched
  worker safely. The graph therefore only cancels subprocesses it owns and
  reports `cancellation_pending` otherwise.

## 4. Dispatcher cannot execute the same work twice

`dispatch_once` (`kanban_db_dispatch.py:1980`) and the claim path only move
`ready`/`todo`/`review` cards. A card held by a live attempt is `running` and is
not re-spawned; duplicate-writer protection for the workspace is therefore
provided by Hermes plus the orchestration coordinator lock (T1).

## 5. Capabilities: supported, conditional, and out of scope

| Capability | Verdict | Evidence |
| --- | --- | --- |
| Read-only review of an independent reviewer's **tool effects** | **SUPPORTED with the Docker terminal backend** | `hermes chat` exposes `file` (`write_file`, `patch`), `terminal` and `execute_code`; there is no read-only toolset (`toolsets.py:118`). R2-A therefore runs the reviewer in a disposable, digest-pinned container with `:ro` candidate/evidence mounts, `--network=none`, non-root, read-only rootfs and no forwarded env, and refuses to review unless an executed attestation measures that surface. See `reviewer-sandbox.md` and `evidence/reviewer-sandbox/attestation.json`. |
| Whole-agent containment | **NOT AVAILABLE, NOT CLAIMED** | The host launcher, its profile home and session DB run as the host user; `/evidence` content reaches the configured provider; `execve` inside the container is not denied (Hermes file tools are `docker exec`); the image and Docker daemon are trusted. |
| Full installed acceptance (`factory run`) | **SUPPORTED on an authorized disposable project** | `evidence/installed-acceptance.json` and `evidence/hermes-graph-run/`: real `bin/factory run` + `bin/factory verify` (exit 0, reason `ok`) against a busybox-only disposable repo with no host ports or mounts. |
| Graph admission of an already-claimed card from a *separate* process | **NOT SUPPORTED** | Claims are admitted through the profile owner's gateway (`kanban_worker_client`); the graph therefore executes **inside** the claiming worker and resumes only inside a current attempt. |

The live reviewer remains refusal-by-default: `HermesReviewer.submit` raises
`capability_unenforced` unless an executed attestor proves the sandbox, and the
CLI only constructs `SandboxedReadonlyAttestor` when the operator passes the
explicit `--reviewer-sandbox` flag. No prompt-only "read-only" claim is made.
T10 is therefore satisfied **for the reviewer's tool effects**, not as
whole-agent containment.

## 6. Consequences for this implementation

1. The graph runs in the claimed worker process; there is no LangGraph daemon
   and no admission loop.
2. Resume requires a current Hermes attempt (`board.view` membership plus the
   run-scoped heartbeat); a superseded attempt cannot act.
3. Worker execution is a foreground, owned `hermes -p ... chat` subprocess with
   a scrubbed environment, bounded by timeout and cancellable by process group.
4. Worker-reported tests are diagnostic only; the trusted gate is the installed
   launcher / engine verifier.
