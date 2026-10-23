# Hermes integration

This is the first-class orchestration path. The engine remains usable without
Hermes. Use your own orchestrator identity. No personal profile configuration is required.

## Install the curated skill bundle

Install the factory Python environment first. From the checkout:

```sh
python3 scripts/install_hermes_skills.py --destination ~/.hermes/skills
```

This prints a dry-run plan. To copy the eight curated skills:

```sh
python3 scripts/install_hermes_skills.py --destination ~/.hermes/skills --apply
```

The installer refuses to overwrite existing skill directories. Compare existing
copies and choose a new destination or reconcile deliberately. It never copies
credentials, profile configuration, memories, chats, databases, or backups.
Only the curated bundle is included. See [the inventory](skill-inventory.md).

## Configure your installation

Use the commands supported by your installed Hermes version to create a dedicated
board and worker profiles. Suggested roles are `factory-runner` for implementation,
`factory-qa` for evidence verification, and `factory-architect` for design review.
These names do not create profiles or grant permissions automatically.

Give workers dedicated workspaces and minimal tool access. Configure the factory
Python interpreter and artifact directory explicitly. Verify your Hermes version
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
