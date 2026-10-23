# Deploying a private installation of the public engine

Use this repository as the source of truth for engine code and portable skills.
Keep installation configuration and local skill supplements in an independent
private operations repository. Do not maintain another copy of the engine there.
Keep credentials and Hermes runtime databases outside Git.

Pin a full public Git commit in the operations repository. Prepare a detached
checkout of that commit and run `uv sync --locked`, the test suite, and lint.
Install engine and skills from that same checkout. Preserve local supplements
explicitly, and check installed skill names for duplicates before replacing them.
Use separate development and deployed checkouts so edits do not change a running
installation. Keep the prior deployment for rollback.

## Host policy

Select host configuration explicitly in the launcher, using an absolute path:

```sh
export FACTORY_HOST_CONFIG=/absolute/path/to/private-ops/host-policy.json
/path/to/deployed-engine/.venv/bin/factory-run --help
```

Example configuration:

```json
{
  "protected_projects": ["my-production-app"],
  "reserved_host_ports": [9000]
}
```

These lists add protection to the built-in defaults; they cannot remove defaults,
allow arbitrary project names, permit host ports in Compose, or disable sensitive
mount checks. Unknown keys, malformed values, relative config paths, and missing
files fail closed. No configuration is auto-discovered from a subject repository.
Every non-factory container change still fails postflight, even if not explicitly
listed. Explicitly protected names also take precedence over the factory prefix.
Keep the operator configuration stable for the duration of a job.

A subject repository nested below the operator's home directory is supported: its
Compose files may bind existing ordinary relative paths. A relative mount is still
rejected when it resolves to the repository root, names or contains a credential
or metadata subtree (including `.env*` variants), leaves the repository, or
contains a symlink pointing at the root, out of the repository, or into a
sensitive subtree. Absolute paths under `/Users` or `/home`, and a `repo_path`
that is the host root, a sensitive host root, or a home directory, are rejected
before Compose validation.

Configure artifact storage through `--artifacts-root` and select an existing Docker
endpoint through the launcher's environment. Do not change global Docker context
or restart services during deployment. Subject repositories keep their own Python
environments; use `FACTORY_COVERAGE_INTERPRETER` when needed.

## Promotion

1. Develop and validate generic changes in the public repository.
2. Update the full revision pin in the private operations repository.
3. Prepare and test the new release without changing the active deployment.
4. Switch the engine and installed skills together, retaining local supplements.
5. Verify the actual installed entry point and configuration before accepting work.
6. Roll back to the previous validated release if verification fails.

Tests and CLI smoke checks do not establish live Compose or agent orchestration
acceptance. Record those separately. Existing sessions may retain loaded skill
content; use a fresh session after a skill update.
