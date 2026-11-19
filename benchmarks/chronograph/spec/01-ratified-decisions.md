# ChronoGraph — Ratified Decisions (BINDING)

Ratified by Mark 2026-11-18 ("ratify all as proposed") from the intake record
(the intake record). SC-21+ are
implementation decisions recorded by the orchestrator under builder discretion; they change
structure, not product behaviour. This file OVERRIDES both `00-original.md` and
`00-condensed-spec.md`.
`contracts/openapi.yaml` is the wire contract. `fixtures/` is the frozen oracle.

A worker who finds a contradiction among these three sources stops and blocks with
the exact conflicting text. Never edit fixtures or the oracle to make a test pass.

## Invariants

- I1 Postgres is the system of record. Neo4j is a rebuildable projection.
- I2 At most one `active` fact per (subject, predicate) for functional predicates,
  enforced at the storage layer (partial unique index), not only in code.
- I3 Every superseded fact has `valid_until` = successor `valid_from` and
  `superseded_by_id` = successor id. `valid_until > valid_from` always.
- I4 Ledger rows are never deleted. Identity columns are immutable at the storage
  layer (trigger). Only lifecycle columns mutate. The demo seed reset is the sole wipe.
- I5 An episode is atomic: all of its facts and its extraction log commit, or nothing does.
- I6 Projection is idempotent. Re-projecting any fact any number of times yields
  the same graph.

## Product decisions

SC-01 Compose files. `docker-compose.yml` is the dev stack (spec host ports kept,
`container_name` removed, obsolete `version:` key removed, `test` service under
`profiles: ["test"]`). `compose.factory.yml` is the self-contained acceptance input:
no `ports`, no `container_name`, no bind mounts, no `restart: always`, named volumes
only, plus the `test` service. Both define db, graph, api and web identically apart
from ports. Neo4j heap is capped in the factory file (512m heap, 256m pagecache).

SC-02 The projection MERGEs relationships on `fact_id`:
`MERGE (s)-[r:RELATION {fact_id:$fact_id}]->(o) SET r.predicate=..., ...`. Never CREATE.

SC-03 Invalidation of the superseded graph edge matches `fact_id = $superseded_fact_id`,
not "any active edge with this predicate".

SC-04 Reassertion. If an active fact exists with the same (subject, predicate, object),
no new fact is written. Its id goes in `extraction_logs.details_json.reaffirmed`.
This applies to functional and non-functional predicates (SC-22).

SC-05 Ordering. For a functional predicate with active fact A, an incoming
`observed_at <= A.valid_from` with a different object rejects the whole episode:
HTTP 409 `{"detail":{"code":"out_of_order", "subject", "predicate",
"active_valid_from", "observed_at"}}`. Nothing is persisted, including the episode.
One episode asserting two different objects for the same functional (S, P):
HTTP 422 `{"detail":{"code":"conflicting_functional_assertions", "subject",
"predicate", "objects":[...]}}`, nothing persisted. Exact duplicate lines within one
episode are counted once and listed in `details_json.duplicate_lines`.

SC-06 Concurrency. Each episode runs in one transaction. Before reading the active
fact, take `pg_advisory_xact_lock` keyed on (subject, predicate) in a deterministic
order (sorted) to avoid deadlocks. Backstops: partial unique index
`(subject, predicate) WHERE status='active' AND predicate IN (<functional>)`, and
`(subject, predicate, object) WHERE status='active'`. A unique violation is never
surfaced as a 500.

SC-07 Write order inside the transaction: insert episode; allocate the successor id in the application; update the old fact (status='superseded', valid_until = successor valid_from, superseded_by_id = successor id, neo4j_synced_at = NULL); insert the successor fact with that id; insert the extraction log. `superseded_by_id` references fact_ledger(id) DEFERRABLE INITIALLY DEFERRED, so the FK is checked at commit. (Ratified 2026-11-18, resolves the SC-06/SC-07 conflict.)

SC-08 Mutability. Rows are never deleted (DELETE trigger raises, except during the seed
reset, which uses TRUNCATE). Immutable: id, subject, predicate, object, valid_from,
episode_id, created_at. Mutable lifecycle: status, valid_until, superseded_by_id,
neo4j_synced_at. Legal status transition: active -> superseded only. superseded ->
anything raises. `retracted` is reserved (SC-20). `episodes` and `extraction_logs`
reject UPDATE and DELETE. CHECK constraints:
`status IN ('active','superseded','retracted')`;
`(status = 'superseded') = (superseded_by_id IS NOT NULL)`;
`status <> 'superseded' OR valid_until IS NOT NULL`;
`valid_until IS NULL OR valid_until > valid_from`.

SC-09 `as_of` uses half-open intervals `[valid_from, valid_until)`. The default is the
current UTC time. Edges carry `status` = the temporal status at `as_of` (`active` if
`valid_from <= as_of < valid_until` or `valid_until` is null). Facts with
`valid_from > as_of` are never returned. `include_superseded=false` (the default)
returns only edges active at `as_of`. `true` also returns facts whose
`valid_until <= as_of`, with `status: "superseded"`. Nodes are exactly the endpoints of
the returned edges. `as_of` must be ISO 8601 with an offset or Z; naive or unparsable
values return 422.

SC-10 Projection failure. A Postgres commit is authoritative. If projection fails,
POST still returns 201 with `"projection":"pending"`, and the affected facts keep
`neo4j_synced_at = NULL`. The projector syncs every fact with `neo4j_synced_at IS NULL`,
in `created_at` order, at startup, after every ingest, and after seed. Success sets
`neo4j_synced_at`. A lifecycle update resets it to NULL in the same transaction.

SC-11 Wire shapes are defined in `contracts/openapi.yaml`. The seed returns
`{"status":"ok","episodes":4,"facts":10,"active":5,"superseded":5,"chains":4,"entities":10}`.

SC-12 Parser (`extractor_mode = "rule_based"`). A line matches
`^\s*-\s*S\s*\|\s*P\s*\|\s*O\s*$` with exactly three non-empty parts, each at most 255
characters after trimming. The predicate is upper-cased and must then match
`^[A-Z][A-Z0-9_]*$`. Subject and object are trimmed and kept case-sensitive. Blank lines
are ignored silently. Every other non-matching line is skipped and listed verbatim in
`details_json.skipped_lines`. An episode with zero facts is still stored: 201 with
`extracted_count = 0`. `valid_from` of every fact is the episode `observed_at`.

SC-13 Browser API base. The web server proxies `/api/*` to `http://api:8000`. The
frontend calls relative `/api/...` by default. `VITE_API_BASE_URL` stays an optional
override and defaults to an empty string.

SC-14 The `test` service exists in both compose files, and the `web` healthcheck exists.
The API image contains curl. The Playwright image tag equals the pinned
`@playwright/test` version.

SC-15 No `.claude/settings.json` and no Stop hook.

SC-16 (amended 2026-11-18, Mark) Deployment validation is binding acceptance. scripts/check-stack.sh probes a running stack using URLs from the environment (FACTORY_HOST + FACTORY_PORT_API_8000 / FACTORY_PORT_WEB_3000 when set, otherwise localhost:8000/3000); it performs no lifecycle actions. The factory runs it via `bin/factory run --deployment --check scripts/check-stack.sh --compose docker-compose.yml`. scripts/verify-stack.sh is the developer lifecycle wrapper (down -v, up --wait, check-stack.sh, profile test run). Checks use exact JSON comparison, not substrings.

SC-17 Section 8 launch prompts are out of scope.

SC-18 "Temporal" means valid time. Transaction-time queries are a non-goal for v1.

SC-19 The UI uses the seed timestamps. The mock's "T4: 2026-03-31" is wrong.

SC-20 `retracted` is reserved: allowed by the CHECK constraint, but no API produces it.
`GET /api/facts?status=` accepts `active|superseded|all`; anything else returns 422.

## Implementation decisions (the orchestrator)

SC-21 Layout is driven by the factory gates, which are repo-root scoped. These live at
the repository root: `pyproject.toml` + `uv.lock` (Python 3.12, package `chronograph`
at `backend/chronograph`, tests at `backend/tests`, pytest + pytest-cov in the dev
group), and an npm workspace `package.json` with `workspaces: ["frontend"]` and a root
`package-lock.json`. All images build with context `.` and per-service Dockerfiles
(`backend/Dockerfile`, `frontend/Dockerfile`, `acceptance/Dockerfile`). A
`.dockerignore` excludes `.git`, `.venv`, `node_modules`, `.env*` and `.entire`.

SC-22 Non-functional duplicates: identical active (S, P, O) is reaffirmed, not duplicated
(see SC-04).

SC-23 One test container. The runner uses `up --abort-on-container-exit --exit-code-from
test`, so `test` is the ONLY container that may exit. Migrations, constraint setup and
projector catch-up run inside the api lifespan, never as one-shot services. `test` is
built from `acceptance/Dockerfile` (Playwright Node image with Python 3.12 and the uv
dev environment). It depends on `web: service_healthy`. Its entrypoint,
`acceptance/run.sh`, runs `pytest -m integration` against the live services, then
`npx playwright test` from `frontend/` when `frontend/e2e` exists. It exits non-zero if
either fails. Source is image-contained (COPY), never bind-mounted.

SC-24 Host coverage. The factory coverage gate runs host pytest and requires 100% of
added Python lines covered, with no services available. Integration tests carry
`@pytest.mark.integration` and are skipped unless `CHRONOGRAPH_INTEGRATION=1` (set
only in the test container). Keep I/O adapters thin. The pure core is tested directly.
Adapter lines may be covered on the host by labelled contract doubles
(`backend/tests/unit/`), but the behavioural evidence is the integration test in the
container. Forbidden: coverage `omit`, blanket `# pragma: no cover`, mocks presented
as storage or concurrency proof.

SC-25 Migrations are plain SQL files, `backend/migrations/NNN_name.sql`. The lifespan
applies them in order under `pg_advisory_lock`, tracked in `schema_migrations`.
Applying them twice is a no-op.

SC-26 Neo4j temporal properties are stored as Neo4j DateTime (UTC), not strings, so
Cypher comparisons are temporal. API output is ISO 8601 UTC with `Z` and microsecond
precision when non-zero.

SC-27 The UI renders the graph as SVG DOM (D3-force or hand-rolled). Canvas renderers
are out because e2e needs DOM selectors. The stable `data-testid` contract is in
`docs/spec/02-ui-contract.md`.
