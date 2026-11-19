# Hermes integration

This is the first-class orchestration path. The engine remains usable without
Hermes. Use your own orchestrator identity. No personal profile configuration is required.

## Install the curated skill bundle

Run `uv sync --locked` to install the factory Python environment first. From the checkout:

```sh
uv run --locked python scripts/install_hermes_skills.py --destination ~/.hermes/skills
```

This prints a dry-run plan. To copy the eight curated skills:

```sh
uv run --locked python scripts/install_hermes_skills.py --destination ~/.hermes/skills --apply
```

The installer refuses to overwrite existing skill directories. Compare existing
copies and choose a new destination or reconcile deliberately. It never copies
credentials, profile configuration, memories, chats, databases, or backups.
Only the curated bundle is included. See [the inventory](skill-inventory.md).

## Configure your installation

Use the commands supported by your installed Hermes version to create a dedicated
board and worker profiles. The engine defines three worker roles in
`integrations/hermes/roles/roles.json`: `factory-runner` (implementation),
`factory-qa` (independent evidence review), and `factory-architect` (design). Each
role names a host-neutral SOUL template, the curated skills it needs, the toolsets
it must enable on `cli`, and the toolsets it must not enable on any platform
(`delegation`). Role names do not create profiles or grant permissions.

Bind your profiles to roles in a JSON file kept with your installation, not here:

```json
{
  "schema": 1,
  "profiles": {
    "factory-runner": {
      "role": "factory-runner",
      "soul_supplement": "roles/factory-runner.md",
      "model": {"provider": "example-provider", "default": "example-model"}
    }
  }
}
```

`soul_supplement` (relative to the binding file) carries host facts such as the
engine `AGENTS.md` path, board slug, and artifacts root; it is appended to the
template. `model` is optional. Then:

```sh
factory roles list
factory roles render factory-qa --supplement roles/factory-qa.md
factory roles check --binding roles-binding.json --skills-source /path/to/skills
```

`check` is read-only. It compares each bound profile's `SOUL.md`, `config.yaml`
toolsets and model, and `skills/` tree with the binding, and exits 0 (conforms),
1 (drift, with findings), or 2 (invalid manifest or binding). It reads no
credential files and prints no configuration values other than toolset names
and model identifiers. Apply a rendered SOUL to a profile deliberately, with a
backup, while no worker is running; nothing in this repository writes profiles.
Role skills are a minimum: profiles may carry other skills, but a required skill
installed twice is drift because Hermes cannot load both. From a wheel install,
pass `--roles-dir` pointing at a checkout's `integrations/hermes/roles`.

Give workers dedicated workspaces and minimal tool access. Configure the factory
Python interpreter (`/path/to/software-factory/.venv/bin/python`) and artifact directory explicitly. Verify your Hermes version
supports the desired Kanban dispatcher and lifecycle operations; this repo does
not install a dispatcher or promise compatibility with private Hermes extensions.

Use an explicit board for every command. For example, after verifying local help:

```sh
hermes kanban --board software-factory --help
```

## Roll up chain time, tokens, and cost

The optional stdlib-only integration script reads Kanban exports and worker session
records; it does not import the core engine or modify any Hermes database:

```sh
.venv/bin/python scripts/hermes_chain_metrics.py --board example-board \
  --prefix PRODUCT-A --prefix PRODUCT-B
.venv/bin/python scripts/hermes_chain_metrics.py --board example-board \
  --prefix PRODUCT-A --json --hermes-home /path/to/hermes-home
```

The default home is `$HERMES_HOME`, falling back to `~/.hermes`. Each profile database
is opened at `<home>/profiles/<profile>/state.db` using SQLite `mode=ro`. The script
runs `hermes kanban --board B list --json` and `show ID --json` for every listed
card, including unrelated cards in the `others` group. Prefixes are literal,
case-sensitive title starts; the first supplied matching prefix wins. `others` is
reserved. Completed intervals are measured in minutes: worker time is their sum,
active time is their union, and elapsed time spans the earliest start to latest
end. Missing ends or reversed intervals are excluded and counted separately;
ongoing runs are not extrapolated to the current time.

Matching uses the nearest `source='kanban'` session start in the same profile,
within `--tolerance` seconds (default 15, inclusive). Equidistant sessions, sessions
selected by multiple runs, missing databases, and absent candidates are reported
as unmatched, never guessed. JSON includes `matches` with session IDs and time
deltas, `unmatched_runs` with reasons, per-prefix `groups`, and `total` under
`schema_version: 1`. All listed runs count, including retries. A blocked run has
status or outcome `blocked`; a QA failure has a summary starting exactly `FAIL`.

Session totals use `model`, `api_call_count`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `estimated_cost_usd`,
`actual_cost_usd`, `billing_mode`, and `cost_status`. Missing/non-numeric counters
and costs are counted in `missing_values`, not estimated. Sums cover matched
sessions only, so always check matching coverage and missing values. Cache-hit
share is cache-read tokens divided by input plus cache-read tokens (null for a
zero denominator); cache-write and reasoning tokens are reported separately, not
added again to that denominator. Subscription or subscription-included billing
mode/cost status excludes both cost fields, even if stale prices exist in the
row. The billing and status breakdowns retain these rows as subscription usage;
no model pricing lookup is performed. Cost sums are recorded values, not a claim
that an included subscription was free or that missing prices were zero.

For offline use, pass `--kanban-json-dir /path/to/exports`. That directory must
contain `list.json` (an array of cards, an object with `tasks`, or an object with
`columns` mapping statuses to card arrays) and `<card-id>.json` for each listed
card. Show exports contain `task` (including `title`) and `runs`. This mode never
invokes Hermes; session databases are still read from the selected home. Tests
use generated synthetic exports and temporary SQLite databases only. Live-board
compatibility and accounting completeness must be verified by the operator
against their installed Hermes version before publishing benchmark figures.

## Walk through one task

1. The orchestrator uses `factory-spec-intake` to capture outcome, repository,
   scope, non-goals, validation commands, and promotion authority.
2. `hermes-kanban-dispatch` verifies the owning board, worker, workspace, and real
   dependency edges. Only one writer operates on a shared checkout.
3. `worker-software-engineer` implements a bounded change and its tests.
4. `software-factory-ops` invokes the Python runner and collects actual artifacts.
5. `factory-qc-gating` checks the candidate revision and evidence independently.
6. The owning orchestrator completes the card or releases dependencies using the
   installed lifecycle API. Merge, push, and deployment require their own authority.

A delegated helper returns findings to its parent. It must not bypass lifecycle
restrictions by clearing environment markers or editing the Kanban database.

## Example task card

```yaml
outcome: Reject negative quantities before persisting an order
workspace: /path/to/project
branch: fix/negative-quantity
scope: [src/orders.py, tests/test_orders.py]
non_goals: [schema changes, deployment]
acceptance:
  - Negative quantities fail through the public order API
  - No order is persisted after rejection
  - Valid quantities retain existing behavior
validation:
  - pytest -q tests/test_orders.py
  - python -m factory.runner --card task-123 --repo /path/to/project --compose compose.test.yml
promotion: Return evidence to the orchestrator; no push or deployment
```

This is a task-content example, not a claimed Hermes card schema. Adapt it to the
installed CLI and your project's actual commands.
