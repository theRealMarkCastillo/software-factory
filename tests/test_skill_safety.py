"""Agent-facing runbooks must not offer host-wide cleanup commands."""

import re
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[1]
    / "integrations/hermes/skills/software-factory-ops/SKILL.md"
)


def test_ops_skill_does_not_offer_executable_host_wide_cleanup():
    text = SKILL.read_text()
    for command in re.findall(r"`([^`\n]+)`", text):
        # Prose may name forbidden operations, but must not hand agents
        # ready-to-run pruning commands with automatic confirmation flags.
        assert not re.search(
            r"docker\s+(?:builder|image|system|volume|network|container)\s+prune\b.*(?:-f|--force)",
            command,
        )


def test_ops_skill_scopes_direct_commands_to_standalone_installations():
    """The portable skill ships to third parties, so its direct CLI commands
    must be labelled standalone instructions and must defer to an operator's
    private launcher where one exists."""
    text = SKILL.read_text()
    heading = text.index("## Standalone installation")
    assert "private launcher" in text.lower()
    assert "supersedes" in text.lower()
    for command in (
        "uv run --locked factory-run --help",
        "python -m factory.runner --help",
    ):
        assert text.index(command) > heading
