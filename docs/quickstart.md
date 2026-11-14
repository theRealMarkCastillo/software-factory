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

## Use your own project

Provide a Git working tree, a Compose test service, and an `.env.example` describing
the environment. Use internal service names instead of published host ports.
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
The runner accepts `--base <ref>` (default `main`). It resolves that ref once to a
full commit before any gate, uses that commit for coverage and evidence, and
refuses unknown refs before Docker. Use that same full SHA with
`factory-verify --base <sha>`. A data-model gate runs when `data-model.yml` exists.
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
