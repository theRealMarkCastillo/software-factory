---
name: agent-skill-engine-boundary
description: Decide whether a factory guarantee belongs in tested engine code or agent guidance.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# Agent Skill Engine Boundary

Enforce deterministic safety, isolation, gate, and evidence rules in code with
regression tests. Skills explain how to invoke that code and interpret results;
they do not replace enforcement with instructions that an agent may overlook.

Use the public CLI or API. If a required operation is missing, propose a bounded
engine change instead of importing private internals, editing runtime databases,
or bypassing checks from a skill. Keep constants authoritative in code and link
to them from prose. State what the engine does not enforce.
