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
