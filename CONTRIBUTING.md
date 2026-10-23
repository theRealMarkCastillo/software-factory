# Contributing

Use uv with the committed lockfile. Python 3.12 is the local default; CI checks
Python 3.11, 3.12, and 3.13:

```sh
uv sync --locked
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
git diff --check
```

For a targeted dependency update, run `uv lock --upgrade-package <package>`,
then `uv sync --locked` and the checks above. Commit `uv.lock` and any accompanying
`pyproject.toml` changes together. CI uses uv 0.11.6 and rejects a stale lockfile.
The README also documents installation with pip.

Keep behavior changes narrow. For safety or evidence changes, first add a failing
regression at the public runner boundary, then implement the fix. Mock Docker for
unit tests; never use protected workloads as test fixtures.

Two existing host-inspection checks require explicit opt-in:

```sh
uv run --locked python -m pytest -q --run-live
```

They inspect Docker/host port state and may create a local allocator lock. They
do not establish end-to-end Compose acceptance. Run only on an appropriate host.
Report unavailable services as INCONCLUSIVE, not PASS.

Use conventional commit messages. Include the problem, resulting behavior, exact
validation, and limitations in proposed changes. Do not include credentials,
local agent configuration, conversations, or runtime databases. Changes to skills
should preserve the CLI boundary and must not authorize broader execution than
the operator's task.

Contributions are offered under the repository MIT license. Preserve upstream
notices for adapted material. See the public-release checklist for release evidence and remaining acceptance gaps.
