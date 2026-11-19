# ChronoGraph: Temporal Knowledge Graph & Fact Conflict Engine — Master Spec (as received)

Received from Mark 2026-11-18. Preserved for traceability; markdown escape artifacts
from the paste (`\#`, `\-`, `\|`, `\$`) normalized, content otherwise unchanged.

BINDING ORDER: `docs/spec/01-ratified-decisions.md` overrides this document wherever
they differ. Sections 6 (Claude Code hook) and 8 (launch prompts) are NOT in scope;
see SC-15 and SC-17.

## 1. Project Overview & Architectural Philosophy

ChronoGraph is an enterprise-grade temporal knowledge graph engine designed to track,
store, invalidate, and visualize evolving state assertions over time. Traditional graph
databases record facts as static edges; ChronoGraph introduces point-in-time temporal
mechanics, automatically invalidating stale assertions when functional state conflicts
occur (dynamic IP reassignments, organizational role updates, service status toggles,
infrastructure version shifts).

```
 API Layer: FastAPI (Python 3.12)
   |                                   |
   v                                   v
 System of Record / Ledger         Live Graph Projection
 PostgreSQL 16                 --> Neo4j 5 Community
  - Raw Episodes                    - :Entity Nodes
  - Append-only fact_ledger         - Parameterized :RELATION facts
  - Full Audit & Lineage Logs       - Lineage [:SUPERSEDED_BY] Links
```

### Executive Goal

Zero to a fully operational multi-container full-stack application in local Docker
Compose via `docker compose up -d --build --wait`. Deterministic out of the box, zero
external API keys.

### Hybrid Dual-Store Architecture

1. PostgreSQL 16 (System of Record & Immutable Audit Store)
   - Single source of truth.
   - Append-only records of raw `episodes`, `extraction_logs`, primary `fact_ledger`.
   - Fact lifecycle statuses: `active`, `superseded`, `retracted`.
   - Hard FK lineage pointers (`superseded_by_id`) and projection markers (`neo4j_synced_at`).
2. Neo4j 5 Community (Live Operational Graph Projection)
   - Read-optimized, queryable projection of active and historical topology.
   - Accessed via official `neo4j` Python driver (`AsyncGraphDatabase`).
   - Driver instantiated inside FastAPI `lifespan`, bound to `database_="neo4j"`.
   - Deterministic Cypher `MERGE` for idempotency across ingest cycles.

## 2. Container Topology & Docker Compose Contract

| Service | Image / Base | Ports | Depends on | Healthcheck |
|---|---|---|---|---|
| db | postgres:16-alpine | 5432 | — | `pg_isready -U postgres` (5s/5s/10) |
| graph | neo4j:5-community | 7474, 7687 | — | `cypher-shell -u neo4j -p chronograph_pass "RETURN 1;"` (start 40s, 10s/10s/12) |
| api | Python 3.12 FastAPI | 8000 | db, graph healthy | `curl -f http://localhost:8000/api/health` (5s/5s/10) |
| web | Node 22, React 19, Vite, TS | 3000 | api healthy | `curl -f http://localhost:3000/` (5s/5s/5) |
| e2e | Playwright Node container | — | web healthy | runs suite against `http://web:3000` |

Spec compose (dev reference; see SC-01 for factory form):

```yaml
services:
  db:
    image: postgres:16-alpine
    container_name: chronograph-db
    environment:
      POSTGRES_DB: chronograph
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres_password
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d chronograph"]
      interval: 5s
      timeout: 5s
      retries: 10
  graph:
    image: neo4j:5-community
    container_name: chronograph-graph
    environment:
      NEO4J_AUTH: neo4j/chronograph_pass
      NEO4J_dbms_security_auth__enabled: "true"
    ports: ["7474:7474", "7687:7687"]
    volumes: [neo4jdata:/data]
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p chronograph_pass 'RETURN 1;'"]
      start_period: 40s
      interval: 10s
      timeout: 10s
      retries: 12
  api:
    build: {context: ./backend, dockerfile: Dockerfile}
    container_name: chronograph-api
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:postgres_password@db:5432/chronograph
      NEO4J_URI: bolt://graph:7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: chronograph_pass
    ports: ["8000:8000"]
    depends_on:
      db: {condition: service_healthy}
      graph: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
      interval: 5s
      timeout: 5s
      retries: 10
  web:
    build: {context: ./frontend, dockerfile: Dockerfile}
    container_name: chronograph-web
    ports: ["3000:3000"]
    environment: [VITE_API_BASE_URL=http://localhost:8000]
    depends_on:
      api: {condition: service_healthy}
volumes:
  pgdata:
  neo4jdata:
```

## 3. Data Model, Temporal Conflict Resolution & Cypher Schema

### PostgreSQL schema

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE episodes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title VARCHAR(255) NOT NULL,
    source_type VARCHAR(50) NOT NULL DEFAULT 'text',
    raw_text TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE fact_ledger (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    subject VARCHAR(255) NOT NULL,
    predicate VARCHAR(255) NOT NULL,
    object VARCHAR(255) NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'active', -- active | superseded | retracted
    superseded_by_id UUID NULL REFERENCES fact_ledger(id),
    episode_id UUID NOT NULL REFERENCES episodes(id),
    neo4j_synced_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE extraction_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    episode_id UUID NOT NULL REFERENCES episodes(id),
    extractor_mode VARCHAR(50) NOT NULL,
    extracted_count INT NOT NULL DEFAULT 0,
    conflicts_resolved_count INT NOT NULL DEFAULT 0,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_fact_ledger_lookup ON fact_ledger (subject, predicate, status);
CREATE INDEX idx_fact_ledger_temporal ON fact_ledger (valid_from, valid_until);
```

### Neo4j model

```cypher
CREATE CONSTRAINT entity_name_unique IF NOT EXISTS
FOR (e:Entity) REQUIRE e.name IS UNIQUE;
// (:Entity {name:$subject})-[r:RELATION {fact_id, predicate, valid_from, valid_until, status, episode_id}]->(:Entity {name:$object})
// (:FactNode {id:$old_fact_id})-[:SUPERSEDED_BY]->(:FactNode {id:$new_fact_id})
```

### Functional predicates

```python
FUNCTIONAL_PREDICATES = {"HAS_IP", "CURRENT_ROLE", "STATUS", "LOCATION",
                         "ASSIGNED_TO", "VERSION", "PRIMARY_HOST"}
```

### Conflict resolution algorithm (incoming triple S, P, O_new, T_new)

- P not functional -> insert fact directly.
- P functional -> query active facts for (S, P).
  - Active fact exists with O_old != O_new ->
    1. old fact: status='superseded', valid_until=T_new, superseded_by_id=new.id
    2. insert new fact status='active'
    3. project changes and [:SUPERSEDED_BY] to Neo4j
  - otherwise -> insert new fact (active).  (Amended by SC-04/SC-05.)

### Cypher projection (as received; amended by SC-02/SC-03)

```cypher
MERGE (s:Entity {name: $subject})
MERGE (o:Entity {name: $object})
WITH s, o
OPTIONAL MATCH (s)-[old_r:RELATION {predicate: $predicate, status: 'active'}]->(:Entity)
WHERE $is_functional = true AND $is_superseding = true
SET old_r.status = 'superseded', old_r.valid_until = $valid_from
CREATE (s)-[r:RELATION {fact_id: $fact_id, predicate: $predicate, valid_from: $valid_from,
        valid_until: null, status: 'active', episode_id: $episode_id}]->(o)
MERGE (fn_new:FactNode {id: $fact_id})
WITH fn_new
FOREACH (old_id IN CASE WHEN $superseded_fact_id IS NOT NULL THEN [$superseded_fact_id] ELSE [] END |
    MERGE (fn_old:FactNode {id: old_id})
    MERGE (fn_old)-[:SUPERSEDED_BY]->(fn_new))
```

## 4. REST API (summary; concrete schemas in contracts/openapi.yaml)

- `GET /api/health` -> 200 `{status, postgres, neo4j}` all "ok"; 503 when a store is unreachable.
- `POST /api/episodes` {title, source_type="text", raw_text, observed_at} -> 201.
- `GET /api/episodes` -> episodes with extraction metrics, chronological.
- `GET /api/graph?as_of=<iso>&include_superseded=false` -> nodes + edges.
- `GET /api/facts?status=active|superseded|all&entity=<name>` -> ledger facts.
- `GET /api/conflicts` -> ordered conflict replacement chains.
- `POST /api/demo/seed` -> 200; wipe and reseed the 4-episode scenario.

## 5. Frontend UI

Header "ChronoGraph | Temporal Visualizer" with [Load Multi-Episode Demo].
Left: interactive graph canvas. Right: Conflict & Lineage Inspector. Bottom: time
travel scrubber with play button.

1. Graph canvas (D3 force / Cytoscape / SVG): :Entity nodes, directed predicate edges.
   active = solid emerald #10B981 or cyan #06B6D4; superseded = dashed amber #F59E0B
   or slate #64748B with muted opacity. Clicking a node/edge focuses the inspector.
2. Temporal scrubber: full-width range from min(valid_from) to max(observed_at);
   debounced query to `/api/graph?as_of=<value>`; edges update as you scrub.
3. Episode ingestion drawer: textarea of lines `- Subject | PREDICATE | Object`;
   explicit "Load Multi-Episode Conflict Demo" control (POST /api/demo/seed).
4. Conflict & Lineage Inspector: step-by-step chain for the selected element, source
   episode IDs, valid_from -> valid_until transitions, superseding links v1 -> v2 -> v3.

## 6. Agent guardrails and verification gate

Context hygiene: redirect verbose build/test output to log files; inspect exit codes
and log tails only.

`scripts/verify-stack.sh` (as received, intent): down -v; `up -d --build --wait
--wait-timeout 180`; curl /api/health must contain ok; POST /api/demo/seed must
contain success or ok; `docker compose exec -T api pytest -v`; then
`cd frontend && npx playwright test` when frontend/e2e exists; print SUCCESS.

`.claude/settings.json` Stop hook running verify-stack.sh — EXCLUDED (SC-15).

## 7. Deterministic demo seed

```
Episode 1 [T1: 2026-01-01T00:00:00Z] Initial Setup
- AuthCluster | HAS_IP | 10.0.0.12
- AuthCluster | VERSION | 1.0.0
- AuthCluster | STATUS | HEALTHY
- Alice | CURRENT_ROLE | Infrastructure Lead

Episode 2 [T2: 2026-01-05T12:00:00Z] IP Reassignment & Promotion
- AuthCluster | HAS_IP | 10.0.0.45            (supersedes episode 1 IP)
- Alice | CURRENT_ROLE | Principal Architect   (supersedes role)

Episode 3 [T3: 2026-01-10T08:30:00Z] Incident Outage
- AuthCluster | STATUS | DEGRADED              (supersedes status)
- AuthCluster | ASSIGNED_TO | Alice

Episode 4 [T4: 2026-01-10T09:15:00Z] Hotfix Deployment
- AuthCluster | VERSION | 1.0.1                (supersedes version)
- AuthCluster | STATUS | HEALTHY               (supersedes status back to HEALTHY)
```

## 8. Launch prompts (Antigravity /grill-me, /plan, /goal; Claude Code Desktop)

Out of scope (SC-17). Their five milestones informed the card chain.
