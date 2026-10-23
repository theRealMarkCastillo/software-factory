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

Requires a reachable Docker daemon and Compose v2, an existing `main` Git ref,
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
The coverage base in the runner is currently `main`; the standalone coverage CLI
accepts `--base`. A data-model gate runs when `data-model.yml` exists.

`--env-file` currently selects the file hashed in evidence; it does **not** pass a
Compose `--env-file` option. Use Compose's actual environment configuration and
ensure the recorded file is the one used. The manifest is not a complete capture
of inherited environment variables or image digests.

`--no-gates` is diagnostic only and cannot produce an acceptance pass. Service-
dependent Python tests may fail the host coverage gate before Compose starts;
container-produced coverage is not yet integrated. Do not hide that gap with broad
coverage exclusions.
