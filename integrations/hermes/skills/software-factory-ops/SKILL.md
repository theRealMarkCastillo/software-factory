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

Keep Compose paths static and resources project-scoped. By default host ports, Docker socket
mounts, privileged mode, external resources, and unsafe overrides are rejected.
Do not work around these checks with raw Docker commands. Do not prune host-wide
images, volumes, networks, or build caches. Cleanup belongs to the policy module.

Coverage runs on the host before Compose; service-dependent coverage may remain
unavailable. Disabled gates and unavailable audits produce diagnostic evidence,
not an acceptance pass. Record limitations explicitly.

The npm gate audits production dependencies only (`--omit=dev`, npm 7+ required; evidence records `npm_scope: production` and exact argv), not dev/build tooling, so reviewers must check that deployed images never execute dev-only packages (for example a Vite dev server); Python auditing is unchanged.

A protected-state change requires operator review. Do not auto-retry or attempt
to repair unrelated workloads. A failed snapshot makes safety verification
INCONCLUSIVE. Consult the repository quickstart for current CLI limitations.

## Greenfield chains and final operator smoke

Read `references/greenfield-compose-chains.md` before planning a multi-service
build chain. Its mandatory final operator-smoke card exercises the actual
developer lifecycle on the host with real services, verifies every listener is
loopback-only and LAN-address connections are refused, captures UI evidence where
applicable, and leaves the stack in a documented state. Deployment-mode port
rewriting alone cannot verify the original developer mappings or wrapper.
Unavailable real-run paths are UNEXERCISED/INCONCLUSIVE, not acceptance passes.

## Opt-in real deployment validation

Use `factory-run --deployment --check <repo-relative executable>` to validate a
project's real dev Compose file, not a test service. Default runs still reject
ports. Deployment mappings must be single TCP ports; the runner ignores requested
bind addresses/host ports and allocates unique loopback-only factory-range ports,
holding the allocator lock through teardown. Other policy rules stay unchanged.
It runs `up -d --build --wait --wait-timeout S` (default 300); profile-gated
services are not activated. Do not combine with `--test-service` or disable gates.

The check runs on the host with repository trust, the same trust as host coverage,
cwd at the repo and a minimal environment plus `FACTORY_HOST` and
`FACTORY_PORT_<SERVICE>_<CONTAINERPORT>`. Use these allocations in your checks.
Read both check logs and the wait/check/teardown outcomes in `runtime.deployment`
and `runtime.teardown`, not merely a green service healthcheck. Deployment
evidence is separately certifiable but never promotable; acceptance runs remain
the promotion input. Never bypass policy with raw Compose lifecycle commands.
