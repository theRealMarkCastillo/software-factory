"""Contract checks for the shipped delegation runbook, not live CLI probes."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = "cli-agent-delegation"
SKILLS = ROOT / "integrations/hermes/skills"
SKILL = SKILLS / NAME / "SKILL.md"


def _read_skill():
    assert SKILL.is_file(), f"Curated skill missing: {NAME}"
    return SKILL.read_text(encoding="utf-8")


def _section(title):
    heading = f"\n## {title}\n"
    text = _read_skill()
    assert heading in text, f"Missing contract section: {title}"
    return " ".join(text.split(heading, 1)[1].split("\n## ", 1)[0].split())


def test_frontmatter_matches_public_skill_shape():
    opening, header, body = _read_skill().split("---\n", 2)
    assert not opening and body.strip()
    metadata = yaml.safe_load(header)
    description = metadata.pop("description")
    assert description.startswith("Use when ") and len(description) <= 60
    assert metadata == {
        "name": NAME,
        "license": "MIT",
        "metadata": {"hermes": {"category": "software-factory"}},
    }


def test_claiming_worker_owns_card_and_cli_has_no_lifecycle_authority():
    ownership = _section("Ownership")
    assert "The claiming worker owns the card" in ownership
    assert (
        "The CLI is a leaf: it never touches the board, commits, pushes, merges, or deploys."
        in ownership
    )


def test_worker_bounds_the_run_and_independently_validates_its_diff():
    procedure = _section("Step protocol")
    brief = procedure.index(
        "Write `HANDOFF.md` with the goal, permitted files, exact validation command, and non-goals."
    )
    invoke = procedure.index("Run the CLI headless with `cwd` set to the card worktree")
    assert "with a wall-clock timeout and a turn/budget cap" in procedure
    assert "Do not edit the worktree while the CLI runs" in procedure
    inspect = procedure.index("Reject changes outside the permitted files")
    validate = procedure.index("Run the validation command yourself, then commit only")
    assert brief < invoke < inspect < validate
    assert "CLI self-report is never evidence" in procedure


def test_tdd_requires_worker_verified_red_before_green():
    tdd = _section("Two-phase TDD")
    red = tdd.index(
        "Delegate a tests-only step. The worker verifies the expected failure and commits RED."
    )
    green = tdd.index(
        "Delegate the implementation step. The worker verifies the tests pass and commits GREEN."
    )
    assert red < green


@pytest.mark.parametrize(
    ("command", "permission"),
    [
        (
            'codex exec -s workspace-write -m <pinned model> "<brief>"',
            "Sandbox scoped to cwd",
        ),
        (
            'claude -p "<brief>" --permission-mode dontAsk '
            "--permission-prompts none --tools Read,Edit,Write,Bash "
            '--allowedTools "Read,Edit(<permitted file>)...,Write(<permitted file>)...,'
            'Bash(<exact validation cmd>)" --max-turns <n> --output-format json '
            "--strict-mcp-config",
            "Listed files only; exact validation command plus Claude Code's built-in "
            "read-only commands; argv is not proof",
        ),
        (
            'agy -p "<brief>" --mode accept-edits --sandbox --print-timeout <t> '
            "--output-format json",
            "Edits; terminal restricted",
        ),
    ],
)
def test_write_command_is_paired_with_its_permission_boundary(command, permission):
    assert f"`{command}` | {permission} |" in _section("CLI command shapes")


def test_claude_implementation_has_no_bash_wildcard():
    row = next(
        line
        for line in _read_skill().splitlines()
        if line.startswith("| Claude Code |")
    )
    assert not re.search(r"Bash\([^)]*\*[^)]*\)", row)


def test_profile_audits_transcript_and_diff_before_accepting_any_cli_step():
    audit = _section("Transcript audit (all CLIs, mandatory)")
    assert "Before accepting any delegated step" in audit
    assert "Parse the CLI transcript or JSON" in audit
    assert "Reject the step if" in audit
    assert (
        "any shell call is neither the exact validation command nor a read-only "
        "inspection command with no redirection or chaining" in audit
    )
    assert "any write or edit targets a path outside the permitted files" in audit
    assert "the worktree diff touches any unpermitted path" in audit
    assert "discard the CLI's edits" in audit
    assert "fall back or block" in audit
    assert "unaudited and must use the fallback" in audit


def test_transcript_audit_result_and_offending_calls_are_invocation_evidence():
    evidence = _section("Evidence")
    assert "`transcript_audit: pass|fail`" in evidence
    assert "list the offending calls" in evidence


def test_reviewer_cli_is_read_only_for_each_vendor():
    review = _section("CLI command shapes")
    assert "QA may use a CLI only read-only" in review
    assert '`codex exec -s read-only -m <pinned model> "<brief>"`' in review
    assert 'claude -p "<brief>" --tools Read --allowedTools Read' in review
    assert 'agy -p "<brief>" --mode plan --sandbox' in review


def test_permission_bypasses_appear_only_in_explicit_prohibitions():
    banned = (
        "--dangerously-skip-permissions",
        "--dangerously-bypass-approvals-and-sandbox",
        "bypassPermissions",
    )
    lines = _read_skill().splitlines()
    for flag in banned:
        prohibition = f"Never use `{flag}`."
        matches = [line.strip() for line in lines if flag in line]
        assert matches and all(line == prohibition for line in matches), flag


def test_every_invocation_has_worker_owned_evidence():
    evidence = _section("Evidence")
    assert "For each invocation, write `cli-runs/<n>.json`" in evidence
    assert (
        "CLI name and version, recorded model, full argv (brief by file path)"
        in evidence
    )
    assert "exit code, duration, session or conversation ID" in evidence
    assert "usage and cost if reported, transcript path, files changed" in evidence
    assert "the worker's own validation command and result" in evidence
    assert "`cli_fallback` when applicable" in evidence


def test_model_identity_comes_from_records_not_self_report():
    evidence = _section("Evidence")
    assert (
        "Source model claims only from CLI machine-readable output or the profile session store"
        in evidence
    )
    assert "never ask the model to identify itself" in evidence


def test_unavailable_or_failed_cli_falls_back_with_reason_before_blocking():
    fallback = _section("Fallback")
    assert (
        "If the CLI is unavailable or fails, use the claiming profile's native worker loop"
        in fallback
    )
    assert "Record `cli_fallback` with the reason" in fallback
    assert (
        "Block with kind `capability` only if no route remains or no reviewer vendor is available."
        in fallback
    )


def test_review_preflight_enforces_disjoint_recorded_vendor_sets():
    review = _section("Cross-vendor review")
    assert (
        "Build the implementation vendor set from the models that actually ran"
        in review
    )
    assert (
        "every CLI run record and every native session record, including provider fallbacks"
        in review
    )
    assert "The reviewer's vendor must be outside that set" in review
    assert (
        "At staging, the orchestrator pins the QA card's provider and model" in review
    )
    assert (
        "QA preflight compares its own recorded model's vendor with the implementation vendor set"
        in review
    )
    assert (
        'On overlap, block with kind `needs_input` and reason "reviewer vendor conflict"'
        in review
    )
    assert "The verdict records the reviewer model that actually ran" in review


def test_installation_owns_host_inputs_and_skill_has_no_host_paths():
    installation = _section("Installation inputs")
    assert (
        "the installation's role supplements supply CLI paths, routing defaults, run caps, "
        "and the single-run lock location" in installation
    )
    text = _read_skill()
    # Match rooted paths, not relative paths such as cli-runs/<n>.json.
    assert not re.search(r"(?<![\w/])(?:~?/|[A-Za-z]:[\\/])[\w.~-]+", text)


def test_current_bundle_inventory_lists_every_skill():
    text = (ROOT / "docs/skill-inventory.md").read_text(encoding="utf-8")
    names = set(re.findall(r"^\| ([a-z]+(?:-[a-z]+)+) \|", text, re.MULTILINE))
    assert NAME in names
    assert names == {path.parent.name for path in SKILLS.glob("*/SKILL.md")}


def test_install_guide_does_not_freeze_bundle_size():
    text = (ROOT / "docs/hermes.md").read_text(encoding="utf-8")
    assert "To copy the curated skills:" in text
