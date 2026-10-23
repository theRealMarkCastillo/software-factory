# Contributing

Use Python 3.11 or later and a project virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,audit]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
git diff --check
```

Keep behavior changes narrow. For safety or evidence changes, first add a failing
regression at the public runner boundary, then implement the fix. Mock Docker for
unit tests; never use protected workloads as test fixtures.

Two existing host-inspection checks require explicit opt-in:

```sh
.venv/bin/python -m pytest -q --run-live
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
notices for adapted material. See the public-release checklist for outstanding
provenance and history review.
