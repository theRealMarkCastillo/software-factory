# LangGraph hybrid: design and acceptance records

These documents record how the LangGraph orchestration lane was designed,
accepted and repaired.

Raw run records (worker and reviewer transcripts, request and receipt JSON,
shell snapshots, captured test output) are not published. They carry
host-specific paths, so references below to `evidence/...` name files that
stay on the operator's machine. The acceptance scripts still write them to
`docs/langgraph-hybrid/evidence/`, which is git-ignored.

- `ownership-and-design.md`: ownership boundaries and design.
- `operator-runbook.md`: how to run, inspect and recover the lane.
- `reviewer-sandbox.md`: the sandboxed reviewer.
- `g0-capabilities.md`, `acceptance-2026-11-08.md`, `rollout-2026-11-09.md`,
  `repair-2026-11-10.md`, `owner-handoff.md`: dated acceptance and rollout records.
