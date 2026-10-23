import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_hermes_skills.py"


def _run(destination, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--destination", str(destination), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_does_not_create_destination(tmp_path):
    destination = tmp_path / "skills"
    result = _run(destination)
    assert result.returncode == 0
    assert "Would install" in result.stdout
    assert not destination.exists()


def test_installs_only_curated_skills_and_refuses_overwrite(tmp_path):
    destination = tmp_path / "skills"
    result = _run(destination, "--apply")
    assert result.returncode == 0
    names = {path.name for path in destination.iterdir()}
    assert names == {
        "factory-spec-intake",
        "software-factory-ops",
        "factory-qc-gating",
        "worker-software-engineer",
        "hermes-kanban-dispatch",
        "integration-test-verification",
        "one-verifiable-change",
        "agent-skill-engine-boundary",
    }
    marker = destination / "worker-software-engineer/SKILL.md"
    marker.write_text("local customization")
    assert _run(destination, "--apply").returncode == 1
    assert marker.read_text() == "local customization"


def test_conflict_preflight_copies_nothing(tmp_path):
    destination = tmp_path / "skills"
    (destination / "worker-software-engineer").mkdir(parents=True)
    assert _run(destination, "--apply").returncode == 1
    assert list(destination.iterdir()) == [destination / "worker-software-engineer"]
