# Architecture and supported boundaries

The factory has an agent-independent execution engine and a Hermes orchestration
layer. The stable integration point is the Python CLI and JSON result, not a
particular agent identity.

## Execution engine

`factory.runner.run_factory_card` validates every supplied Compose document,
runs the preflight gates, generates a `factory-*` project name, snapshots container
state, invokes Compose through `factory.policy`, collects logs, attempts teardown,
checks postflight state, and writes and validates evidence.

The policy checks are a conservative supported subset of Compose. The loader does
not implement the complete Compose merge specification; it validates each input
before merging mappings and validates the merged model again. A file with an
unsafe declaration is rejected even if a later override would remove it.

## Compose bind-mount policy

Absolute bind sources may not point at sensitive host roots (`/etc`, `/var`,
`/home`, `/Users`, `/root`, `/proc`, `/sys`, `/dev`), checked both as written and
after symlink resolution. This includes the operator's own home directory, the
Docker socket, and system paths.

Relative sources are resolved against the subject repository. They are allowed
inside that repository even when the checkout itself lives under `/Users` or
`/home`, so a normal repository-local data or fixture mount keeps working. They
are rejected when resolution leaves the repository (traversal or symlink escape)
or when any path component is a credential or metadata subtree: `.git`, `.ssh`,
`.aws`, `.gnupg`, `.docker`, `.kube`, `.netrc`, `.npmrc`, `.pypirc`, `.env`, or
`secrets`. The absolute spelling of a repository path remains rejected; use the
relative form. Named volumes skip the host-path check because Compose resolves
them internally.

Coverage compares the selected Git base with the actual working tree and includes
untracked Python files. Audit availability and successful execution are separate
facts. The data-model gate validates declarations and referenced tests; it does not
prove their behavior independently of the test run.

## Python environments

uv manages the development environment and committed dependency lockfile. The
engine has no uv runtime dependency and can be installed with pip. Subject
repositories keep their own environment tooling. Audits inspect target
site-packages directly, including environments created without pip. The factory's
development environment includes pip transitively through its audit tooling.

## Hermes adapter

Hermes owns task scheduling and worker lifecycle. Skills tell the orchestrator and
workers how to use the engine and interpret evidence. The orchestrator profile name is installation-specific, not an engine dependency.

Other agents can call the same CLI and parse its JSON. A future adapter could map
another task queue onto card IDs and result artifacts without rewriting the gates.
The first release does not promise cross-agent scheduling compatibility.

## Current portability limits

- Artifact storage defaults to `~/.hermes/kanban/factory-artifacts`; override with
  `--artifacts-root`.
- Audit binary discovery prefers the environment of the interpreter running the
  engine, then falls back to PATH and a historical Hermes venv. An explicit
  `FACTORY_PIP_AUDIT` override always wins.
- `HERMES_KANBAN_TASK` is optional provenance in the manifest.
- The runner uses `main` as the coverage base.
- Protected projects and reserved ports can be extended through an absolute
  `FACTORY_HOST_CONFIG` JSON path. Invalid configuration fails closed. Sensitive
  roots and the other built-in safety restrictions cannot be disabled by it.
- Unix host tools are used by port helpers. Windows support has not been established.
- `.env.example` is required for assembled evidence.
- `factory.contract.yml` is not automatically loaded by the runner. The standalone
  isolation assertion is available, but writing a contract file alone does not
  activate enforcement.

## Trust boundary

The host runs repository tests and audit tooling. Give workers dedicated workspaces
and only the credentials and permissions their task requires. Do not treat the
factory as a safe execution service for arbitrary untrusted repositories.

Container snapshots compare selected before/after state; they cannot prove that
no transient restart or data mutation occurred. Teardown is best-effort under
process failure. The policy does not replace daemon access controls, OS isolation,
credential boundaries, or independent acceptance review.
