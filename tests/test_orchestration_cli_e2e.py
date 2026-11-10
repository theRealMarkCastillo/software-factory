"""End-to-end CLI bodies with stubbed external binaries.

The shipped ``run`` and ``resume`` commands are executed as real subprocesses
against a temporary installation, a temporary board projection (a stub
``hermes`` executable) and a temporary worker (the same stub commits a
candidate). No Docker, board database or network is used.

This is the coverage that was missing: the previous CLI tests only exercised
refusals, so the documented path (a JSON assignment -> admitted run -> persisted
worker hold -> resume that adopts the durable receipt -> installed acceptance ->
independent-review refusal) was never executed as shipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARD = "t_abcd1234"

STUB_HERMES = """#!{python}
import json, subprocess, sys, time
from pathlib import Path

argv = sys.argv[1:]
workdir = None
if "--in" in argv:
    workdir = Path(argv[argv.index("--in") + 1])
if argv[:1] == ["kanban"]:
    rest = [part for part in argv[1:] if part not in ("--board", "software-factory")]
    if rest[:1] == ["show"]:
        print(json.dumps({{
            "task": {{"id": rest[1], "status": "running", "assignee": "factory-runner",
                      "workspace_path": ""}},
            "runs": [{{"id": 7, "status": "running", "outcome": None}}],
        }}))
        sys.exit(0)
    if rest[:1] == ["heartbeat"]:
        sys.exit(0)
    sys.exit(2)
if workdir is None:
    sys.exit(2)
time.sleep(2.0)
print(json.dumps({{"type": "system", "session_id": "stub-session"}}), flush=True)
(workdir / "normalize.txt").write_text("normalize\\n")
subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
subprocess.run(
    ["git", "-C", str(workdir), "-c", "user.email=t@example.com",
     "-c", "user.name=t", "commit", "-qm", "candidate"], check=True,
)
head = subprocess.run(
    ["git", "-C", str(workdir), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
).stdout.strip()
print(json.dumps({{"type": "result", "session_id": "stub-session",
                   "exit_code": 0, "text": head}}), flush=True)
"""

STUB_FACTORY = """#!{python}
import os, sys
command = sys.argv[1]
if command == "orchestrate":
    os.execv(sys.executable, [sys.executable, "-m", "factory_orchestration", *sys.argv[2:]])
if command == "verify":
    print('{{"ok": true, "reason": "ok", "detail": "", "checks": []}}')
    sys.exit(0)
sys.exit(2)
"""

STUB_ACCEPTANCE = """#!{python}
import json, subprocess, sys
from pathlib import Path

from factory.evidence import REQUIRED_ARTIFACTS, write_evidence_manifest

repo, workspace, artifacts, base = (
    Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
)
candidate = subprocess.run(
    ["git", "-C", str(workspace), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
).stdout.strip()
subprocess.run(
    ["git", "-C", str(repo), "-c", "advice.detachedHead=false",
     "checkout", "-q", "--detach", candidate], check=True,
)
artifacts.mkdir(parents=True, exist_ok=True)
for name in REQUIRED_ARTIFACTS:
    (artifacts / name).write_text(f"{{name}}: ran\\n")
write_evidence_manifest(
    artifacts,
    repo,
    entry_point="stub acceptance",
    command=["stub"],
    mode="acceptance",
    source_start={{"sha": candidate, "status_porcelain": ""}},
    source_end={{"sha": candidate, "status_porcelain": ""}},
    base={{"ref": "main", "kind": "commit", "sha": base}},
    runtime={{
        "teardown": {{"attempted": True, "ok": True, "detail": ""}},
        "safety": {{"postflight": "clean", "protected_changes": [],
                   "unexpected_changes": [], "collateral_damage": False}},
    }},
    artifacts=list(REQUIRED_ARTIFACTS),
    audit={{"status": "pass", "passed": True}},
    coverage={{"status": "pass", "passed": True, "base": "main",
               "base_sha": base, "base_kind": "commit"}},
    datamodel={{"status": "skipped", "reason": "no_data_model"}},
    execution={{"exit_code": 0, "passed": True, "stage": "complete", "duration_s": 0.1}},
    fixture_paths=[],
    gates_run=True,
)
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text.format(python=sys.executable))
    path.chmod(0o755)
    return path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _build(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".env.example").write_text("A=1\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "base",
    )
    base = _git(repo, "rev-parse", "HEAD")
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "work", str(workspace), base],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    spec = tmp_path / "spec.md"
    spec.write_text("# bounded card\n")
    digest = "sha256:" + __import__("hashlib").sha256(spec.read_bytes()).hexdigest()
    artifacts = tmp_path / "artifacts"
    state = tmp_path / "state"
    state.mkdir()
    (state / "admission.json").write_text(
        json.dumps({"enabled": True, "enrolled_cards": [CARD]})
    )
    bins = tmp_path / "bin"
    bins.mkdir()
    _write(bins / "hermes", STUB_HERMES)
    factory = _write(bins / "factory", STUB_FACTORY)
    acceptance = _write(tmp_path / "acceptance.py", STUB_ACCEPTANCE)
    assignment = tmp_path / "assignment.json"
    assignment.write_text(
        json.dumps(
            {
                "board_id": "software-factory",
                "card_id": CARD,
                "repository": str(repo),
                "workspace_path": str(workspace),
                "immutable_base_sha": base,
                "spec_reference": str(spec),
                "spec_digest": digest,
                "template_id": "bounded-card-v1",
                "worker_profile": "factory-runner",
                "reviewer_profile": "factory-qa",
                "acceptance_argv": [
                    str(acceptance),
                    str(repo),
                    str(workspace),
                    str(artifacts),
                    base,
                ],
                "acceptance_artifacts": str(artifacts),
                "max_repairs": 2,
            },
            indent=2,
        )
        + "\n"
    )
    env = dict(os.environ)
    env["PATH"] = f"{bins}{os.pathsep}{env.get('PATH', '')}"
    env["HERMES_KANBAN_TASK"] = CARD
    env["HERMES_KANBAN_RUN_ID"] = "7"
    env["HERMES_KANBAN_CLAIM_LOCK"] = "stub-lock"
    env["HERMES_KANBAN_BOARD"] = "software-factory"
    env["HERMES_PROFILE"] = "factory-runner"
    return {
        "repo": repo,
        "workspace": workspace,
        "artifacts": artifacts,
        "state": state,
        "assignment": assignment,
        "launcher": factory,
        "base": base,
        "env": env,
    }


def _cli(fixture: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_orchestration",
            "--state-dir",
            str(fixture["state"]),
            "--launcher",
            str(fixture["launcher"]),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=fixture["env"],
        timeout=300,
        check=False,
    )


def _json(completed: subprocess.CompletedProcess) -> dict:
    for stream in (completed.stdout, completed.stderr):
        text = (stream or "").strip()
        if text.startswith("{"):
            return json.loads(text)
    raise AssertionError(f"no JSON: {completed.stdout!r} {completed.stderr!r}")


def _worker_finished(artifacts: Path) -> bool:
    for transcript in (artifacts / "hermes-worker").glob("*/stream.jsonl"):
        for line in transcript.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return True
    return False


def _wait_worker(artifacts: Path, timeout: float = 60.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _worker_finished(artifacts):
            return True
        time.sleep(0.5)
    return False


def test_run_and_resume_bodies_execute_end_to_end(tmp_path):
    fixture = _build(tmp_path)

    run = _cli(
        fixture,
        "run",
        "--assignment",
        str(fixture["assignment"]),
        "--artifacts",
        str(fixture["artifacts"]),
        "--attempt-run-id",
        "7",
        "--attempt-profile",
        "factory-runner",
        "--claim-lock",
        "stub-lock",
    )

    assert run.returncode == 0, run.stderr
    first = _json(run)
    assert first["status"] == "awaiting_worker"
    assert first["awaiting"]["kind"] == "await_worker"
    requests = list((fixture["artifacts"] / "hermes-worker").glob("*/request.json"))
    assert len(requests) == 1

    # A second run must not submit a second worker.
    duplicate = _cli(
        fixture,
        "run",
        "--assignment",
        str(fixture["assignment"]),
        "--artifacts",
        str(fixture["artifacts"]),
        "--attempt-run-id",
        "7",
        "--attempt-profile",
        "factory-runner",
        "--claim-lock",
        "stub-lock",
    )
    assert duplicate.returncode == 0
    assert _json(duplicate)["status"] == "awaiting_worker"
    assert (
        len(list((fixture["artifacts"] / "hermes-worker").glob("*/request.json"))) == 1
    )

    # The worker stub finishes after the run process exited; resume adopts its
    # durable receipt, runs the installed gate, and refuses the unenforced
    # reviewer (the documented default) instead of claiming a review.
    assert _wait_worker(fixture["artifacts"])
    resumed = _cli(
        fixture,
        "resume",
        CARD,
        "--resolved-hold",
        "worker request awaited",
        "--artifacts",
        str(fixture["artifacts"]),
        "--attempt-run-id",
        "7",
        "--attempt-profile",
        "factory-runner",
    )
    assert resumed.returncode == 0, resumed.stderr
    result = _json(resumed)
    assert result["status"] == "held"
    assert result["stage"] == "hold_operator"
    assert (
        len(list((fixture["artifacts"] / "hermes-worker").glob("*/request.json"))) == 1
    )

    inspected = _json(_cli(fixture, "inspect", CARD))
    assert inspected["state"]["last_reason_code"] == "capability_unenforced"
    assert inspected["state"]["candidate_sha"] == _git(
        fixture["workspace"], "rev-parse", "HEAD"
    )
    assert inspected["state"]["evidence_reference"]


def test_resume_without_a_finished_worker_refuses_to_replay(tmp_path):
    fixture = _build(tmp_path)
    run = _cli(
        fixture,
        "run",
        "--assignment",
        str(fixture["assignment"]),
        "--artifacts",
        str(fixture["artifacts"]),
        "--attempt-run-id",
        "7",
        "--attempt-profile",
        "factory-runner",
        "--claim-lock",
        "stub-lock",
    )
    assert run.returncode == 0
    # The stub worker sleeps two seconds; resume immediately must hold, not replay.
    resumed = _cli(
        fixture,
        "resume",
        CARD,
        "--resolved-hold",
        "worker request awaited",
        "--artifacts",
        str(fixture["artifacts"]),
        "--attempt-run-id",
        "7",
        "--attempt-profile",
        "factory-runner",
    )
    assert resumed.returncode == 1
    assert _json(resumed)["reason"] == "effect_unknown"
    assert (
        len(list((fixture["artifacts"] / "hermes-worker").glob("*/request.json"))) == 1
    )
