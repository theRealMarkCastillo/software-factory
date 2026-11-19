# ChronoGraph benchmark kit

ChronoGraph is a temporal knowledge graph and fact-conflict engine: PostgreSQL
keeps the authoritative episode and fact ledger, Neo4j projects the graph, and a
React visualizer exposes point-in-time state and replacement lineage. Extraction
is deterministic and rule-based; no external API keys are required.

## Task and frozen answer key

Build a working application to the **ratified specification**, not just a demo
that returns the expected counts. This kit contains the requirements and answer
key, not the reference implementation:

- [Master spec as received](spec/00-master-spec-as-received.md).
- [Ratified decisions](spec/01-ratified-decisions.md), which override the master
  spec, including its excluded hooks and launch prompts.
- [UI contract](spec/02-ui-contract.md), including stable browser-test selectors.
- [OpenAPI wire contract](contracts/openapi.yaml).
- [Frozen seed](fixtures/demo-seed.json) and [oracle](fixtures/demo-seed-oracle.json):
  the shared answer key for episode counts, ledger state, conflict chains and
  temporal graph queries. Fixture-local labels are not production UUIDs; compare
  graph edges as sets, as described in the oracle.
- [SHA256SUMS](fixtures/SHA256SUMS), binding the two JSON files byte-for-byte.

The inputs were frozen at revision
`2697d343b0b6005a1b0c5e9575a1702e1173885b`. Contracts and fixtures are unmodified;
only three provenance lines in the ratified decisions were neutralized. Historical
`docs/spec/` references in these verbatim documents correspond to this kit's
`spec/` directory. Referenced implementation paths describe what the participant
must build; they are not additional files supplied by this kit.

Verify the answer key **from this directory** before a run:

```sh
sha256sum -c fixtures/SHA256SUMS
# macOS alternative:
shasum -a 256 -c fixtures/SHA256SUMS
```

Both checksum paths resolve here without rewriting the original sums file. The
oracle records counts, not a seed-hash field; the sums file binds seed and oracle.
Never edit the fixtures or oracle to make an implementation pass.

Credentials in the preserved spec are **demo-only values**, not secrets or
production defaults. Use isolated disposable resources. The original Compose
example is a development reference, not permission for host-wide cleanup or
network exposure: explicitly check published port bindings and prefer loopback
for local-only access. Apply your own factory's safety policy to lifecycle actions.

## Suggested execution chain

1. **C1 — topology/health:** healthy PostgreSQL, Neo4j, API and web services.
2. **C2 — ledger + supersede:** extraction, transactional ingest, storage invariants,
   duplicate handling and conflict replacement.
3. **C3 — graph projection:** idempotent projection and recovery after failures.
4. **C4 — point-in-time APIs:** half-open temporal boundaries and wire contracts.
5. **C5 — UI + browser e2e:** visualizer, scrubber, ingestion and lineage inspector.
6. **C6 — real-deployment check:** probe the running deployment via its published
   URLs, independently of lifecycle management, using exact comparisons.
7. **Final real run:** start a fresh deployment, load the frozen seed, exercise
   the UI and APIs as a user, scrub across replacement boundaries and inspect
   lineage. Run integration tests against empty stores as well as seeded user
   flows; inspect actual host port exposure. Preserve commands, revision, logs,
   browser evidence and teardown outcomes. Unit doubles are not deployment proof.

Independent review should assess each bounded candidate, not trust a green marker
or another worker's summary. Record unavailable paths as INCONCLUSIVE.

## What to measure

Report the exact input revision, candidate revisions, environment and evidence
limits, together with:

- Worker runs and worker time, distinguishing summed worker time from wall time.
- Active versus elapsed time; human decisions, interruptions and waiting time.
- Independent review counts and outcomes, blocks and repair cycles.
- Tokens and cost (or explicitly mark them unavailable).
- Defects found only through real use, beyond unit/integration green markers.
- Port exposure: intended versus observed bindings, including all-interface binds.

Use consistent timing definitions across factories. Concurrent worker durations
need not sum to active wall time. Separate product work from factory repairs.

## Reference run

These are operator-supplied measurements for this factory's reference run, not
results reproduced by publishing this kit. Durations are **measured from Kanban
run timestamps and rounded, not estimates**; counts are recorded observations.
Token figures below are also recorded measurements, rounded for presentation.
They are context, not a performance guarantee.

| Measure | Reference observation |
|---|---|
| ChronoGraph implementation | 17 runs; approximately 3.6 h product worker time (measured, rounded) |
| Factory fixes | 19 runs; approximately 4.5 h worker time (measured, rounded) |
| Active time | Approximately 6.6 h (measured, rounded) |
| Elapsed time | Approximately 11.75 h, including approximately 4.5 h waiting on one human decision (both measured, rounded) |
| Independent reviews | 15: 14 PASS, 1 FAIL |
| Blocks | 7 |
| Defects found only by a real run | 2: integration tests used an already-seeded database; development ports were bound to all interfaces |
| Workers | GPT-6.1 via subscription; 36 sessions; 1,634 model calls |
| Recorded token usage | Approximately 128M input tokens (97% cache); approximately 477K output tokens |
| Marginal worker cost under subscription | $0 |
| Orchestrator | Claude Opus; approximately $35 **estimated** spend |

**API-equivalent worker costs are reconstructed from recorded token counts at
published rates, not billed.** Using the operator-supplied
[OpenAI standard rates for gpt-6.1-sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
of $2/M uncached input tokens, $0.10/M cached input tokens and $10/M output tokens:

- **Whole run (36 worker sessions):** approximately $25 reconstructed from
  approximately 3.82M uncached input, 124.3M cached input and 0.477M output tokens.
  The supplied approximate ceiling is $27 because cache writes and the
  reasoning-token split are not recorded.
- **ChronoGraph cards only:** approximately $14 reconstructed API-equivalent cost.
- **Without prompt caching:** approximately $261 reconstructed API-equivalent cost
  for the whole run.

These reconstructed costs are separate from the actual $0 marginal worker cost
under subscription and the estimated orchestrator spend. This publication verifies
kit integrity and portability; it does not reconstruct private session or billing
records, rerun the application or claim that a new participant implementation has
passed deployment acceptance.
