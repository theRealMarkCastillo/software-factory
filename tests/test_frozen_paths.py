"""Frozen acceptance contracts, exercised offline with real Git objects."""

import json
import subprocess
import sys
import unicodedata
from dataclasses import replace
from types import SimpleNamespace

import pytest
from factory_orch_fixtures import commit, git, make_evidence, seed
from test_repair_context import GOLDEN, PROMPT_STATE, capture, context, run

from factory import verify
from factory.evidence import verify_evidence
from factory_orchestration.adapters.gates import InstalledLauncherGate
from factory_orchestration.cli import _assignment_payload
from factory_orchestration.errors import OrchestrationError
from factory_orchestration.graph import _implementation_prompt, thread_id
from factory_orchestration.runner import assignment_from_state, resume


def freeze(s, *paths):
    s["assignment"] = replace(s["assignment"], frozen_paths=tuple(paths))


def candidate(s):
    sha = commit(s["workspace"], "candidate change")
    s["candidate"] = sha
    s["worker"].candidate_sha = sha
    make_evidence(
        s["artifacts"],
        s["workspace"],
        s["base"],
        sha,
        **({"fixture_paths": []} if (s["workspace"] / "tests").is_file() else {}),
    )
    return sha


def prepared(tmp_path, file_entry=False):
    s = seed(tmp_path)
    w = s["workspace"]
    (w / "compose.yml").write_text("command: pytest\n")
    (w / "caf\u00e9.txt").write_text("base\n")
    if file_entry:
        git(w, "rm", "-r", "tests")
        (w / "tests").write_text("a file\n")
    base = commit(w, "frozen base")
    s["base"] = base
    s["assignment"] = replace(s["assignment"], immutable_base_sha=base)
    return s


@pytest.mark.parametrize(
    "change",
    [
        "modify",
        "delete",
        "rename-out",
        "rename-in",
        "mode",
        "case",
        "unicode",
        "type",
        "gitlink",
    ],
)
def test_changed_paths_reject_before_acceptance_and_feed_repair(tmp_path, change):
    s = prepared(tmp_path)
    w = s["workspace"]
    freeze(s, "compose.yml", "tests/", "caf\u00e9.txt")
    expected = ["compose.yml"]
    if change == "modify":
        (w / "compose.yml").write_text("command: true\n")
    elif change == "delete":
        (w / "compose.yml").unlink()
    elif change == "rename-out":
        git(w, "mv", "compose.yml", "elsewhere.yml")
    elif change == "rename-in":
        git(w, "mv", "app.py", "tests/new.py")
        expected = ["tests/new.py"]
    elif change == "mode":
        (w / "compose.yml").chmod(0o755)
        git(w, "update-index", "--chmod=+x", "compose.yml")
    elif change == "case":
        # Plumbing preserves case even on case-insensitive filesystems.
        blob = git(w, "rev-parse", "HEAD:app.py")
        git(w, "update-index", "--add", "--cacheinfo", f"100644,{blob},Tests/new.py")
        git(
            w,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            "case",
        )
        git(w, "reset", "--hard", "HEAD")
        expected = ["Tests/new.py"]
    elif change == "unicode":
        git(w, "config", "core.precomposeunicode", "false")
        name = unicodedata.normalize("NFD", "caf\u00e9.txt")
        blob = git(w, "rev-parse", "HEAD:app.py")
        git(w, "rm", "caf\u00e9.txt")
        git(w, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
        git(
            w,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            "unicode",
        )
        git(w, "reset", "--hard", "HEAD")
        expected = sorted([name, "caf\u00e9.txt"])
    elif change == "type":
        (w / "compose.yml").unlink()
        (w / "compose.yml").symlink_to("app.py")
    else:
        git(w, "rm", "compose.yml")
        git(
            w, "update-index", "--add", "--cacheinfo", f"160000,{s['base']},compose.yml"
        )
    if change in {"case", "unicode"}:
        sha = git(w, "rev-parse", "HEAD")
    elif change in {"mode", "gitlink"}:
        git(
            w,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            "index change",
        )
        sha = git(w, "rev-parse", "HEAD")
        git(w, "reset", "--hard", "HEAD")
    else:
        sha = commit(w, "candidate")
    s["worker"].candidate_sha = sha
    requests = capture(s)
    result = run(s)
    assert s["gate"].submissions == []
    assert not list((s["gate"].root / "gate-receipts").glob("*.json"))
    data = context(requests[1])
    assert data["source"] == "bind"
    assert data["reason_code"] == "frozen_path_modified"
    assert json.loads(data["detail"]) == expected
    assert result.state["last_reason_code"] == "not_converging"
    assert result.state["repair_count"] == 1


def test_frozen_signature_uses_all_paths_without_volatile_normalization(tmp_path):
    s = prepared(tmp_path)
    freeze(s, "tests/")
    requests = capture(s)
    original = s["worker"].submit

    def submit(request):
        (s["workspace"] / "app.py").write_text(str(request.repair_index))
        (s["workspace"] / "tests" / "123456abcdef.txt").write_text("changed")
        s["worker"].candidate_sha = commit(s["workspace"], "attempt")
        return original(request)

    s["worker"].submit = submit
    result = run(s)
    assert len(requests) == 2
    assert result.state["last_reason_code"] == "not_converging"
    assert "signature" in result.state["last_reason_detail"]


@pytest.mark.parametrize("file_entry", [False, True])
def test_prefix_edges_and_untouched_paths_follow_normal_flow(tmp_path, file_entry):
    s = prepared(tmp_path, file_entry)
    freeze(s, "tests" if file_entry else "tests/", "compose.yml")
    (s["workspace"] / "tests_extra").mkdir()
    (s["workspace"] / "tests_extra" / "x").write_text("allowed")
    candidate(s)
    calls = []
    original = s["gate"].verify

    def gate_verify(**kw):
        calls.append(kw)
        return original(**kw)

    s["gate"].verify = gate_verify
    result = run(s)
    assert result.status == "ready_for_authorized_completion"
    assert calls[0]["frozen_paths"] == s["assignment"].frozen_paths
    assert (
        assignment_from_state(result.state).frozen_paths == s["assignment"].frozen_paths
    )


INVALID = [
    ("",),
    ("/a",),
    ("../a",),
    ("a/../b",),
    ("./a",),
    ("a/./b",),
    ("a//b",),
    ("a//",),
    ("a\\b",),
    ("a\0b",),
    ("a\nb",),
    ("a\x7fb",),
    ("a\x85b",),
    ("*.py",),
    ("a?",),
    ("[ab]",),
    ("app.py", "APP.py"),
    ("caf\u00e9.txt", "cafe\u0301.txt"),
    tuple(f"path{i}" for i in range(201)),
    ("absent",),
    ("tests",),
    ("absent/",),
    ("app.py/",),
]


@pytest.mark.parametrize("paths", INVALID)
def test_invalid_assignment_refused_before_effects(tmp_path, paths):
    s = prepared(tmp_path)
    with pytest.raises(OrchestrationError) as exc:
        freeze(s, *paths)
        run(s)
    assert exc.value.reason == "invalid_assignment"
    assert not s["deps"].store.run(thread_id(s["assignment"]))
    assert not list((tmp_path / "effects").rglob("*.json"))


def test_old_assignment_and_checkpoint_resume_without_frozen_field(tmp_path):
    s = seed(tmp_path)
    assert s["assignment"].frozen_paths == ()
    s["worker"].outcomes = ["pending"]
    paused = run(s)
    state = dict(paused.state)
    state.pop("frozen_paths", None)
    assert assignment_from_state(state).frozen_paths == ()
    assert "frozen_paths" not in paused.state
    result = resume(
        s["deps"],
        paused.thread_id,
        s["attempt"],
        payload={
            "receipt": {
                "operation_key": paused.awaiting["operation_key"],
                "session_id": "fixture",
                "outcome": "completed",
                "candidate_sha": s["candidate"],
            }
        },
        resolved_hold="worker finished",
    )
    assert result.status == "ready_for_authorized_completion"


def test_prompt_and_json_assignment_boundary():
    paths = ('a"quoted', "tests/")
    payload = _assignment_payload({"frozen_paths": list(paths)})
    assert payload["frozen_paths"] == paths
    prompt = _implementation_prompt({**PROMPT_STATE, "frozen_paths": paths}, 0)
    prefix = "Do not modify these paths; a candidate that changes them is rejected: "
    assert prompt == GOLDEN + prefix + json.dumps(paths) + "\n"
    assert _implementation_prompt({**PROMPT_STATE, "frozen_paths": ()}, 0) == GOLDEN


def test_empty_assignment_serialization_preserves_legacy_bytes(tmp_path):
    s = seed(tmp_path)
    assert "frozen_paths" not in s["assignment"].as_dict()
    freeze(s, "tests/")
    assert s["assignment"].as_dict()["frozen_paths"] == ("tests/",)


@pytest.mark.parametrize("as_json", [False, True])
def test_verify_cli_frozen_reject_pass_and_no_flag_bytes(tmp_path, capsys, as_json):
    s = seed(tmp_path)
    args = [
        "--artifacts-dir",
        str(s["artifacts"]),
        "--repo",
        str(s["repo"]),
        "--base",
        s["base"],
        "--candidate",
        s["candidate"],
    ]
    if as_json:
        args += ["--json"]
    original = verify_evidence(s["artifacts"], s["repo"], s["candidate"], s["base"])
    expected = (
        json.dumps(original.as_dict(), indent=2)
        if as_json
        else verify.render_human(original)
    ) + "\n"
    assert verify.main(args) == 0
    assert capsys.readouterr().out == expected
    assert (
        verify.main(args + ["--frozen", "tests/", "--frozen", "missing-at-base"]) == 0
    )
    capsys.readouterr()
    assert verify.main(args + ["--frozen", "app.py"]) == 1
    out = capsys.readouterr().out
    if as_json:
        assert json.loads(out)["reason"] == "frozen_path_modified"
    else:
        assert "REJECTED frozen_path_modified" in out


@pytest.mark.parametrize("paths", INVALID[:19])
def test_verify_refuses_invalid_frozen_syntax(tmp_path, capsys, paths):
    s = seed(tmp_path)
    args = [
        "--artifacts-dir",
        str(s["artifacts"]),
        "--repo",
        str(s["repo"]),
        "--base",
        s["base"],
        "--candidate",
        s["candidate"],
    ]
    for path in paths:
        args += ["--frozen", path]
    assert verify.main(args) == 2
    assert "frozen" in capsys.readouterr().err


@pytest.mark.parametrize("paths", [(), ("compose.yml", "tests/")])
def test_launcher_gate_argv(tmp_path, monkeypatch, paths):
    s = seed(tmp_path)
    seen = []

    def command(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(
            returncode=0, stdout='{"ok": true, "reason": "ok"}', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", command)
    gate = InstalledLauncherGate(tmp_path / "launcher", tmp_path)
    assert gate.verify(
        artifacts=str(s["artifacts"]),
        repository=str(s["repo"]),
        candidate_sha=s["candidate"],
        base_sha=s["base"],
        frozen_paths=paths,
    ).certified
    expected = [
        str(tmp_path / "launcher"),
        "verify",
        "--artifacts-dir",
        str(s["artifacts"]),
        "--repo",
        str(s["repo"]),
        "--candidate",
        s["candidate"],
        "--base",
        s["base"],
        "--json",
    ]
    for path in paths:
        expected += [f"--frozen={path}"]
    assert seen == [expected]


def test_nonempty_frozen_paths_survive_checkpoint_resume(tmp_path):
    s = seed(tmp_path)
    freeze(s, "tests/")
    s["worker"].outcomes = ["pending"]
    paused = run(s)
    assert tuple(paused.state["frozen_paths"]) == ("tests/",)
    result = resume(
        s["deps"],
        paused.thread_id,
        s["attempt"],
        payload={
            "receipt": {
                "operation_key": paused.awaiting["operation_key"],
                "session_id": "fixture",
                "outcome": "completed",
                "candidate_sha": s["candidate"],
            }
        },
        resolved_hold="worker finished",
    )
    assert result.status == "ready_for_authorized_completion"
    assert assignment_from_state(result.state).frozen_paths == ("tests/",)


def test_bounded_path_detail_and_full_set_signature(tmp_path):
    s = seed(tmp_path)
    freeze(s, "tests/")
    requests = capture(s)
    original = s["worker"].submit

    def submit(request):
        directory = s["workspace"] / "tests"
        for i in range(25):
            (directory / f"f{i:02}.txt").write_text("changed")
        # Only the path beyond the first 20 differs on each failure. It must
        # still affect convergence; do not hash the truncated detail.
        for path in directory.glob("z*.txt"):
            path.unlink()
        (directory / f"z{request.repair_index}.txt").write_text("changed")
        s["worker"].candidate_sha = commit(s["workspace"], "attempt")
        return original(request)

    s["worker"].submit = submit
    result = run(s)
    assert len(requests) == 3
    assert result.state["last_reason_code"] == "budget_exhausted"
    detail = json.loads(context(requests[1])["detail"])
    assert detail == [f"tests/f{i:02}.txt" for i in range(20)] + [{"omitted_count": 6}]
    assert s["gate"].submissions == []


def test_nul_diff_names_are_json_data_not_lines(tmp_path):
    s = seed(tmp_path)
    freeze(s, "tests/")
    name = 'tests/quoted"\nline.py'
    (s["workspace"] / name).write_text("changed")
    candidate(s)
    requests = capture(s)
    run(s)
    assert json.loads(context(requests[1])["detail"]) == [name]
    assert s["gate"].submissions == []


def test_verify_shipped_module_entrypoint(tmp_path):
    s = seed(tmp_path)
    args = [
        sys.executable,
        "-m",
        "factory.verify",
        "--artifacts-dir",
        str(s["artifacts"]),
        "--repo",
        str(s["repo"]),
        "--base",
        s["base"],
        "--candidate",
        s["candidate"],
        "--json",
    ]
    passed = subprocess.run(
        args + ["--frozen", "tests/"], capture_output=True, text=True
    )
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout)["ok"] is True
    rejected = subprocess.run(
        args + ["--frozen", "app.py"], capture_output=True, text=True
    )
    assert rejected.returncode == 1, rejected.stderr
    assert json.loads(rejected.stdout)["reason"] == "frozen_path_modified"
