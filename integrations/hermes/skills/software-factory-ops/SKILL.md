---
name: software-factory-ops
description: Run a factory Compose acceptance job and inspect its evidence without bypassing policy.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# Software Factory Ops

## Standalone installation

The direct commands in this section are instructions for a standalone
installation of the public engine. Where an operator provides a private launcher
(for example a `factory` command or a deployment-specific wrapper), that
launcher's guidance supersedes them; follow it and do not substitute these
commands.

From the factory checkout, use `uv run --locked factory-run --help`. An
installation managed without uv can use `python -m factory.runner --help`
from its factory environment. Supply the task ID, subject repository, Compose files,
test service, and a writable artifact root.

The runner validates every input, runs host gates, snapshots containers, executes
a unique factory project, collects logs, attempts teardown, and checks evidence.
Read the actual JSON result, logs, and manifest. Zero test exit alone is not a pass.

Keep Compose paths static and resources project-scoped. Host ports, Docker socket
mounts, privileged mode, external resources, and unsafe overrides are rejected.
Do not work around these checks with raw Docker commands. Do not prune host-wide
images, volumes, networks, or build caches. Cleanup belongs to the policy module.

Coverage runs on the host before Compose; service-dependent coverage may remain
unavailable. Disabled gates and unavailable audits produce diagnostic evidence,
not an acceptance pass. Record limitations explicitly.

A protected-state change requires operator review. Do not auto-retry or attempt
to repair unrelated workloads. A failed snapshot makes safety verification
INCONCLUSIVE. Consult the repository quickstart for current CLI limitations.
