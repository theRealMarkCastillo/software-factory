---
name: hermes-kanban-dispatch
description: Dispatch factory work through the owning Hermes board and respect worker lifecycle permissions.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# Hermes Kanban Dispatch

Identify the installation, explicit board, workspace, and registered worker
profile before dispatch. Similar profile names on another host do not establish
ownership. Use the installed `hermes kanban --help` and profile tooling; do not
assume flags from another version or a private runtime extension exist.

Use one active writer per shared repository. Parallel writers require explicitly
isolated worktrees and disjoint scopes. Encode dependency edges with supported
Kanban operations and verify them before releasing downstream work.

A top-level card worker owns its card lifecycle. Delegated helpers return results
to the parent instead of mutating board state. Never clear delegation markers,
edit a Kanban database, or change runtime permissions to bypass this boundary.

For notification routing, orchestrator-created cards default to wake-only subscriptions.
Worker events wake the orchestrator; only owner decisions and chain completion
are delivered to the human. Keep routine progress, intermediate QA, and repair
results in card evidence rather than human status notifications. Inspect supported
subscription tooling and read back the effective routing after creation; do not
invent flags, silently enable per-card human delivery, or edit runtime databases
when an installation lacks this capability. Surface that limitation to the owner.

When a task appears stuck, inspect its current state and owning worker through
supported read-only interfaces. A timeout is not proof the worker stopped. Do not
launch duplicate writers or retry completed work. Surface inconsistent lifecycle
state to the operator rather than repairing internal database rows from a skill.
