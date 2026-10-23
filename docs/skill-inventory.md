# Skill inventory and provenance

The public integration uses a curated rewrite of factory guidance from the default
Hermes installation. Runtime identity is not bundled.
Source paths below are relative to that installation's `skills/` directory.

| Public skill | Local source | Treatment |
| --- | --- | --- |
| factory-spec-intake | workers/factory-spec-intake | Generalized operator and card workflow |
| software-factory-ops | devops/software-factory-ops | Current engine behavior; removed host anecdotes |
| factory-qc-gating | devops/factory-qc-gating | Evidence and promotion rules without host routing |
| worker-software-engineer | workers/worker-software-engineer | Bounded worker scope and evidence reporting |
| hermes-kanban-dispatch | autonomous-ai-agents/hermes-kanban-dispatch | Lifecycle boundaries; omitted database repair and marker bypass recipes |
| integration-test-verification | software-development/integration-test-verification | Shipped-entry-point verification |
| one-verifiable-change | software-development/one-verifiable-change | Bounded increments |
| agent-skill-engine-boundary | software-development/agent-skill-engine-boundary | Engine enforcement versus agent guidance |

These eight skills live under `integrations/hermes/skills`. They intentionally do
not copy the local source folders verbatim or include runtime backups and incident
records. The local Hermes installation has not been modified.

Historical operational skills, incident notes, archived workflows, and general
third-party skill libraries are excluded from this snapshot. Generic code review,
TDD, debugging, and multi-reviewer skills may be installed separately according to
their upstream instructions and licenses. See [third-party notices](../THIRD_PARTY_NOTICES.md).
