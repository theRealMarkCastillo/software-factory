# **ChronoGraph: Temporal Knowledge Graph & Fact Conflict Engine**

# **Master Architecture & Agent Execution Handoff Specification**

## **1\. Project Overview & Architectural Philosophy**

**ChronoGraph** is an enterprise-grade temporal knowledge graph engine designed to track, store, invalidate, and visualize evolving state assertions over time. Traditional graph databases record facts as static edges; ChronoGraph introduces point-in-time bi-temporal mechanics, automatically invalidating stale assertions when functional state conflicts occur (e.g., dynamic IP reassignments, organizational role updates, service status toggles, infrastructure version shifts).

```
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │                                   API Layer                                      │
 │                            FastAPI (Python 3.12)                                 │
 └─────────────────────────┬──────────────────────────────┬─────────────────────────┘
                           │                              │
                           ▼                              ▼
 ┌──────────────────────────────────────────┐   ┌───────────────────────────────────┐
 │       System of Record / Ledger          │   │      Live Graph Projection        │
 │              PostgreSQL 16               │──►│         Neo4j 5 Community         │
 │  - Raw Episodes                          │   │  - :Entity Nodes                  │
 │  - Append-only fact_ledger               │   │  - Parameterized :FACT / Relations│
 │  - Full Audit & Lineage Logs             │   │  - Lineage [:SUPERSEDED_BY] Links │
 └──────────────────────────────────────────┘   └───────────────────────────────────┘
```

### **Executive Goal**

To take this specification from zero to a fully operational, multi-container full-stack application running in local Docker Compose using a single orchestration command: `docker compose up -d --build --wait`. The entire system operates deterministically out of the box with zero external API key requirements.

### **Hybrid Dual-Store Architecture**

1. **PostgreSQL 16 (System of Record & Immutable Audit Store)**  
   * Functions as the single source of truth.  
   * Maintains append-only records of incoming raw `episodes`, detailed `extraction_logs`, and the primary `fact_ledger`.  
   * Manages fact lifecycles through explicit statuses: `active`, `superseded`, and `retracted`.  
   * Holds hard foreign key lineage pointers (`superseded_by_id`) and projection markers (`neo4j_synced_at`).  
2. **Neo4j 5 Community (Live Operational Graph Projection)**  
   * Acts as a read-optimized, queryable projection of the active and historical graph topology.  
   * Accessed asynchronously via the official `neo4j` Python driver (`AsyncGraphDatabase`).  
   * Driver instantiation managed tightly inside FastAPI's `lifespan` manager, bound explicitly to `database_="neo4j"`.  
   * State projections use deterministic Cypher `MERGE` operations to guarantee idempotency across ingest cycles.

---

## **2\. Container Topology & Docker Compose Contract**

The application ecosystem is structured into five isolated services. The orchestration contract requires deterministic startup ordering enforced by explicit healthchecks.

| Service | Image / Base | Target Container Ports | Internal Dependencies | Healthcheck Criteria / Protocol |
| :---- | :---- | :---- | :---- | :---- |
| `db` | `postgres:16-alpine` | `5432` | None | `pg_isready -U postgres` (Interval: 5s, Timeout: 5s, Retries: 10\) |
| `graph` | `neo4j:5-community` | `7474` (HTTP), `7687` (Bolt) | None | `cypher-shell -u neo4j -p chronograph_pass "RETURN 1;"` (Start Period: 40s, Interval: 10s, Timeout: 10s, Retries: 12\) |
| `api` | Python 3.12 (FastAPI) | `8000` | `db (healthy), graph (healthy)` | `curl -f http://localhost:8000/api/health` (Interval: 5s, Timeout: 5s, Retries: 10\) |
| `web` | Node 22 (React 19, Vite, TS) | `3000` | `api (healthy)` | `curl -f http://localhost:3000/` (Interval: 5s, Timeout: 5s, Retries: 5\) |
| `e2e` | Playwright Node Container | Host execution | `web (healthy)` | Executes test suite against `http://web:3000` (or `localhost:3000` on host) |

### **Exact Service Specification (`docker-compose.yml`)**

```
version: '3.8'

services:
  db:
    image: postgres:16-alpine
    container_name: chronograph-db
    environment:
      POSTGRES_DB: chronograph
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres_password
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
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
    ports:
      - "7474:7474"
      - "7687:7687"
    volumes:
      - neo4jdata:/data
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p chronograph_pass 'RETURN 1;'"]
      start_period: 40s
      interval: 10s
      timeout: 10s
      retries: 12

  api:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: chronograph-api
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:postgres_password@db:5432/chronograph
      NEO4J_URI: bolt://graph:7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: chronograph_pass
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
      graph:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
      interval: 5s
      timeout: 5s
      retries: 10

  web:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    container_name: chronograph-web
    ports:
      - "3000:3000"
    environment:
      - VITE_API_BASE_URL=http://localhost:8000
    depends_on:
      api:
        condition: service_healthy

volumes:
  pgdata:
  neo4jdata:
```

---

## **3\. Data Model, Temporal Conflict Resolution & Cypher Schema**

### **PostgreSQL Schema Definitions**

```
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

### **Neo4j Property Graph Model & Constraints**

```
// Ensure unique entities by name
CREATE CONSTRAINT entity_name_unique IF NOT EXISTS 
FOR (e:Entity) REQUIRE e.name IS UNIQUE;

// Model Structure:
// (:Entity {name: $subject}) -[r:RELATION {fact_id, predicate, valid_from, valid_until, status, episode_id}]-> (:Entity {name: $object})
// Lineage structure:
// (:FactNode {id: $old_fact_id}) -[:SUPERSEDED_BY]-> (:FactNode {id: $new_fact_id})
```

### **Functional Predicate Superseding Engine**

Functional predicates represent single-valued properties for an entity at any given point in time.

```py
FUNCTIONAL_PREDICATES = {
    "HAS_IP", 
    "CURRENT_ROLE", 
    "STATUS", 
    "LOCATION", 
    "ASSIGNED_TO", 
    "VERSION", 
    "PRIMARY_HOST"
}
```

#### **Conflict Resolution Algorithm**

When a new triple \$(S, P, O\_{\\text{new}}, T\_{\\text{new}})\$ arrives:

```
[Incoming Triple (S, P, O_new, T_new)]
                 │
                 ▼
     Is P in FUNCTIONAL_PREDICATES?
                 │
        ┌────────┴────────┐
        YES               NO
        │                 │
        ▼                 ▼
Query active facts for (S, P)    Directly Insert Fact
where status = 'active'          (No invalidation needed)
        │
        ▼
Does active fact exist with O_old != O_new?
        │
        ┌────────┴────────┐
        YES               NO
        │                 │
        ▼                 ▼
1. Update old fact in DB:           Insert New Fact
   - status = 'superseded'          (status = 'active')
   - valid_until = T_new
   - superseded_by_id = new_fact.id
2. Insert new fact (status = 'active')
3. Project changes & [:SUPERSEDED_BY] to Neo4j
```

#### **Cypher Transaction Projections**

```
// 1. Merge Subject and Object Nodes
MERGE (s:Entity {name: $subject})
MERGE (o:Entity {name: $object})

// 2. Invalidate Old Graph Fact (If Superseded)
WITH s, o
OPTIONAL MATCH (s)-[old_r:RELATION {predicate: $predicate, status: 'active'}]->(:Entity)
WHERE $is_functional = true AND $is_superseding = true
SET old_r.status = 'superseded',
    old_r.valid_until = $valid_from

// 3. Create New Active Relation Fact
CREATE (s)-[r:RELATION {
    fact_id: $fact_id,
    predicate: $predicate,
    valid_from: $valid_from,
    valid_until: null,
    status: 'active',
    episode_id: $episode_id
}]->(o)

// 4. Create Reified Nodes for Lineage Tracking
MERGE (fn_new:FactNode {id: $fact_id})
WITH fn_new
FOREACH (old_id IN CASE WHEN $superseded_fact_id IS NOT NULL THEN [$superseded_fact_id] ELSE [] END |
    MERGE (fn_old:FactNode {id: old_id})
    MERGE (fn_old)-[:SUPERSEDED_BY]->(fn_new)
)
```

---

## **4\. REST API Contract (`contracts/openapi.yaml` Specification)**

Below is the OpenAPI 3.0.3 specification defining the API contract.

```
openapi: 3.0.3
info:
  title: ChronoGraph Temporal API
  version: 1.0.0
  description: Engine for temporal knowledge graph ingestion, fact versioning, and state superseding.
paths:
  /api/health:
    get:
      summary: System Healthcheck
      responses:
        '200':
          description: Operational status of PostgreSQL and Neo4j connections.
          content:
            application/json:
              example:
                status: "ok"
                postgres: "ok"
                neo4j: "ok"
        '503':
          description: One or more data stores are unreachable.

  /api/episodes:
    post:
      summary: Ingest raw episode text and run extraction pipeline
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [title, raw_text, observed_at]
              properties:
                title:
                  type: string
                source_type:
                  type: string
                  default: "text"
                raw_text:
                  type: string
                observed_at:
                  type: string
                  format: date-time
      responses:
        '201':
          description: Episode processed, facts derived, conflicts resolved.
    get:
      summary: List chronologically ingested episodes
      responses:
        '200':
          description: List of episodes with extraction metrics.

  /api/graph:
    get:
      summary: Retrieve point-in-time graph topology
      parameters:
        - name: as_of
          in: query
          required: false
          schema:
            type: string
            format: date-time
          description: ISO8601 timestamp for point-in-time state evaluation.
        - name: include_superseded
          in: query
          required: false
          schema:
            type: boolean
            default: false
      responses:
        '200':
          description: Returns node and edge collections matching point-in-time rules.

  /api/facts:
    get:
      summary: Query fact ledger
      parameters:
        - name: status
          in: query
          schema:
            type: string
            enum: [active, superseded, all]
            default: active
        - name: entity
          in: query
          schema:
            type: string
      responses:
        '200':
          description: List of ledger facts.

  /api/conflicts:
    get:
      summary: Fetch conflict resolution chains and superseding lineage
      responses:
        '200':
          description: Ordered list of conflict replacement chains.

  /api/demo/seed:
    post:
      summary: Seed application with deterministic 4-episode conflict scenario
      responses:
        '200':
          description: Database wiped and reseeded with multi-episode conflict progression.
```

---

## **5\. Frontend UI Specification (`web`)**

The user interface provides interactive visual analysis of temporal state modifications.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  ChronoGraph | Temporal Visualizer               [ Load Multi-Episode Demo ]      │
├─────────────────────────────────────────┬────────────────────────────────────────┤
│                                         │                                        │
│          Interactive Graph Canvas       │       Conflict & Lineage Inspector     │
│                                         │                                        │
│   (Server-01) ──[HAS_IP (Active)]──► (10.0.0.45)                                 │
│        │                                │  Selected Node: Server-01              │
│        │                                │  History Chain:                        │
│        └──[HAS_IP (Superseded)]─► (10.0.0.12)  - v1: 10.0.0.12 (T1 -> T2) [Stale] │
│                                         │  - v2: 10.0.0.45 (T2 -> NOW) [Active] │
│                                         │                                        │
├─────────────────────────────────────────┴────────────────────────────────────────┤
│  [Play ▶] ───●─────────────────────────────────────────────── [T4: 2026-03-31]    │
│            Time Travel Scrubber (Scrub point-in-time state)                      │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### **Primary UI Components**

1. **Interactive Graph Canvas (D3 Force / Cytoscape / SVG)**  
   * Renders `:Entity` nodes and directional predicate edges.  
   * **Visual Coding:**  
     * `active` relationships render with **solid emerald (`#10B981`) or cyan (`#06B6D4`)** strokes.  
     * `superseded` relationships render with **dashed amber (`#F59E0B`) or slate (`#64748B`)** strokes with muted opacity.  
   * Clicking nodes or edges focuses details inside the Lineage Inspector.  
2. **Temporal Scrubber ("Time Travel" Slider)**  
   * A full-width interactive range control spanning \$\\min(\\text{valid\_from})\$ to \$\\max(\\text{observed\_at})\$.  
   * Scrubbing triggers a debounced query to `/api/graph?as_of=<SLIDER_VALUE>`.  
   * Edge visibility updates dynamically, allowing users to watch dynamic reassignments happen step by step.  
3. **Episode Ingestion Drawer & Seed Control**  
   * Drawer containing a text area accepting structured markdown lines:  
     `- Subject | PREDICATE | Object`  
   * Includes an explicit control: **"Load Multi-Episode Conflict Demo"** (`POST /api/demo/seed`), loading predefined state transitions without external API keys.  
4. **Conflict & Lineage Inspector Panel**  
   * Displays step-by-step conflict chains for any selected element.  
   * Shows source episode IDs, exact point-in-time transitions (\$T\_{\\text{valid\_from}} \\to T\_{\\text{valid\_until}}\$), and superseding links (\$v\_1 \\to v\_2 \\to v\_3\$).

---

## **6\. Universal Agent Guardrails & Verification Gate**

### **Context Hygiene Rule**

To maintain continuous execution within context constraints, subshell execution engines (Google Antigravity / Claude Code) **MUST NEVER** stream raw build output, dependencies installation logs, or verbose test reports directly to stdout.

* Redirect all verbose execution output to local temporary log files (e.g., `docker compose build > /tmp/docker_build.log 2>&1`).  
* Inspect status solely via exit codes (`echo $?`) and targeted tail inspections (`tail -n 20 /tmp/docker_build.log`).

### **Stack Verification Script (`scripts/verify-stack.sh`)**

Create `scripts/verify-stack.sh` to act as an unpassable verification gate across execution environments.h  
\#\!/usr/bin/env bash  
set \-euo pipefail

echo "=== \[1/4\] Booting ChronoGraph Stack \==="  
docker compose down \-v \--remove-orphans \> /tmp/docker\_down.log 2\>&1 \|\| true  
docker compose up \-d \--build \--wait \--wait-timeout 180 \> /tmp/docker\_up.log 2\>&1 \|\| {  
echo "X Stack boot failed. Log tail:"  
tail \-n 30 /tmp/docker\_up.log  
exit 1  
}  
echo "✓ All services healthy."

echo "=== \[2/4\] Verifying System Health Endpoints \==="  
HEALTH\_RESP=\$(curl \-s \-f http\://localhost:8000/api/health \|\| echo "FAILED")  
if \[\[ "\$HEALTH\_RESP" \!= *"ok"* \]\]; then  
echo "X API Health failed: \$HEALTH\_RESP"  
exit 1  
fi  
echo "✓ API Health check passed: \$HEALTH\_RESP"

echo "=== \[3/4\] Triggering Demo Seed & Ingest Check \==="  
SEED\_RESP=\$(curl \-s \-f \-X POST http\://localhost:8000/api/demo/seed \|\| echo "FAILED")  
if \[\[ "\$SEED\_RESP" \!= *"success"* && "\$SEED\_RESP" \!= *"ok"* \]\]; then  
echo "X Seeding failed: \$SEED\_RESP"  
exit 1  
fi  
echo "✓ Demo dataset successfully ingested and transformed."

echo "=== \[4/4\] Running Live-Container PyTest & Host Playwright Suite \==="  
docker compose exec \-T api pytest \-v \> /tmp/pytest.log 2\>&1 \|\| {  
echo "X Backend tests failed. Tail:"  
tail \-n 30 /tmp/pytest.log  
exit 1  
}  
echo "✓ PyTest backend verification successful."

if command \-v npx &\> /dev/null && \[ \-d "frontend/e2e" \]; then  
(cd frontend && npx playwright test \> /tmp/playwright.log 2\>&1) \|\| {  
echo "X Playwright E2E tests failed. Tail:"  
tail \-n 30 /tmp/playwright.log  
exit 1  
}  
echo "✓ Playwright E2E verification successful."  
fi

echo "================================================="  
echo " SUCCESS: ChronoGraph architecture verified. "  
echo "================================================="

````

#### Claude Code Desktop Hooks (`.claude/settings.json`)

To ensure execution portability into Claude Code Desktop, project settings enforce the completion gate automatically.

```json
{
  "hooks": {
    "Stop": [
      {
        "type": "command",
        "command": "./scripts/verify-stack.sh"
      }
    ]
  }
}
````

---

## **7\. Deterministic Demo Seed Dataset**

When calling `POST /api/demo/seed`, the backend executes an idempotent reset and ingests four chronological infrastructure episodes containing explicit state assertions.

```
Episode 1 [T1: 2026-01-01T00:00:00Z] Initial Setup
- AuthCluster | HAS_IP | 10.0.0.12
- AuthCluster | VERSION | 1.0.0
- AuthCluster | STATUS | HEALTHY
- Alice | CURRENT_ROLE | Infrastructure Lead

Episode 2 [T2: 2026-01-05T12:00:00Z] IP Reassignment & Promotion
- AuthCluster | HAS_IP | 10.0.0.45          <-- [Supersedes Episode 1 IP]
- Alice | CURRENT_ROLE | Principal Architect <-- [Supersedes Role]

Episode 3 [T3: 2026-01-10T08:30:00Z] Incident Outage
- AuthCluster | STATUS | DEGRADED            <-- [Supersedes Status]
- AuthCluster | ASSIGNED_TO | Alice

Episode 4 [T4: 2026-01-10T09:15:00Z] Hotfix Deployment
- AuthCluster | VERSION | 1.0.1              <-- [Supersedes Version]
- AuthCluster | STATUS | HEALTHY             <-- [Supersedes Status back to HEALTHY]
```

---

## **8\. Ready-to-Run Launch Prompts**

Use the following exact sequences to execute this build using Google Antigravity Ultra or Claude Code Desktop.

### **Sequence A: Google Antigravity Ultra Execution Flow**

#### **Step 1: `/grill-me`**

```
/grill-me Read the master specification for ChronoGraph: Temporal Knowledge Graph & Fact Conflict Engine. Interrogate me on any missing edge cases in temporal invalidation, Cypher projection details, UI temporal scrubbing mechanics, or docker compose dependencies until you have 100% clarity to execute without breaking.
```

#### **Step 2: `/plan`**

```
/plan Construct a multi-milestone execution plan to build ChronoGraph strictly according to the architecture specification. Split work into 5 sequential, git-committed milestones. Ensure context hygiene guidelines are strictly met (directing heavy output to /tmp/*.log) and end with running scripts/verify-stack.sh.
```

#### **Step 3: `/goal`**

```
/goal Build and deliver ChronoGraph across 5 sequential milestones:

Milestone 1: Container Topology & Core Orchestration
- Implement docker-compose.yml with db (Postgres 16), graph (Neo4j 5), api (FastAPI), and web (React+Vite).
- Establish wait-for-health dependencies. Ensure docker compose up -d --build --wait passes cleanly.

Milestone 2: Database Schemas & Dual-Store Driver Setup
- Implement Postgres migrations for episodes, fact_ledger, and extraction_logs.
- Setup FastAPI async SQLAlchemy/asyncpg connection pool and Neo4j AsyncGraphDatabase client inside lifespan manager.
- Implement GET /api/health validating both store connections.

Milestone 3: Rule-Based Fact Extraction & Superseding Engine
- Implement the functional predicate resolution engine for FUNCTIONAL_PREDICATES = {"HAS_IP", "CURRENT_ROLE", "STATUS", "LOCATION", "ASSIGNED_TO", "VERSION", "PRIMARY_HOST"}.
- Build POST /api/episodes ingestion endpoint.
- Implement Cypher MERGE projections for active/superseded relations and [:SUPERSEDED_BY] lineage tracking.
- Implement POST /api/demo/seed endpoint loading the 4-episode test scenario.

Milestone 4: Temporal Query APIs & Interactive React Visualizer
- Implement GET /api/graph?as_of=<ISO8601> point-in-time retrieval.
- Build React 19 visualizer with graph canvas, solid/dashed edge status styling, temporal slider scrubber, and conflict lineage inspector.

Milestone 5: End-to-End Verification Gate & Hardening
- Create scripts/verify-stack.sh and configure .claude/settings.json.
- Run complete test suite and confirm stack passes with zero errors.
```

---

### **Sequence B: Claude Code Desktop Execution Prompt**

```
Build the full-stack "ChronoGraph: Temporal Knowledge Graph & Fact Conflict Engine" application strictly adhering to the architectural specification.

Key Instructions:
1. Setup local Docker Compose topology (Postgres 16, Neo4j 5, FastAPI Python 3.12, React 19 Vite UI).
2. Implement dual-store persistence: Postgres fact_ledger single-source-of-truth + Neo4j live graph projection with Cypher MERGE idempotency.
3. Enforce functional state invalidation logic for predicates {"HAS_IP", "CURRENT_ROLE", "STATUS", "LOCATION", "ASSIGNED_TO", "VERSION", "PRIMARY_HOST"}.
4. Implement UI featuring temporal time-travel slider scrubber, graph canvas, and conflict lineage inspector.
5. Practice context hygiene: pipe heavy build/test logs to /tmp/*.log and inspect only status codes or log tails.
6. Create scripts/verify-stack.sh and ensure it completes with zero errors.
```

