# Enforced read-only reviewer (R2-A): implementation and evidence

Status: implemented and exercised on this host. **T10 is no longer "blocked by
absence of a mechanism":** a live review now runs only after an executed
attestation proves a container boundary for its tool effects. The refusal
default is preserved for every other path, and the claim is deliberately narrower
than "the agent is contained" (residuals below).

Provenance: engine branch `feat/langgraph-hybrid-orchestration`, worktree
`~/software-factory-langgraph`; Hermes
`v0.23.0+689.ga9ff979` (`~/.hermes/hermes-agent` at `a9ff9799e273`). Design
inputs: `t_2abae28a` v2 (`ARCHITECTURE_1.md`, `DESIGN-VERDICT-v2.md`,
`REVIEW-RESPONSE.md`).

## What changed

| Area | Change |
| --- | --- |
| `factory_orchestration/adapters/hermes.py` | Default-deny child environment (allowlist, not a denylist). `HERMES_DELEGATED_CHILD_CONTEXT` is preserved verbatim; `HERMES_KANBAN_*`, `HERMES_PROFILE*`, `HERMES_ACCEPT_HOOKS`, `TERMINAL_*`, `DOCKER_*`, `PYTHONPATH` are never inherited. `-p` (or an explicit disposable `HERMES_HOME`) is the only identity input. |
| `HermesBoard.view` | Fails closed with `owner_grant_absent` unless this process holds the dispatcher grant (`HERMES_KANBAN_TASK` + `RUN_ID` + `CLAIM_LOCK`) **and** the board still lists that run as the live `running` run. `heartbeat` remains liveness only and authorizes nothing. |
| `factory_orchestration/adapters/reviewer_sandbox.py` | R2-A attestation: disposable reviewer home, pinned image digest, read-only candidate/evidence/probe mounts, `--network=none`, non-root, `--read-only` rootfs, no socket, no credential env. Three layers: effective config, executed preflight through the real tool stack, container inspection and in-container probes. |
| `factory/policy.py` | The only path to Docker: `container_image_digest` (validated image reference, fixed `docker image inspect` argv, fails closed when the image has no local `repo@sha256:` pin — a mutable tag is never accepted), `find_containers`, `inspect_container`, `exec_container_probe` (fixed code-owned argv; requires both the Hermes label and the exact reviewer profile label), `remove_own_container` (own profile label only). |
| `HermesReviewer` | Executes the review only after `attestation.enforced`; re-asserts the terminal posture **and the attested mount sources/image/home/tree digests** after switching the model endpoint; persists a complete durable receipt (`schema: factory-review-result/v1`) so a restart can adopt rather than resubmit; audits every `tool_use` name and path; binds candidate/base/spec/evidence digests from adapter state, never from model text. |
| `factory_orchestration/cli.py` | `run`/`resume` accept `--reviewer-sandbox`; the default remains `NoReadonlyEnforcement`, so the shipped path refuses unless the operator opts in explicitly. |
| `scripts/attest_reviewer_sandbox.py` | Host-side, explicitly invoked attestation (the offline suite blocks Docker on purpose). |

## Verified on this host

Attestation on the frozen candidate (clean `git archive` of the implementation
revision) and the selected evidence tree (`docs/langgraph-hybrid/evidence/`),
receipt committed at `docs/langgraph-hybrid/evidence/reviewer-sandbox/attestation.json`
with explicit provenance (`revision: 651b2ea…`, candidate/evidence paths and
digests):

```
enforced: True  reason: readonly_sandbox_attested
```

The review-run attestation is kept separately at
`evidence/reviewer-sandbox/live-review-attestation.json`; its recomputed digest
is the `attestation_digest` recorded in `live-review-run.json` and in the
durable review receipt, so the review uses exactly the sandbox that was
measured for it.

Executed findings recorded in that receipt:

- effective config: `terminal.backend=docker`, `container_persistent=false`,
  `docker_network=false`, `docker_forward_env=[]`,
  `docker_mount_cwd_to_workspace=false`; volumes are the two `:ro` trees plus the
  `:ro` probe mount; image pinned by digest, resolved through
  `policy.container_image_digest` (a mutable tag is refused, and no module in
  `factory_orchestration/` builds a `docker` argv itself — enforced by a test).
- resolved surface measured from the provider request: exactly
  `{patch, read_file, search_files, write_file}`.
- `write_file`/`patch` on the read-only mounts fail with `Read-only file system`
  (EROFS), and the host candidate/evidence digests are unchanged.
- a host canary outside every Hermes denylist, `~/.hermes/auth.json`, and a
  symlink from the candidate to the host canary are all unreadable
  (`File not found` / `not a regular file`).
- `terminal`, `execute_code`, `kanban_complete`, `delegate_task` were each offered
  by the provider in a separate run and never executed (no `tool_use`).
- container: `NetworkMode=none`, `ReadonlyRootfs=true`, user `10001:10001`,
  `CapEff=0`, `NoNewPrivs=1`, no `/var/run/docker.sock`, no docker CLI, no
  default route; `touch /evidence/...` → EROFS; `/tmp` and `/home/reviewer`
  writable (ephemeral); egress probes to `1.1.1.1:443` and `8.8.8.8:53` →
  `ENETUNREACH`; no host path visible.
- only host bind mounts are the `:ro` candidate/evidence/probe trees plus the
  disposable reviewer home's own cache directories (Hermes runtime, created by
  the factory, read-only). Any other host mount fails the attestation.

Live review transport, exercised against a disposable fixture with the
configured provider: transcript and parsed verdict in
`evidence/reviewer-sandbox/live-review-fixture.jsonl`, the adapter's durable
result in `...-result.json`, and a run summary in `live-review-run.json`
(17 `tool_use` events, verdict `approve`, audit violations `[]`). The attestation
receipt was produced by the same adapter instance and its recomputed digest
matches `live-review-run.json:attestation_digest`; `live-review-result.json`
carries the full binding (`operation_key`, candidate/base, spec/evidence
digests, `graph_version`) rather than a compact verdict.

The post-hoc transcript audit allows `/evidence/**` for every tool and a bare
`/` only as the `path` argument of `search_files` (a container-local root
listing). The container's host mounts are the read-only candidate, evidence and
probe binds under `/evidence`, plus the disposable reviewer profile's own
read-only `~/.hermes` directories (attachments, composer-pastes, caches) under
`/root/.hermes`; `/Users` and the host `~/.hermes/auth.json` are absent. That
audit is detection, not prevention: prevention is the container posture measured
by the attestation, and a path outside the container's mounts has nothing to
read.
Any other path-like argument, any out-of-surface tool, or a missing terminal
`result` event makes the review `inconclusive` (`review_scope_violation`).

## Reproduce

```sh
# offline suite (Docker blocked by the test boundary)
PYTHONPATH= .venv/bin/python -m pytest -q

# executed attestation on this host (creates and removes its own container)
# candidate is a clean extract: git archive <rev> | tar -x -C <candidate>
PYTHONPATH= .venv/bin/python scripts/attest_reviewer_sandbox.py \
  --root /path/to/scratch/reviewer-sandbox \
  --candidate /path/to/candidate-worktree \
  --evidence /path/to/selected-evidence \
  --revision "$(git rev-parse HEAD)" \
  --out docs/langgraph-hybrid/evidence/reviewer-sandbox/attestation.json

# attestation + one real-provider review through the same adapter instance
PYTHONPATH= .venv/bin/python scripts/live_review_fixture.py --run \
  --provider-profile factory-runner \
  --out docs/langgraph-hybrid/evidence/reviewer-sandbox \
  --scratch /path/to/scratch/lg-live-review

# full graph: real Hermes worker + attested reviewer + installed gate
PYTHONPATH= .venv/bin/python scripts/langgraph_hermes_acceptance.py --run \
  --scratch /path/to/scratch/lg-hermes-acceptance
```

The offline tests that pin these behaviours are
`tests/test_orchestration_reviewer_sandbox.py` (exact mount binding, mutated
config refusal, attestation reuse refusal, ambiguous volume parser, image-pin
delegation, and a guard that no orchestration module builds a `docker` argv) and
`tests/test_policy.py` (Docker helper validation, image-reference and
digest fail-closed branches, foreign-profile refusal).

## What this does and does not contain

Contained (tool effects of the reviewer): source/evidence mutation, host-home and
credential reads, Docker socket/CLI, network egress, model-chosen subprocesses
(no such tool), board mutation (no kanban tool, no task identity, fence kept).

**Not contained, and not claimed:** the host-side Hermes launcher, its profile
home and session DB run as the host user; reading `/evidence` is exfiltration to
the configured provider; Hermes, the image, the Docker daemon and the runtime are
trusted; `execve` inside the container is not denied (Hermes's own file tools are
`docker exec` commands); ephemeral tmpfs writes are allowed; this is not
graph-level isolation. The full residual list is embedded in every receipt.

T10 is satisfied for the reviewer's **tool effects** by an executed attestation;
it is not a claim of whole-agent containment, and the `NoReadonlyEnforcement`
refusal remains the default for any adapter that has no attestor.
