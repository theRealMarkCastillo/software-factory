# ChronoGraph — UI Contract (BINDING for C5)

Layout per master spec §5: header, graph canvas (left), Conflict & Lineage Inspector
(right), time-travel scrubber (bottom), ingestion drawer.

## Stable selectors (`data-testid`)

| testid | Element | Required attributes / behaviour |
|---|---|---|
| `app-title` | header text | contains "ChronoGraph" |
| `seed-button` | "Load Multi-Episode Conflict Demo" | POST /api/demo/seed, then reload data |
| `graph-canvas` | `<svg>` root | — |
| `graph-node` | one `<g>` per entity | `data-name="<entity>"` |
| `graph-edge` | one `<path>`/`<line>` per edge | `data-fact-id`, `data-predicate`, `data-source`, `data-target`, `data-status` (`active` / `superseded`, the status at as_of) |
| `scrubber` | `<input type="range">` | min = min(valid_from) ms, max = max(observed_at) ms, default = max |
| `scrubber-label` | text | current as_of as ISO 8601 UTC with `Z` |
| `play-button` | button | steps through the distinct episode timestamps in order |
| `inspector` | panel | — |
| `inspector-empty` | text | shown when nothing is selected |
| `inspector-version` | one row per chain version | `data-version`, `data-object`, `data-status`, text includes valid_from -> valid_until (or NOW) and the episode id |
| `ingest-toggle` | opens the drawer | — |
| `ingest-title`, `ingest-observed-at`, `ingest-text` | inputs | observed-at is sent as ISO 8601 UTC `Z` |
| `ingest-submit` | button | POST /api/episodes |
| `ingest-result` | text | extracted / conflicts-resolved counts on 201 |
| `error-banner` | alert | API error text, including the 409 `out_of_order` and 422 codes |
| `empty-state` | shown when the ledger is empty | includes a seed call to action |
| `loading` | shown while a request is in flight | — |

## Behaviour

- The graph requests `/api/graph?as_of=<scrubber>&include_superseded=true`, debounced
  250 ms on scrubber input. Edges whose `status` is `superseded` render dashed.
- Styling: active stroke `#10B981` solid, opacity 1. Superseded stroke `#F59E0B`,
  `stroke-dasharray` set, opacity <= 0.6.
- Clicking an edge selects its (subject, predicate) chain. Clicking a node shows every
  chain where the node is the subject (from `/api/conflicts`), plus its single-version
  facts (from `/api/facts?status=all&entity=`).
- All API calls use relative `/api/...` (SC-13).
- After a successful seed or ingest, the scrubber range recomputes and the scrubber
  moves to max.
