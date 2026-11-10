# Owner handoff: why a dispatched worker cannot run the installed CLI

Status: **diagnosed 2026-11-09; rollout HOLD until a sanctioned owner handoff
exists.** This document records the measured cause of the pilot blocker and the
exact interface the rollout needs. Nothing here was activated, merged or
installed.

## Summary

The orchestration design says the graph runs "in the process that already owns
the Hermes attempt" (`operator-runbook.md`). On this host that premise is false
for the installed CLI: Hermes grants the attempt to the **managed worker
process**, and deliberately strips the same grant from **every tool subprocess**
that worker spawns. `factory orchestrate` is always such a subprocess, so it can
never hold the grant from a dispatched card.

This is not a transient bug and not something the engine can repair by itself.
It is the documented grant boundary:

> "Only the dispatcher's own spawn grants worker scope; do not copy a worker's
> environment into a hand-launched process."
> — `tools/kanban_tools.py:598`

## The chain, in code (Hermes `a9ff9799e27`)

1. **The dispatcher grants the attempt to the worker client's environment.**
   `hermes_cli/kanban_db_dispatch.py:2754` (`_default_spawn`) sets
   `HERMES_KANBAN_TASK` (2811), `HERMES_KANBAN_RUN_ID` (2832) and
   `HERMES_KANBAN_CLAIM_LOCK` (2834) for the spawned worker.
2. **The worker client forwards the grant to the gateway and scrubs its own
   environment.** `hermes_cli/kanban_worker_client.py:175-187` reads the three
   values into the `kanban.run` RPC params, then removes every `HERMES_KANBAN_*`
   key before the owner starts (`:181-186`, "Owner startup must never inherit
   this worker's task/tool/goal identity").
3. **The owner binds the grant in the worker's agent process.**
   `gateway/session_kanban.py:155-202` (`bind_worker_context`) sets
   `HERMES_KANBAN_TASK/BOARD/DB/RUN_ID/CLAIM_LOCK/WORKSPACE/...` and applies them
   with `os.environ.update(env)` at `:201`. The *agent* holds the grant; its
   children do not.
4. **Every tool child is fenced.** `tools/environments/local.py:308-318`
   (`_finalize_child_env`, and the second spawn surface at `:425-426`) ends with
   `agent.delegation_context.delegated_child_subprocess_env`
   (`agent/delegation_context.py:176-187`), which calls `scrub_kanban_env`
   (`:122-141`) because the parent holds `HERMES_KANBAN_TASK`. That function
   deletes `KANBAN_ENV_KEYS` (`:18-21`: task, run id, claim lock, goal markers)
   and stamps `HERMES_DELEGATED_CHILD_CONTEXT`. `execute_code` applies the same
   rule and explicitly prevents an `env_passthrough` from re-granting it
   (`tools/code_execution_env.py:106-119`).
5. **The CLI fence follows the environment fence.** A process carrying the
   marker cannot mutate cards through the CLI:
   `hermes_cli/kanban.py:154` and `_is_delegated_child_cli_mutation` (`:223`),
   which is why the pilot worker cannot unblock or edit its own card.

Consequences:

- the installed CLI, run through `terminal`/`execute_code`, reports
  `owner_grant_absent` before any effect (`factory_orchestration/adapters/hermes.py`,
  `OwnerGrant.from_env` / `HermesBoard.view`);
- passing `--attempt-run-id`/`--claim-lock` cannot substitute: flags populate the
  attempt, but the ownership gate reads the process environment;
- a delegated child is in the same position by design.

## There is no supported way to materialize the grant elsewhere

- `hermes kanban show --json` exposes neither `claim_lock` nor
  `current_run_id`; `hermes kanban claim` prints only the task id and workspace.
  So an operator process cannot obtain the fencing token through a supported
  command (measured below).
- The dispatcher's `spawn_fn` is a parameter to `dispatch_once`
  (`hermes_cli/kanban_db_dispatch.py:1980-2006`), but the gateway call site
  (`gateway/kanban_watchers_dispatcher.py:196`) does not expose it to plugins.
  Hermes' own documentation lists "adding an external CLI worker lane" as
  design work that does not exist yet.
- Reconstructing the token from the board database, copying a worker's
  environment into a hand-launched process, or making the graph claim the card
  are all rejected: the first is unpublished state, the second is exactly what
  the fence forbids, and the third would make the graph a second scheduler.

## Measured on this host (2026-11-09)

Inside a dispatcher-owned worker (this rollout card), through its `terminal`
tool:

```sh
$ python3 scripts/owner_context_probe.py --card t_c52a3ef6 --board software-factory
{
  "fence_marker": "~/.hermes",
  "grant_present": {
    "HERMES_KANBAN_CLAIM_LOCK": false,
    "HERMES_KANBAN_RUN_ID": false,
    "HERMES_KANBAN_TASK": false
  },
  "kanban_show": {
    "exposes_claim_lock": false,
    "exposes_current_run_id": false,
    "ok": true,
    "run_ids": [367, 368, 369],
    "status": "todo"
  },
  "location": {
    "HERMES_HOME": "~/.hermes/profiles/factory-runner",
    "HERMES_KANBAN_BOARD": "software-factory",
    "HERMES_KANBAN_DB": ".../kanban/boards/software-factory/kanban.db",
    "HERMES_KANBAN_WORKSPACE": "~/software-factory-ops",
    "HERMES_KANBAN_WORKSPACES_ROOT": ".../kanban/boards/software-factory/workspaces",
    "HERMES_PROFILE": "factory-runner"
  },
  "verdict": "fenced_descendant"
}
```

The same probe under the **staged release interpreter** proves the engine agrees:

```sh
$ <staged>/.runtime/current/engine/.venv/bin/python \
    scripts/owner_context_probe.py --engine-check --require-grant
{"engine": {"available": true, "grant": null}, "verdict": "fenced_descendant", ...}
$ echo $?
2
```

The agent process itself does hold the grant — the in-process `kanban_*` tools
resolve `HERMES_KANBAN_TASK` with no argument, which is only possible from that
environment — while the fence marker appears only in its children. That is the
whole defect: the identity exists one process above the CLI.

The staged release is unaffected by the wrong paths in the pilot card body:

```sh
<staged>/.runtime/current/engine/scripts/langgraph_rollout_pilot.py  # exists
<staged>/.runtime/current/engine/.venv/bin/python                    # CPython 3.12.11
<staged>/engine/...                                                  # does not exist
```

## The exact missing interface

One of the following must be approved and implemented in Hermes. The engine
already consumes the grant contract; neither option changes admission, gates or
the pilot harness.

### H1 — dispatcher-sanctioned runner lane (recommended)

The dispatcher claims the card and spawns a declared command *as the attempt's
worker*, with the same grant environment, log, heartbeat and dead-worker sweep
it gives a managed worker. The card's profile opts in with a fixed argv list (no
shell); every other card keeps the managed-worker lane.

- Shape: `kanban.runner: {"<profile>": ["/abs/runner", "--flag"]}` in
  `config.yaml`, read by `_default_spawn`; the runner is expected to heartbeat
  with the injected `HERMES_KANBAN_RUN_ID` and to terminate the card through the
  dispatcher-owned lifecycle.
- Why it is safe: the grant stays at the dispatcher→worker boundary; the fence is
  untouched; the engine's adapters already scrub ownership variables from the
  worker/reviewer children they spawn (`assert_child_contract`,
  `OWNERSHIP_ENV`).
- Touches: `hermes_cli/kanban_db_dispatch.py` (spawn branch),
  config defaults/schema, docs, tests. Roughly 150-300 lines with tests.
- Verification: `owner_context_probe.py --require-grant` reports
  `grant_in_env` under the runner, and the existing pilot harness then runs
  unchanged.

### H2 — opt-in owner-grant file (smallest)

The gateway writes `{task_id, run_id, claim_lock, profile, session_id, pid}` to a
per-attempt file (`0600`, profile state dir) and exports
`HERMES_KANBAN_OWNER_GRANT_FILE` next to the grant in
`gateway/session_kanban.py` (`bind_worker_context`). Config
`kanban.owner_grant_file: true` enables it per profile, default `false`.

- The engine gains one source in `OwnerGrant.from_env`: if the variable names a
  `0600` file owned by the invoking uid, read the grant from it; the existing
  `view()` still proves the row is the live current run before any effect.
- Why it is a real weakening: a tool child of that worker can read the file, so
  the fence no longer stops a descendant from acting as the attempt owner. That
  is why it must be an explicit per-profile opt-in, the file is removed at owner
  exit, and the engine never prints the token.
- Touches: `gateway/session_kanban.py`, config defaults, engine
  `adapters/hermes.py`, tests/docs. Roughly 60-120 lines with tests.

### H3 — operator-owned orchestration (no Hermes change today) — not viable

Needs a supported way for a non-worker process to hold a claim's fencing token.
`claim` does not print it and `show --json` hides it, so this would still require
a Hermes CLI change — and it moves ownership away from the dispatched worker,
which the T1/T4 design forbids.

## Decision needed

Approve H1 or H2 (or specify another sanctioned handoff). After the Hermes-side
change lands, the rollout owner will:

1. run `scripts/owner_context_probe.py --engine-check --require-grant` in the
   sanctioned owner context and record `grant_in_env`;
2. correct the pilot card body to the `.runtime/current/engine` paths and the
   owner-context precondition;
3. release the same pilot card (no duplicate) for attempt 2.

Until then, a dispatched worker cannot run the graph, and re-dispatching the
pilot card with unchanged instructions only reproduces
`owner_grant_absent` — it is not an implementation failure to repair by retry.
