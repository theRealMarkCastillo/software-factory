"""Agent-facing runbooks must not offer host-wide cleanup commands."""

import re
from pathlib import Path


def test_ops_skill_does_not_offer_executable_host_wide_cleanup():
    skill = (
        Path(__file__).resolve().parents[1]
        / "integrations/hermes/skills/software-factory-ops/SKILL.md"
    )
    text = skill.read_text()
    for command in re.findall(r"`([^`\n]+)`", text):
        # Prose may name forbidden operations, but must not hand agents
        # ready-to-run pruning commands with automatic confirmation flags.
        assert not re.search(
            r"docker\s+(?:builder|image|system|volume|network|container)\s+prune\b.*(?:-f|--force)",
            command,
        )
