"""Regression coverage for frozen verifier CLI boundary edge cases."""

import json
import os
import subprocess
import sys

import pytest
from factory_orch_fixtures import commit, git, make_evidence, refresh_sidecar, seed

from factory import evidence
from factory_orchestration.adapters.gates import InstalledLauncherGate


@pytest.mark.parametrize("modified", [False, True])
def test_launcher_leading_dash_path_reaches_module_parser(tmp_path, modified):
    s = seed(tmp_path)
    workspace = s["workspace"]
    (workspace / "-guard").write_text("frozen\n")
    base = commit(workspace, "base with leading dash")
    (workspace / ("-guard" if modified else "app.py")).write_text("changed\n")
    candidate = commit(workspace, "candidate")
    make_evidence(s["artifacts"], workspace, base, candidate)
    # An offline launcher dispatcher, not a mocked subprocess or parser.
    launcher = tmp_path / "launcher"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from factory.verify import main\n"
        "assert sys.argv[1] == 'verify'\n"
        "sys.exit(main(sys.argv[2:]))\n"
    )
    launcher.chmod(0o755)
    result = InstalledLauncherGate(launcher, tmp_path / "receipts").verify(
        artifacts=str(s["artifacts"]),
        repository=str(workspace),
        base_sha=base,
        candidate_sha=candidate,
        frozen_paths=("-guard",),
    )
    assert result.certified is (not modified), result
    assert result.reason == ("frozen_path_modified" if modified else "ok"), result
    if modified:
        assert json.loads(result.detail) == ["-guard"]


def raw_candidate(s):
    """Add a non-UTF-8 name using Git objects only (no APFS checkout)."""
    repo = s["repo"]
    git(repo, "read-tree", s["candidate"])
    blob = git(repo, "rev-parse", f"{s['candidate']}:app.py")
    name = b"tests/raw-\xff-\xfe"
    subprocess.run(
        ["git", "update-index", "-z", "--index-info"],
        cwd=repo,
        input=b"100644 " + blob.encode() + b"\t" + name + b"\0",
        check=True,
        capture_output=True,
    )
    tree = git(repo, "write-tree")
    candidate = git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit-tree",
        tree,
        "-p",
        s["candidate"],
        "-m",
        "raw-byte object candidate",
    )
    manifest = s["artifacts"] / "evidence-manifest.json"
    payload = json.loads(manifest.read_text())
    payload["git_revision"] = candidate
    payload["source"]["sha"] = candidate
    payload["source"]["start"]["sha"] = candidate
    payload["source"]["end"]["sha"] = candidate
    manifest.write_text(json.dumps(payload))
    refresh_sidecar(s["artifacts"])
    return candidate, name


@pytest.mark.parametrize("as_json", [False, True])
def test_module_rejects_raw_byte_frozen_change_before_legacy_decoder(
    tmp_path, monkeypatch, as_json
):
    s = seed(tmp_path)
    candidate, name = raw_candidate(s)
    raw_tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "-z", candidate], cwd=s["repo"]
    )
    assert name in raw_tree.split(b"\0")
    # Diagnostic control proves the bound synthetic manifest is otherwise valid.
    # The actual module invocation below runs unmodified code in a fresh process.
    monkeypatch.setattr(
        evidence,
        "_candidate_tree_paths",
        lambda repo, sha: (
            {os.fsdecode(path) for path in raw_tree.split(b"\0") if path},
            "",
        ),
    )
    control = evidence.verify_evidence(s["artifacts"], s["repo"], candidate, s["base"])
    assert control.ok, control
    argv = [
        sys.executable,
        "-m",
        "factory.verify",
        "--artifacts-dir",
        str(s["artifacts"]),
        "--repo",
        str(s["repo"]),
        "--candidate",
        candidate,
        "--base",
        s["base"],
        "--frozen=tests/",
    ]
    if as_json:
        argv.append("--json")
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 1, result.stderr
    assert result.stderr == ""
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["reason"] == "frozen_path_modified"
        assert json.loads(payload["detail"]) == [os.fsdecode(name)]
        assert payload["candidate"] == candidate
        assert payload["base"] == s["base"]
        assert payload["checks"] == [
            {"name": "frozen_paths", "ok": False, "reason": "frozen_path_modified"}
        ]
    else:
        assert "REJECTED frozen_path_modified" in result.stdout
        assert json.dumps([os.fsdecode(name)]) in result.stdout
    # No flag retains the legacy failure; this repair is not a decoder rewrite.
    legacy = subprocess.run(
        [arg for arg in argv if not arg.startswith("--frozen=")],
        capture_output=True,
        text=True,
    )
    assert legacy.returncode == 1
    assert legacy.stdout == ""
    assert "UnicodeDecodeError" in legacy.stderr
