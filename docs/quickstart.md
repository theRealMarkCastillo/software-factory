# Standalone quickstart

Run these commands from the factory checkout after running `uv sync --locked` as
shown in the README. The engine can be invoked by a shell, Hermes, or another agent.

## Verify installation without Docker

```sh
uv run --locked factory-run --help
uv run --locked factory-coverage --help
uv run --locked python -m pytest -q
```

## Run the minimal Compose smoke test

Requires a reachable Docker daemon and Compose v2, an existing `main` Git ref
(or an explicit `--base <ref>`),
and a committed checkout containing `examples/minimal`. The example has no Python
source delta or dependency manifest, so those gates record inapplicability.
It tests the runner lifecycle, not a business application.

```sh
uv run --locked factory-run \
  --card smoke \
  --repo examples/minimal \
  --compose compose.yml \
  --test-service test \
  --artifacts-root ./artifacts
```

Inspect the JSON result and the directory it names. A successful acceptance run
has `passed: true` and `stage: complete`. Logs, `evidence-manifest.json`, and any
failure marker remain in that directory. A missing Docker daemon is an environment
failure, not evidence that the workload passed.
Pre-Docker audit, coverage, and data-model gate refusals leave `result.json` in
the returned `artifacts_dir`, but no certifiable evidence manifest.

## Use your own project

### Validate a real development deployment (opt-in)

The default still rejects all published host ports. Use `--deployment --check`
to validate a project's real dev Compose file rather than a special test service:

```sh
uv run --locked factory-run --deployment \
  --card deployment-smoke --repo . --base HEAD \
  --compose examples/deployment-minimal/compose.yml \
  --check examples/deployment-minimal/check.sh \
  --env-example examples/deployment-minimal/env.example \
  --wait-timeout 300 --timeout 300 --artifacts-root ./artifacts
```

Run from a committed checkout. This example's build context is relative to the
checkout root. For your own project, use its repository root and real Compose
file. Supply your intended diff base instead of `HEAD` when measuring changes.
`--deployment` requires an executable repo-relative `--check`, forbids an explicit
`--test-service`, and cannot disable gates. The runner writes and validates
`compose.resolved.yml`, uses the repository as Compose's project directory, and
runs `up -d --build --wait --wait-timeout S` (default 300 seconds). Profile-gated
services remain inactive. Every original source Compose file must explicitly bind
published ports to loopback: `127.0.0.1` or `[::1]` in short syntax (`::1` as
long-syntax `host_ip`). Only single static TCP mappings are supported: no ranges,
UDP or host mode. Unsafe source exposure is refused before Docker, with stage
`exposure_refused`; no flag, environment override, allowlist or waiver can bypass
this requirement, and an override cannot legalize an unsafe source mapping.
After source validation, the requested host port is reallocated: each mapping
receives a unique, available factory-range port bound only to `127.0.0.1` (also
for IPv6 loopback sources). The allocator lock is held until teardown. All other
safety rules are unchanged.

The check runs **on the host**, with repository trust, just like host coverage;
this is not a sandbox for untrusted repository scripts. Its cwd is the repository,
its environment contains only available `PATH`, `HOME`, `TMPDIR`, `LANG`, plus
`FACTORY_HOST=127.0.0.1` and `FACTORY_PORT_<SERVICE>_<CONTAINERPORT>` (uppercase
service name, non-alphanumerics replaced by `_`). The script's exit code is
authoritative and `--timeout` bounds it. Use the allocated variables, not requested
ports. Output goes to `check.stdout.log` and `check.stderr.log`. Wait failure,
check failure, check timeout and teardown failure have distinct deployment stages.

Deployment evidence can be certified with `factory-verify`, but is **not
promotable**: acceptance runs remain the promotion input. To exercise the
example's negative control, substitute `check-fail.sh` for `check.sh`.

Provide a Git working tree, a Compose test service, and an `.env.example` describing
the environment. Use internal service names instead of published host ports.
The environment example is required in every mode: a missing file refuses before
gates or Docker with `env_example_missing`; add it or pass `--env-example <path>`.
Python projects need pytest and pytest-cov plus project dependencies in the
interpreter used to launch the runner (or `FACTORY_COVERAGE_INTERPRETER`).
The subject project can use any environment manager. Python environment audits
read installed distributions from `lib/python*/site-packages`; the target
environment does not need pip. The factory environment provides `pip-audit`.

The audit gate is fail-closed: it passes only on a readable, internally
consistent pip-audit report with at least one audited dependency and no
unexpected skipped dependency. Tool errors, empty or malformed output, timeouts
and exit-code/report contradictions block the run. Lock files (`uv.lock`,
`poetry.lock`, `Pipfile.lock`) are converted to a fully pinned requirement set
and audited without re-resolution; a `pyproject.toml` without a lock, or an
unpinned `requirements.txt`, fails closed with an actionable reason until you
commit a deterministic input. When an environment is audited beside a
`uv.lock`, installed versions must match the lock (`uv sync --frozen`).

For every Node project the gate runs `npm audit --json --audit-level moderate
--omit=dev` (npm 7+ with audit report v2 required), recording `npm_scope:
production` and the exact argv in evidence; unsupported reports fail closed
with an upgrade instruction. Dev/build tooling is not vulnerability-gated:
deployed images must not execute dev-only packages (for example, a Vite dev
server must not serve production). Reviewers must check that deployment matches
this scope. Python auditing is unchanged.

```sh
uv run --locked factory-run \
  --card task-123 \
  --repo /path/to/project \
  --compose compose.yml \
  --compose compose.test.yml \
  --test-service test \
  --artifacts-root ./artifacts
```

Compose paths are repo-relative by default; absolute paths are also accepted.
The runner accepts `--base <ref>` (default `main`). Accepted spellings are a full
40-hex commit ID, literal `HEAD`, an exact fully qualified `refs/...` name, or a
simple name checked against `refs/heads/`, `refs/tags/`, and `refs/remotes/`.
Exactly one candidate must exist; collisions are refused as `ambiguous_base`
even when they point at the same commit. Fully qualified refs disambiguate.
Annotated tags are peeled to commits. Abbreviated hex, revision expressions and
reflog syntax are refused as `unsupported_base`; missing commits report
`base_not_found`. Explicit invalid bases stop before gates, snapshots or Compose.

Resolution happens once; coverage and evidence use the captured commit. Use that
same full SHA with `factory-verify --base <sha>`. Omitting `--base` (API
`base_ref=None`) retains legacy `main` behavior: a missing `main` permits the
lifecycle and diagnostic artifacts, but unresolved evidence is rejected and can
never certify. An ambiguous omitted `main` is refused before execution; explicit
`--base main` with no `main` is also refused. A data-model gate runs when
`data-model.yml` exists.
With a subdirectory `--repo`, source cleanliness is scoped to that directory,
while the candidate revision remains the enclosing repository's HEAD.

`--env-file` selects the file hashed in evidence; it does **not** pass a
Compose `--env-file` option. Use Compose's actual environment configuration and
ensure the recorded file is the one used. The manifest is not a complete capture
of inherited environment variables or image digests. Absolute files outside the
repository are accepted: evidence records only `location: external` and the
file's SHA-256, never its path or values. Repository-local files retain their
repo-relative path. Missing optional files have no content hash.

The port allocator stores its lock under `$FACTORY_STATE_DIR/ports/.lock` when
set, otherwise `$XDG_STATE_HOME/software-factory/ports/.lock`, otherwise
`~/.local/state/software-factory/ports/.lock`. It creates the state directory
with mode 0700; the installed package does not need to be writable.

`--no-gates` is diagnostic only and cannot produce an acceptance pass. Service-
dependent Python tests may fail the host coverage gate before Compose starts;
container-produced coverage is not yet integrated. Do not hide that gap with broad
coverage exclusions.
