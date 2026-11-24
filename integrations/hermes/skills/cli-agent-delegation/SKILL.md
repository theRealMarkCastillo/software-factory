---
name: cli-agent-delegation
description: Use when delegating a coding step to an agent CLI.
license: MIT
metadata:
  hermes:
    category: software-factory
---

# CLI Agent Delegation

Delegate a bounded coding step, not a card. This skill describes the protocol;
it does not install CLIs, grant permissions, or enforce sandbox behavior.

## Ownership

The claiming worker owns the card from claim through worktree, RED/GREEN
commits, gates, evidence, and handoff. The CLI is a leaf: it never touches the
board, commits, pushes, merges, or deploys. It must not delegate further or
bypass the factory's existing execution and approval boundaries.

## Installation inputs

Before delegating, confirm the installation's role supplements supply CLI paths,
routing defaults, run caps, and the single-run lock location. Use the configured
model pin, preflight, and lock procedure; do not invent missing host settings.
Confirm the CLI is available and the required restrictions are effective before
using it. Record the routing choice and reason.

## Step protocol

1. Write `HANDOFF.md` with the goal, permitted files, exact validation command,
   and non-goals. Capture the starting worktree state so you can attribute changes.
2. Run the CLI headless with `cwd` set to the card worktree, with a wall-clock
   timeout and a turn/budget cap supplied by the installation. Use the
   `terminal` tool with the card worktree as `workdir` and a bounded `timeout`,
   within the installation's single-run lock. Do not edit the worktree while the
   CLI runs, and do not allow a second CLI in that worktree.
3. Perform the mandatory transcript audit below. Inspect the diff and status,
   including untracked files. Reject changes outside the permitted files. Run
   the validation command yourself, then commit only the intended changes if
   the expected result is verified and the card authorizes a commit. CLI
   self-report is never evidence.

## Two-phase TDD

1. Delegate a tests-only step. The worker verifies the expected failure and
   commits RED. Do not begin implementation until that tests-only commit exists.
2. Delegate the implementation step. The worker verifies the tests pass and
   commits GREEN. Commit only after the card's gates pass.

## CLI command shapes

Substitute the configured CLI path, model pin and caps where applicable. Replace
`<brief>` with a short instruction to read `HANDOFF.md` by path, not the file's
contents. Quote values as single arguments; placeholders are not shell syntax.

| CLI | Headless command shape | Write permission |
| --- | --- | --- |
| Codex | `codex exec -s workspace-write -m <pinned model> "<brief>"` | Sandbox scoped to cwd |
| Claude Code | `claude -p "<brief>" --permission-mode dontAsk --permission-prompts none --tools Read,Edit,Write,Bash --allowedTools "Read,Edit(<permitted file>)...,Write(<permitted file>)...,Bash(<exact validation cmd>)" --max-turns <n> --output-format json --strict-mcp-config` | Listed files only; exact validation command plus Claude Code's built-in read-only commands; argv is not proof |
| Antigravity | `agy -p "<brief>" --mode accept-edits --sandbox --print-timeout <t> --output-format json` | Edits; terminal restricted |

For Claude Code, replace the repeated placeholders with one `Edit(...)` and one
`Write(...)` entry per permitted file. Use one Bash entry containing the exact
validation command from the brief, with no wildcard. Claude Code admits its
built-in read-only commands regardless of allow rules. Containment relies on
scoped write rules, the transcript audit, and the profile's own diff check,
not on the Bash allow rule alone.

Verify effective permissions: a tool allowlist is not proof that inherited
settings or extensions cannot grant additional access. Do not delegate if the
installation cannot enforce the stated restrictions. For Codex, include `--json`
when capturing machine-readable events and keep the explicit model pin.

QA may use a CLI only read-only, as an aid within its own independent review.
Apply the same wall-clock timeout and caps; do not permit writes, even if an
inherited setting would allow them.

- Codex: `codex exec -s read-only -m <pinned model> "<brief>"`.
- Claude Code: `claude -p "<brief>" --tools Read --allowedTools Read --permission-mode plan --max-turns <n> --output-format json`.
- Antigravity: `agy -p "<brief>" --mode plan --sandbox --print-timeout <t> --output-format json`.

Never use `--dangerously-skip-permissions`.
Never use `--dangerously-bypass-approvals-and-sandbox`.
Never use `bypassPermissions`.

## Transcript audit (all CLIs, mandatory)

Before accepting any delegated step, the profile must audit actual tool calls
against the brief. Parse the CLI transcript or JSON that records the tool calls;
a final result summary is not a substitute. Independently check the worktree
diff and status against the starting state, including untracked files.

Reject the step if:

- any shell call is neither the exact validation command nor a read-only
  inspection command with no redirection or chaining;
- any write or edit targets a path outside the permitted files; or
- the worktree diff touches any unpermitted path.

Compare the full shell call, not just its first command: a pipe or redirection
appended to the validation command is not an exact match. Read-only inspection
must be a standalone command, not a pipeline. Check the actual target of each
write or edit against the permitted paths. A permission denial does not make an
out-of-scope call acceptable.

On rejection, discard the CLI's edits without disturbing pre-existing changes,
then fall back or block. If the CLI exposes no usable transcript of its tool
calls, the step is unaudited and must use the fallback; do not retain its edits.

## Evidence

For each invocation, write `cli-runs/<n>.json` under the card's artifacts
directory. Record CLI name and version, recorded model, full argv (brief by file
path), exit code, duration, session or conversation ID, usage and cost if
reported, transcript path, files changed, the worker's own validation command
and result, and `cli_fallback` when applicable. Mark unavailable fields as such;
do not infer them or embed credentials or the brief's contents in the record.
Record `transcript_audit: pass|fail` and list the offending calls and any
unpermitted paths found by the diff check. Record an unaudited step as `fail`
with the reason, not as a pass.

Populate each per-invocation record's `model` field from the CLI's own JSON
(Claude Code: `modelUsage` keys; Codex: the rollout's `turn_context` model);
never leave `model` null when the CLI ran. If that source does not establish the
model, preserve the raw output, record the evidence failure, and use the
fallback rather than guessing or accepting the invocation.

Source model claims only from CLI machine-readable output or the profile session
store; never ask the model to identify itself. A configured pin or a model name
in the response text is not proof of what ran.

## Fallback

If the CLI is unavailable or fails, use the claiming profile's native worker
loop. Record `cli_fallback` with the reason. Inspect any partial CLI changes
before continuing; the same scope, TDD, and validation rules still apply. Add
the native model that actually ran to the implementation vendor set.

Block with kind `capability` only if no route remains or no reviewer vendor is
available.

## Cross-vendor review

1. Build the implementation vendor set from the models that actually ran: use
   every CLI run record and every native session record, including provider
   fallbacks. Resolve vendors from recorded model and provider identities, not
   the routing plan. A fallback vendor counts separately from the primary vendor.
2. The reviewer's vendor must be outside that set. At staging, the orchestrator
   pins the QA card's provider and model. This is a plan, not proof of the actual
   reviewer vendor.
3. QA preflight compares its own recorded model's vendor with the implementation
   vendor set. On overlap, block with kind `needs_input` and reason
   "reviewer vendor conflict" so the orchestrator can re-pin the review. Do not
   proceed with a reviewer whose independence cannot be established.
4. The verdict records the reviewer model that actually ran, including any
   fallback. Recheck the vendor boundary if the reviewer model/provider changes.
