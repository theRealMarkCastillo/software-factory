"""Public greenfield guidance is an installable, privacy-safe contract."""

import getpass
import os
import re
import socket
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "integrations/hermes/skills"
REFERENCE = "software-factory-ops/references/greenfield-compose-chains.md"


def skill(name):
    return (SKILLS / name / "SKILL.md").read_text()


def test_intake_requires_owner_ratified_pre_build_consistency_audit():
    text = skill("factory-spec-intake")
    for phrase in (
        "mandatory pre-build spec-consistency audit",
        "findings list",
        "owner ratifies",
        "before the first build card",
        "ordered write protocols",
        "unique/partial indexes",
        "foreign keys",
        "deferrability",
        "CHECKs",
        "triggers",
        "legal state transitions",
        "frozen inputs",
        "fixtures",
        "contracts",
        "checksums",
        "test image",
        "first card",
        "bind interface",
        "loopback",
        "acceptance owner",
        "real-execution step",
        "tests-only RED commit",
        "every implementer card",
    ):
        assert phrase in text, phrase


def test_chain_requires_final_operator_smoke_of_real_lifecycle():
    reference = SKILLS / REFERENCE
    assert reference.is_file(), "public chain template must ship with the skill"
    text = reference.read_text()
    for phrase in (
        "final operator-smoke card",
        "actual developer lifecycle",
        "on the host end to end",
        "not a fake docker",
        "every listener",
        "loopback-only",
        "LAN-address connect is refused",
        "UI evidence",
        "documented state",
        "empty stores",
        "seeded",
        "acceptance owner",
        "tests-only RED commit",
    ):
        assert phrase in text, phrase
    assert "references/greenfield-compose-chains.md" in skill("software-factory-ops")
    assert "software-factory-ops/references/greenfield-compose-chains.md" in skill(
        "factory-spec-intake"
    )
    assert REFERENCE in (ROOT / "docs/skill-inventory.md").read_text()


def test_qc_marks_fake_docker_only_lifecycle_evidence_unexercised():
    text = skill("factory-qc-gating")
    assert re.search(r"fake-docker-only.*UNEXERCISED", text)
    assert "never PASS" in text
    assert "actual developer lifecycle" in text
    assert "LAN-address" in text


def test_dispatch_defaults_to_wake_only_and_limits_human_delivery():
    text = skill("hermes-kanban-dispatch")
    assert "orchestrator-created cards default to wake-only subscriptions" in text
    assert "only owner decisions and chain completion" in text
    assert "read back" in text


def privacy_violations(text):
    """Reject local identity plus deployment-provided literal names/hostnames.

    Keep private values out of this public test source and diagnostic output.
    Additional personal names belong in FACTORY_LEAK_DENYLIST, not the bundle.
    """
    literals = {getpass.getuser(), socket.gethostname(), str(Path.home())}
    denylist = os.environ.get("FACTORY_LEAK_DENYLIST")
    if denylist:
        literals.update(
            line.strip()
            for line in Path(denylist).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    patterns = [
        re.escape(str(Path("/", "Users")) + "/"),
        r"\b[\w-]+\.(?:local|internal)\b",
        r"\b192[.]168[.]\d+[.]\d+\b",
        *(re.escape(value) for value in literals if value),
    ]
    return bool(re.search("|".join(patterns), text, re.IGNORECASE))


def test_public_skill_tree_has_no_private_identity():
    violations = [
        str(path.relative_to(SKILLS))
        for path in SKILLS.rglob("*")
        if path.is_file() and privacy_violations(path.read_text())
    ]
    assert not violations, violations


@pytest.mark.parametrize(
    "value",
    [
        str(Path("/", "Users", "example", "repo")),
        "example-host.internal",
        "Example Person",
        "example-private-host",
    ],
)
def test_privacy_contract_rejects_paths_hostnames_and_configured_personal_names(
    tmp_path, monkeypatch, value
):
    denylist = tmp_path / "private-identifiers.txt"
    denylist.write_text("Example Person\nexample-private-host\n")
    monkeypatch.setenv("FACTORY_LEAK_DENYLIST", str(denylist))
    assert privacy_violations(value)


def test_all_public_skills_keep_frontmatter_and_content_limits():
    for path in SKILLS.glob("*/SKILL.md"):
        text = path.read_text()
        assert text.startswith("---\n"), path
        frontmatter, body = text[4:].split("\n---\n", 1)
        metadata = yaml.safe_load(frontmatter)
        assert metadata["name"] == path.parent.name
        assert isinstance(metadata["description"], str)
        assert 0 < len(metadata["description"]) <= 1024
        assert body.strip()
        assert len(text) <= 100_000


def test_installer_copies_public_chain_reference(tmp_path):
    from scripts.install_hermes_skills import install

    destination = tmp_path / "skills"
    install(destination, apply=True)
    assert (destination / REFERENCE).is_file()
    assert (destination / REFERENCE).read_bytes() == (SKILLS / REFERENCE).read_bytes()
