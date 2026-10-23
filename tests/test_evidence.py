import json
import subprocess
from pathlib import Path

import pytest

from factory.evidence import (
    GATE_POLICY,
    MANIFEST_FILENAME,
    REQUIRED_ARTIFACTS,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    SIDECAR_FILENAME,
    EvidenceError,
    artifact_records,
    assert_assembled_evidence,
    check_evidence,
    engine_identity,
    gate_policy_violations,
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


# ---------------------------------------------------------------------------
# schema v2: producer identity, code-owned gate policy, strict checker
# ---------------------------------------------------------------------------

CANDIDATE = "a" * 40
BASE_SHA = "b" * 40


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _clean_runtime() -> dict:
    return {
        "teardown": {"attempted": True, "ok": True, "detail": ""},
        "safety": {
            "postflight": "clean",
            "protected_changes": [],
            "unexpected_changes": [],
            "collateral_damage": False,
        },
    }


def _log_dir(tmp_path: Path) -> Path:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in REQUIRED_ARTIFACTS:
        (artifacts / name).write_text(f"{name}: ran\n")
    return artifacts


def _write_v2(tmp_path: Path, repo: Path, artifacts_dir: Path, **overrides):
    head = _head(repo)
    params = {
        "entry_point": "docker compose test service",
        "command": ["docker", "compose", "up", "--build"],
        "mode": "acceptance",
        "source_start": {"sha": head, "status_porcelain": ""},
        "source_end": {"sha": head, "status_porcelain": ""},
        "base": {"ref": "main", "kind": "commit", "sha": BASE_SHA},
        "runtime": _clean_runtime(),
        "artifacts": list(REQUIRED_ARTIFACTS),
        "audit": {"status": "pass", "passed": True},
        "coverage": {
            "status": "pass",
            "passed": True,
            "base": "main",
            "base_sha": BASE_SHA,
            "base_kind": "commit",
        },
        "datamodel": {"status": "skipped", "reason": "no_data_model"},
        "execution": {"exit_code": 0, "passed": True, "stage": "complete"},
        "fixture_paths": ["fixture.json"],
        "gates_run": True,
    }
    params.update(overrides)
    path = write_evidence_manifest(artifacts_dir, repo, **params)
    return path, json.loads(path.read_text())


def test_manifest_writes_schema_identity_mode_and_checksum_sidecar(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    path, manifest = _write_v2(tmp_path, repo, artifacts)

    assert manifest["schema"] == {"name": SCHEMA_NAME, "version": SCHEMA_VERSION}
    assert manifest["mode"] == "acceptance"
    assert path.name == MANIFEST_FILENAME

    import hashlib

    sidecar = (artifacts / SIDECAR_FILENAME).read_text()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert sidecar == f"{digest}  {MANIFEST_FILENAME}\n"


def test_manifest_preserves_legacy_reader_keys(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    _, manifest = _write_v2(tmp_path, repo, artifacts)

    # Every legacy top-level key keeps its prior name and meaning.
    assert manifest["git_revision"] == _head(repo)
    assert manifest["git_status_porcelain"] == ""
    assert manifest["entry_point"] == "docker compose test service"
    assert manifest["command"][:3] == ["docker", "compose", "up"]
    assert manifest["gates_run"] is True
    assert manifest["audit"]["status"] == "pass"
    assert manifest["coverage"]["status"] == "pass"
    assert manifest["datamodel"] == {"status": "skipped", "reason": "no_data_model"}
    assert manifest["environment"]["example_env"]["present"] is True
    assert manifest["fixtures"][0]["path"] == "fixture.json"
    assert manifest["fixtures"][0]["tracked"] is True
    assert manifest["execution"]["stage"] == "complete"
    assert isinstance(manifest["hermes_factory_card"], str)


def test_manifest_records_engine_identity_and_fingerprint(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    _, manifest = _write_v2(tmp_path, repo, artifacts)

    engine = manifest["engine"]
    assert engine == engine_identity()
    assert engine["name"] == "hermes-software-factory"
    assert engine["fingerprint"].startswith("sha256:")
    assert len(engine["fingerprint"]) == len("sha256:") + 64
    # Deterministic across calls: the verifier recomputes exactly this value.
    assert engine_identity() == engine_identity()


def test_manifest_records_source_start_end_base_and_artifacts(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    head = _head(repo)
    _, manifest = _write_v2(tmp_path, repo, artifacts)

    assert manifest["source"]["sha"] == head
    assert manifest["source"]["clean"] is True
    assert manifest["source"]["start"] == {"sha": head, "status_porcelain": ""}
    assert manifest["source"]["end"] == manifest["source"]["start"]
    assert manifest["base"] == {"ref": "main", "kind": "commit", "sha": BASE_SHA}
    assert manifest["coverage"]["base_sha"] == BASE_SHA
    names = [record["name"] for record in manifest["artifacts"]]
    assert sorted(names) == sorted(REQUIRED_ARTIFACTS)
    for record in manifest["artifacts"]:
        assert record["bytes"] == (artifacts / record["name"]).stat().st_size
        assert len(record["sha256"]) == 64


def test_manifest_records_dirty_or_changed_source_as_not_clean(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    head = _head(repo)
    _, dirty = _write_v2(
        tmp_path,
        repo,
        artifacts,
        source_end={"sha": head, "status_porcelain": " M app.py"},
    )
    assert dirty["source"]["clean"] is False

    other = "c" * 40
    _, changed = _write_v2(
        tmp_path,
        repo,
        artifacts,
        source_end={"sha": other, "status_porcelain": ""},
    )
    assert changed["source"]["clean"] is False
    assert changed["source"]["end"]["sha"] == other


def test_manifest_rejects_artifact_path_escape(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    (tmp_path / "outside.log").write_text("secret\n")
    with pytest.raises(EvidenceError, match="artifact name"):
        _write_v2(tmp_path, repo, artifacts, artifacts=["../outside.log"])


def test_manifest_rejects_acceptance_mode_with_gates_disabled(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    with pytest.raises(EvidenceError, match="gates"):
        _write_v2(tmp_path, repo, artifacts, mode="acceptance", gates_run=False)


def test_manifest_defaults_to_diagnostic_when_gates_are_disabled(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    _, manifest = _write_v2(tmp_path, repo, artifacts, mode=None, gates_run=False)
    assert manifest["mode"] == "diagnostic"
    assert manifest["gates_run"] is False


def test_artifact_records_reject_missing_and_escaping_names(tmp_path):
    artifacts = _log_dir(tmp_path)
    records = artifact_records(artifacts)
    assert [r["name"] for r in records] == list(REQUIRED_ARTIFACTS)
    with pytest.raises(EvidenceError, match="artifact"):
        artifact_records(artifacts, ["missing.log"])
    with pytest.raises(EvidenceError, match="artifact name"):
        artifact_records(artifacts, ["/etc/passwd"])


def test_gate_policy_is_code_owned_and_closed():
    """One table, code-owned: a manifest cannot add or relax a gate."""
    assert set(GATE_POLICY) == {"audit", "coverage", "datamodel"}
    assert GATE_POLICY["audit"]["required"] == {"status": "pass"}
    assert set(GATE_POLICY["audit"]["inapplicable"]) == {"no_manifest"}
    assert GATE_POLICY["coverage"]["required"] == {"status": "pass", "passed": True}
    assert set(GATE_POLICY["coverage"]["inapplicable"]) == {"skipped:no_added_python"}
    assert set(GATE_POLICY["datamodel"]["inapplicable"]) == {"skipped:no_data_model"}


def _passing_gates() -> dict:
    return {
        "audit": {"status": "pass", "passed": True},
        "coverage": {"status": "pass", "passed": True},
        "datamodel": {"status": "skipped", "reason": "no_data_model"},
    }


@pytest.mark.parametrize(
    ("gate", "value", "reason"),
    [
        ("audit", {"status": "fail", "passed": False}, "gate_failed:audit"),
        ("audit", {"status": "error", "passed": False}, "gate_failed:audit"),
        ("audit", {"status": "unavailable"}, "gate_unavailable:audit"),
        ("audit", {"status": "unsupported"}, "gate_unavailable:audit"),
        ("coverage", {"status": "pass", "passed": False}, "gate_failed:coverage"),
        ("coverage", {"status": "disabled"}, "gate_unavailable:coverage"),
        (
            "coverage",
            {"status": "skipped", "reason": "disabled"},
            "gate_not_applicable_unpermitted:coverage",
        ),
        (
            "datamodel",
            {"status": "skipped", "reason": "no_data_model", "passed": False},
            "gate_failed:datamodel",
        ),
        (
            "datamodel",
            {"status": "skipped", "reason": "not_configured"},
            "gate_not_applicable_unpermitted:datamodel",
        ),
        ("datamodel", {}, "gate_missing:datamodel"),
    ],
)
def test_gate_policy_returns_named_violation(gate, value, reason):
    gates = _passing_gates()
    gates[gate] = value
    assert gate_policy_violations(gates) == [reason]


def test_gate_policy_accepts_documented_inapplicable_gates():
    gates = _passing_gates()
    gates["audit"] = {"status": "no_manifest"}
    gates["coverage"] = {"status": "skipped", "reason": "no_added_python"}
    assert gate_policy_violations(gates) == []


def test_check_evidence_accepts_a_complete_manifest(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    _, manifest = _write_v2(tmp_path, repo, artifacts)

    result = check_evidence(manifest, artifacts_dir=artifacts)
    assert result.ok is True, (result.reason, result.detail)
    assert result.reason == "ok"
    assert {check["name"] for check in result.checks} >= {
        "schema",
        "source",
        "execution",
        "runtime",
        "gates",
        "engine",
        "artifacts",
    }


def test_check_evidence_rejects_legacy_schema(tmp_path):
    artifacts = _log_dir(tmp_path)
    result = check_evidence(_valid_manifest(), artifacts_dir=artifacts)
    assert result.ok is False
    assert result.reason == "legacy_schema_unsupported"


def test_check_evidence_rejects_diagnostic_mode(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    _, manifest = _write_v2(tmp_path, repo, artifacts)
    manifest["mode"] = "diagnostic"
    manifest["gates_run"] = False
    result = check_evidence(manifest, artifacts_dir=artifacts)
    assert result.reason == "mode_not_acceptance"

    manifest["mode"] = "acceptance"
    result = check_evidence(manifest, artifacts_dir=artifacts)
    assert result.reason == "gates_disabled"


def test_check_evidence_rejects_boolean_where_integer_is_required(tmp_path):
    repo = _repo(tmp_path)
    artifacts = _log_dir(tmp_path)
    _, manifest = _write_v2(tmp_path, repo, artifacts)
    manifest["execution"]["exit_code"] = True
    result = check_evidence(manifest, artifacts_dir=artifacts)
    assert result.reason == "malformed_manifest"
    assert "exit_code" in result.detail


def test_legacy_assert_assembled_evidence_is_not_the_new_verifier(tmp_path):
    """The old reader keeps its prior compatibility; it certifies nothing new."""
    legacy = {
        **_valid_manifest(),
        "git_revision": "abc",
        "entry_point": "MCP transport",
        "command": "pytest assembled",
        "environment": {"example_env": {"present": True}},
        "fixtures": [{"path": "fixture.json", "sha256": "x"}],
    }
    assert assert_assembled_evidence(legacy) is None

    artifacts = _log_dir(tmp_path)
    result = check_evidence(legacy, artifacts_dir=artifacts)
    assert result.ok is False
    assert result.reason == "legacy_schema_unsupported"
