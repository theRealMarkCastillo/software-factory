---
name: factory-qc-gating
description: Verify a factory candidate against its acceptance criteria before dependent work or promotion.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# Factory Qc Gating

Review the exact candidate revision, task scope, and intentional diff. Run the
relevant checks yourself and read the generated evidence; a worker summary or
marker file alone cannot certify completion.

Check forbidden paths and negative cases as well as success. Distinguish unit,
mocked integration, real service, deployed, and operator acceptance evidence.
Treat unavailable prerequisites as INCONCLUSIVE, not PASS.

For lifecycle scripts, label fake-docker-only verification UNEXERCISED, never PASS
for real-run criteria. Such tests prove command assembly, not the actual developer lifecycle.
Require host execution with the real engine and services, empty-store integration
tests (including reset from seeded state), every listener's bind-interface evidence,
and a refused LAN-address connection for every published port. Deployment-mode
rewritten mappings cannot prove the original developer Compose is loopback-only.
Review final operator-smoke artifacts, UI evidence where applicable, and the
stack's documented final state. Missing prerequisites remain INCONCLUSIVE; do not
release real-run acceptance on mock-only evidence.

Return actionable findings with locations, impact, and a falsifiable regression.
For blocking defects, the implementation owner repairs the candidate and reruns
the required checks; review the resulting revision before releasing dependencies.
Keep review retries bounded by the task's budget and surface unresolved blockers.

Promote only within explicit operator authorization. Completion, commit, merge,
push, and deployment are separate actions. A successful prerequisite must have
both verified evidence and a real scheduling dependency before a child is released.
