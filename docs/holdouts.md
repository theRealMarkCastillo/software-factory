# Holdout acceptance suites

`factory run --holdout DIR` runs an operator-owned acceptance suite **after** the
visible Compose test succeeds and its project has been torn down. It is refused
with `--deployment`, `--no-gates`, or `--mode diagnostic`, before gates or Docker.

## Inputs and isolation

DIR must be an absolute existing directory outside the candidate repository.
Neither its ancestors nor any entry beneath it may be a symlink. Repository,
holdout and public artifacts must not overlap. Use the actual canonical path on
systems with symlinked temporary-directory aliases. DIR contains `holdout.json`:

```json
{
  "version": 1,
  "service": "test",
  "command": ["python", "-B", "/factory-holdout/check.py"],
  "files": ["check.py"]
}
```

`service` must exist in the validated Compose inputs. `command` is a nonempty
argv array, not a shell command (invoke a shell explicitly if required). Compose
interpolation is escaped so literal dollar signs survive. `files` is a nonempty,
unique set of regular relative files, with no traversal. Every listed file must
exist; no unlisted regular files are allowed, except the manifest itself and
engine output in the reserved `runs/` subtree. Do not list `holdout.json` or
anything under `runs/`. Symlinks and special files are refused even under runs.
Use `-B`/`PYTHONDONTWRITEBYTECODE` for Python because the mount is read-only.

The engine generates a second unique `factory-*` Compose project using the same
candidate and Compose file set plus a private override. Only the holdout service
gets the read-only DIR mount at `/factory-holdout` and the replacement command.
It starts that service and its dependencies. Project-scoped volumes and networks
are fresh, and teardown removes that project's volumes in `finally`. Writable
host bind mounts are refused for holdout runs because they would share stores
between projects. Other ordinary default-deny Compose rules remain in force;
there is no general exemption for external mounts, explicit volume names,
external stores, privileged containers, or published host ports. Holdout
pre/post container snapshots and the encompassing run snapshots must remain
clean. Failure, timeout, teardown failure, changed inputs, or snapshot failure
fails closed at `holdout_failed` (an encompassing safety incident can take
precedence).

## Evidence and confidentiality

Public `evidence-manifest.json` records `holdout_required: true` and `holdout`:

- `manifest_sha256` and `files` (relative filename to SHA-256 mapping);
- `exit_code`, `passed` and `failed` counts (null when not parseable);
- `duration_s`, `project_name`, `teardown_ok`, `safety_clean`.

Only numbers are extracted from pytest-style summary lines or JUnit XML emitted
on stdout. JUnit files inside the container are not automatically collected.
Counts are informational; nonzero exits and parsed nonzero failure counts both
prevent certification. No test names, assertion messages, command argv, source
text, or holdout path are included in this block. Filenames and hashes **are**
visible; do not put secrets in filenames.

The override, raw stdout/stderr, Compose logs, exception diagnostics and teardown
logs live **only** in `DIR/runs/<run-id>/`, not the public artifacts directory or
candidate repository. The run ID is the visible project's generated name. Keep
DIR operator-owned and restrict filesystem access. The engine does not upload,
copy, or retain these private logs elsewhere.

```sh
factory verify --repo "$CANDIDATE_REPO" --candidate "$CANDIDATE" --base "$BASE" \
  --artifacts-dir "$ARTIFACTS" --holdout "$HOLDOUT" --json
```

With `--holdout`, verification revalidates the directory and compares the entire
manifest and per-file digest set. Any mismatch rejects certification as
`holdout_digest_mismatch`. It does not rerun the tests. Without DIR it reports
`recorded_not_reverified`: recorded successful holdout evidence may certify,
but no claim is made that the operator's current suite still matches. A declared
holdout missing its block or carrying failure cannot certify, including through
promotion's shared checker. Older evidence without a holdout declaration retains
its existing meaning; supplying DIR cannot retroactively add holdout evidence.

## Demo and the overfit case

See [holdout-minimal](../examples/holdout-minimal/README.md). Its in-tree holdout
is **an example**, not a confidential suite. Copy it outside the candidate before
running. The app doubles integers; the visible smoke test checks only `n=1`.
Changing the app to always return `2` still passes the visible suite but fails
the external suite, producing `holdout_failed`.

## Threat model and limits

This protects against overfitting to visible tests and accidental reads through
the repository or public artifacts. It is **not** a sandbox against a determined
agent with filesystem access. Same-UID actors can read DIR directly, race path
checks, modify files during a run then restore them, or forge self-consistent
evidence and sidecars. A hostile candidate image or entrypoint can inspect the
mounted suite, exfiltrate it over available networks, or deliberately print
misleading summaries; this feature does not attest trustworthy test execution.
Builds, ignored files, mutable image tags and external dependencies retain the
existing evidence trust limits. Snapshot comparison detects non-factory changes,
not arbitrary host effects. Operator-controlled suite authors are trusted.
FACTORY-HOLDOUT-02 adds a builder-profile read guard; it is not implemented here.
Do not expose DIR or its private logs to builders, and use stronger OS isolation
when adversarial confidentiality is required.

Design reference: the MIT-licensed `factory/holdout.py` in
[eidolonlabs-ai/hermes-factory](https://github.com/eidolonlabs-ai/hermes-factory).
This engine implements external read-only mounts rather than staging secrets
inside a candidate worktree.
