import json
import subprocess
from pathlib import Path

import pytest

from factory.evidence import (
    EvidenceError,
    assert_assembled_evidence,
    write_evidence_manifest,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".env.example").write_text("REQUIRE_TLS=false\n")
    (repo / ".env").write_text("REQUIRE_TLS=false\n")
    (repo / "fixture.json").write_text('{"case":"real"}\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def test_manifest_records_revision_environment_entry_point_and_fixture_hash(tmp_path):
    repo = _repo(tmp_path)
    output = write_evidence_manifest(
        tmp_path / "artifacts",
        repo,
        entry_point="POST /verify_claims via MCP transport",
        command=["pytest", "tests/test_assembled.py"],
        fixture_paths=["fixture.json"],
    )
    manifest = json.loads(output.read_text())
    assert manifest["git_revision"]
    assert manifest["entry_point"].startswith("POST")
    assert manifest["environment"]["example_env"]["present"] is True
    assert manifest["fixtures"][0]["path"] == "fixture.json"
    assert len(manifest["fixtures"][0]["sha256"]) == 64


def test_manifest_records_execution_result(tmp_path):
    repo = _repo(tmp_path)
    output = write_evidence_manifest(
        tmp_path / "artifacts",
        repo,
        entry_point="MCP",
        command="pytest",
        execution={"exit_code": 0, "passed": True, "stage": "complete"},
    )
    manifest = json.loads(output.read_text())
    assert manifest["execution"] == {
        "exit_code": 0,
        "passed": True,
        "stage": "complete",
    }


def test_assembled_evidence_rejects_missing_call_path_and_environment():
    with pytest.raises(EvidenceError):
        assert_assembled_evidence({"git_revision": "abc", "command": "pytest"})


def test_assembled_evidence_rejects_marker_only_anti_story():
    with pytest.raises(EvidenceError):
        assert_assembled_evidence(
            {
                "git_revision": "abc",
                "entry_point": "MCP transport",
                "command": "pytest",
                "environment": {"example_env": {"present": True}},
                "anti_stories": ["A2"],
                "fixtures": [],
            }
        )


def test_assembled_evidence_accepts_complete_manifest():
    assert (
        assert_assembled_evidence(
            {
                **_valid_manifest(),
                "git_revision": "abc",
                "entry_point": "MCP transport",
                "command": "pytest assembled",
                "environment": {"example_env": {"present": True}},
                "fixtures": [{"path": "fixture.json", "sha256": "x"}],
            }
        )
        is None
    )


def test_fixture_outside_repo_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(EvidenceError):
        write_evidence_manifest(
            tmp_path / "artifacts",
            repo,
            entry_point="MCP",
            command="pytest",
            fixture_paths=["missing.json"],
        )


def test_fixture_path_escape_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("secret\n")
    with pytest.raises(EvidenceError, match="escapes repository"):
        write_evidence_manifest(
            tmp_path / "artifacts",
            repo,
            entry_point="MCP",
            command="pytest",
            fixture_paths=["../outside.json"],
        )


def test_manifest_does_not_copy_environment_values(tmp_path):
    repo = _repo(tmp_path)
    output = write_evidence_manifest(
        tmp_path / "artifacts", repo, entry_point="MCP", command="pytest"
    )
    text = output.read_text()
    assert "REQUIRE_TLS" not in text
    assert "false" not in text


def test_manifest_records_fixture_hash_change(tmp_path):
    repo = _repo(tmp_path)
    first = json.loads(
        write_evidence_manifest(
            tmp_path / "a",
            repo,
            entry_point="MCP",
            command="pytest",
            fixture_paths=["fixture.json"],
        ).read_text()
    )
    (repo / "fixture.json").write_text('{"case":"weakened"}\n')
    second = json.loads(
        write_evidence_manifest(
            tmp_path / "b",
            repo,
            entry_point="MCP",
            command="pytest",
            fixture_paths=["fixture.json"],
        ).read_text()
    )
    assert first["fixtures"][0]["sha256"] != second["fixtures"][0]["sha256"]


def _valid_manifest():
    return {
        "git_revision": "abc",
        "entry_point": "test",
        "command": ["pytest"],
        "environment": {"example_env": {"present": True}},
        "execution": {"exit_code": 0, "passed": True, "stage": "complete"},
        "gates_run": True,
        "audit": {"status": "pass"},
        "coverage": {"status": "pass", "passed": True},
        "datamodel": {"status": "skipped", "reason": "no_data_model"},
    }


@pytest.mark.parametrize(
    "replacement",
    [
        {"execution": {}},
        {
            "execution": {
                "exit_code": 1,
                "passed": False,
                "stage": "complete_with_errors",
            }
        },
        {
            "execution": {
                "exit_code": 0,
                "passed": False,
                "stage": "complete_with_errors",
            }
        },
        {"gates_run": False},
        {"gates_run": None},
        {"audit": {"status": "fail"}},
        {"audit": {}},
        {"audit": {"status": "unavailable"}},
        {"coverage": {"status": "fail", "passed": False}},
        {"coverage": {}},
        {"datamodel": {"status": "fail"}},
        {"datamodel": {}},
        {"coverage": {"status": "skipped", "reason": "disabled"}},
    ],
)
def test_assembled_evidence_rejects_failed_or_incomplete_gates(replacement):
    manifest = {**_valid_manifest(), **replacement}
    with pytest.raises(EvidenceError):
        assert_assembled_evidence(manifest)


def test_assembled_evidence_allows_explicit_not_applicable_gates():
    manifest = _valid_manifest()
    manifest["audit"] = {"status": "no_manifest"}
    manifest["coverage"] = {"status": "skipped", "reason": "no_added_python"}
    assert_assembled_evidence(manifest)


@pytest.mark.parametrize("section", ["execution", "audit", "coverage", "datamodel"])
@pytest.mark.parametrize("value", ["pass", [], {"status": "pass", "passed": False}])
def test_assembled_evidence_rejects_malformed_or_contradictory_results(section, value):
    manifest = _valid_manifest()
    manifest[section] = value
    with pytest.raises(EvidenceError):
        assert_assembled_evidence(manifest)


# The fixture is intentionally a real git-tracked file: the manifest hashes it
# so a later acceptance step can compare before/after state instead of trusting
# a green test result.
