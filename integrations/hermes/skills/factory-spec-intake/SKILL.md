---
name: factory-spec-intake
description: Turn an operator request into a bounded, testable software-factory card.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# Factory Spec Intake

Clarify the desired outcome, affected repository and deployment tier, scope,
non-goals, and falsifiable acceptance checks. Reuse answers already supplied by
the operator. Ask only questions that materially affect implementation.

Record the workspace, branch/worktree, permitted files, dependencies, validation
commands, artifact location, and who may approve promotion. Separate code completion
from merge, push, deployment, and production acceptance. Do not infer authorization
for those actions from authorization to implement.

Before creating a card, check for an existing card for the same outcome. Use the
explicit installation board on every Kanban command. Link scheduling dependencies
through the board's supported dependency operation and verify with a fresh show;
prose alone is not an edge. Check the installed CLI help for supported commands.
