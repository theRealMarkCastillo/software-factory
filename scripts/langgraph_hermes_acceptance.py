#!/usr/bin/env python3
"""End-to-end graph run with the real Hermes worker and sandboxed reviewer.

This is the acceptance the offline suite cannot provide: one ``factory-card/v1``
graph execution whose implementation step is a **real Hermes worker profile**
(``hermes -p ... chat``) and whose independent review runs in the **attested
Docker sandbox**. Acceptance itself goes through the installed
``bin/factory`` launcher and verifier.

The model behind both agents is a loopback scripted provider from the installed
Hermes test fixtures. That keeps the *transport* deterministic: the Hermes
runtime, the tool stack, the git commit, the attestation, the container, the
installed gate and the review audit are all real; the model text is scripted.
A real-provider review is separate evidence
(``scripts/live_review_fixture.py``).

The board projection is a read-only CLI fixture: creating or mutating a real
Kanban board is outside this card's authority. The graph only reads that
projection (``kanban show``) and never writes to the board.

Usage::

    .venv/bin/python scripts/langgraph_hermes_acceptance.py --run \\
        --scratch /path/to/disposable/scratch \\
        --out docs/langgraph-hybrid/evidence/hermes-graph-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from factory_orchestration.adapters.base import WorkerHandle  # noqa: E402
from factory_orchestration.adapters.gates import InstalledLauncherGate  # noqa: E402
from factory_orchestration.adapters.hermes import (  # noqa: E402
    HermesBoard,
    HermesWorker,
)
from factory_orchestration.adapters.reviewer_sandbox import _load_fake_provider  # noqa: E402
from factory_orchestration.cli import _reviewer, build_parser  # noqa: E402
from factory_orchestration.graph import Deps, thread_id  # noqa: E402
from factory_orchestration.runner import drive, inspect_run  # noqa: E402
from factory_orchestration.state import Attempt, RunAssignment  # noqa: E402
from factory_orchestration.store import RunStore  # noqa: E402

LAUNCHER = Path("~/software-factory-ops/bin/factory")
CARD = "t_1a2b3c4d"
PREFLIGHT_KEY = "factory-preflight-token"


def terminate_disposable_gateways(scratch: Path) -> list[int]:
    """Stop gateway clients spawned by the disposable homes (never host units)."""

    import signal

    needle = str(scratch)
    listing = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False
    )
    killed = []
    for line in listing.stdout.splitlines():
        line = line.strip()
        if needle not in line or "gateway run" not in line:
            continue
        pid = int(line.split()[0])
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
    return killed


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.email=fixture@example.com",
        "-c",
        "user.name=fixture",
        "commit",
        "-qm",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


def build_repo(scratch: Path) -> dict:
    repo = scratch / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("Disposable LangGraph/Hermes acceptance fixture.\n")
    (repo / ".env.example").write_text("ACCEPTANCE_MARKER=1\n")
    (repo / "compose.yml").write_text(
        "services:\n"
        "  test:\n"
        "    image: busybox:latest\n"
        '    command: ["sh", "-c", "echo hermes-graph-acceptance-ok"]\n'
    )
    (repo / "module.py").write_text("VALUE = 1\n")
    base = commit(repo, "base")
    worktree = scratch / "workspace"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), base],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return {"repo": repo, "worktree": worktree, "base": base}


def write_board_stub(scratch: Path, payload: dict) -> Path:
    """A read-only ``hermes kanban`` projection, so no board is mutated."""

    stub = scratch / "board-stub"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"SHOW = {json.dumps(json.dumps(payload))}\n"
        "if 'kanban' in sys.argv:\n"
        "    if 'show' in sys.argv:\n"
        "        print(SHOW)\n"
        "        raise SystemExit(0)\n"
        "    if 'heartbeat' in sys.argv:\n"
        "        raise SystemExit(0)\n"
        "raise SystemExit(2)\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def write_worker_home(scratch: Path, base_url: str) -> Path:
    home = scratch / "worker-home"
    profile = home / "profiles" / "factory-runner"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        f"  base_url: {base_url}\n"
        "  default: fake-model\n"
        "  context_length: 128000\n"
        "agent:\n"
        "  api_max_retries: 0\n"
        "terminal:\n"
        "  backend: local\n"
        "  timeout: 300\n"
        "telemetry:\n"
        "  shared_metrics:\n"
        "    enabled: false\n"
        "    send: false\n"
    )
    (profile / ".env").write_text(f"OPENAI_API_KEY={PREFLIGHT_KEY}\n")
    return home


def worker_script(workspace: Path) -> list:
    _, Text, ToolCall = _load_fake_provider()
    commit_command = (
        f"git -C {workspace} add -A && "
        f"git -C {workspace} -c user.email=fixture@example.com "
        f"-c user.name=fixture commit -qm 'candidate: acceptance marker'"
    )
    return [
        ToolCall(
            "write_file",
            {
                "path": str(workspace / "CHANGE.md"),
                "content": "hermes graph acceptance candidate\n",
            },
        ),
        ToolCall("terminal", {"command": commit_command}),
        Text("committed the bounded candidate change"),
    ]


def review_script() -> list:
    _, Text, ToolCall = _load_fake_provider()
    return [
        ToolCall("read_file", {"path": "/evidence/candidate/README.md"}),
        ToolCall(
            "read_file",
            {"path": "/evidence/evidence/evidence-manifest.json"},
        ),
        Text(
            '{"outcome":"approve","comment":"candidate and evidence inspected; '
            'bounded fixture change is consistent with the installed acceptance"}'
        ),
    ]


def receipt_payload(receipt) -> dict:
    return {
        "receipt": {
            "operation_key": receipt.operation_key,
            "session_id": receipt.session_id,
            "outcome": receipt.outcome,
            "candidate_sha": receipt.candidate_sha,
            "artifact_references": list(receipt.artifact_references),
            "reported_tests": list(receipt.reported_tests),
            "detail": receipt.detail,
        }
    }


def review_payload(review) -> dict:
    return {
        "review": {
            "operation_key": review.operation_key,
            "reviewer": review.reviewer,
            "outcome": review.outcome,
            "candidate_sha": review.candidate_sha,
            "base_sha": review.base_sha,
            "spec_digest": review.spec_digest,
            "evidence_digest": review.evidence_digest,
            "graph_version": review.graph_version,
            "comment": review.comment,
        }
    }


def drive_until_settled(deps, assignment, attempt, *, deadline_s: int = 1800) -> dict:
    """Drive the graph, polling the adapters it owns and resuming with receipts.

    This is the owning worker's poll loop: no daemon and no external scheduler.
    """

    result = drive(deps, assignment, attempt)
    deadline = time.time() + deadline_s
    while result.awaiting is not None and time.time() < deadline:
        kind = result.awaiting.get("kind")
        key = result.awaiting.get("operation_key", "")
        payload = None
        if kind == "await_worker":
            receipt = deps.worker.poll(WorkerHandle(key, ""))
            if receipt is not None:
                payload = receipt_payload(receipt)
        elif kind == "await_review":
            review = deps.reviewer.poll(WorkerHandle(key, ""))
            if review is not None:
                payload = review_payload(review)
        if payload is None:
            time.sleep(0.5)
            continue
        result = drive(deps, assignment, attempt, resume_payload=payload)
    if result.awaiting is not None:
        raise SystemExit(f"graph did not settle: {result.awaiting}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="actually execute")
    parser.add_argument(
        "--scratch",
        default=str(
            Path.home()
            / ".hermes/profiles/factory-runner/cache/scratch/lg-hermes-acceptance"
        ),
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs/langgraph-hybrid/evidence/hermes-graph-run"),
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--image", default="")
    args = parser.parse_args()
    if not args.run:
        print(
            "would run one graph with a real scripted Hermes worker and the "
            f"attested reviewer sandbox into {args.out}"
        )
        return 0

    scratch = Path(args.scratch).expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    terminate_disposable_gateways(scratch)
    # Each run gets its own fixture tree; the disposable reviewer generation is
    # shared so its dependency bootstrap is not repeated (and never deleted
    # while a Hermes client might still hold it).
    run_root = scratch / time.strftime("run-%Y%m%dT%H%M%S")
    run_root.mkdir(parents=True, exist_ok=False)
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    repo_info = build_repo(run_root)
    base = repo_info["base"]
    workspace = repo_info["worktree"]

    spec = run_root / "card-spec.md"
    spec.write_text("# Acceptance card\n\nCommit one bounded candidate marker.\n")
    spec_digest = "sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest()
    artifacts_root = run_root / "artifacts"
    state_dir = run_root / "state"
    state_dir.mkdir()
    (state_dir / "admission.json").write_text(
        json.dumps({"enabled": True, "enrolled_cards": [CARD]})
    )

    attempt = Attempt(task_id=CARD, run_id=7, profile="factory-runner")
    grant = {
        "HERMES_KANBAN_TASK": CARD,
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_CLAIM_LOCK": "fixture-host:1",
    }
    show = {
        "task": {
            "id": CARD,
            "status": "running",
            "assignee": "factory-runner",
            "workspace_path": str(workspace),
        },
        "runs": [{"id": 7, "profile": "factory-runner", "status": "running"}],
    }
    board = HermesBoard(
        executable=str(write_board_stub(run_root, show)),
        board="software-factory",
        env=grant,
    )

    store = RunStore(state_dir)
    gate = InstalledLauncherGate(LAUNCHER, run_root / "gate-receipts")
    FakeLLMServer, _Text, _ToolCall = _load_fake_provider()
    summary: dict = {
        "schema": "factory-hermes-graph-acceptance/v1",
        "revision": git(ROOT, "rev-parse", "HEAD"),
        "card": CARD,
        "base": base,
        "release": (LAUNCHER.parent.parent / "RELEASE_MANIFEST.txt").read_text()
        if (LAUNCHER.parent.parent / "RELEASE_MANIFEST.txt").is_file()
        else "",
        "board_projection": "read-only CLI fixture; no Kanban write",
    }

    with (
        FakeLLMServer(worker_script(workspace)) as worker_server,
        FakeLLMServer(review_script()) as review_server,
    ):
        worker_home = write_worker_home(run_root, worker_server.base_url)
        worker = HermesWorker(
            artifact_root=run_root / "worker-artifacts",
            home=worker_home,
            max_turns=20,
            terminate_grace_s=10,
        )
        review_args = build_parser().parse_args(
            [
                "--state-dir",
                str(state_dir),
                "run",
                "--assignment",
                "unused.json",
                "--reviewer-sandbox",
                "--reviewer-root",
                str(scratch / "reviewer-root"),
                "--review-model",
                "fake-model",
                "--review-provider",
                "custom",
                "--review-base-url",
                review_server.base_url,
                "--review-provider-profile",
                "absent-profile",
            ]
        )
        reviewer = _reviewer(review_args, store, run_root / "review-artifacts")
        reviewer.review_api_mode = ""
        reviewer.provider_env = {"OPENAI_API_KEY": PREFLIGHT_KEY}
        if args.image:
            reviewer.attestor.image = args.image
        deps = Deps(
            store=store,
            board=board,
            worker=worker,
            gate=gate,
            reviewer=reviewer,
            state_dir=state_dir,
            artifact_dir=artifacts_root,
            gate_timeout_s=args.timeout,
            review_timeout_s=args.timeout,
        )
        assignment = RunAssignment(
            board_id="software-factory",
            card_id=CARD,
            repository=str(repo_info["repo"]),
            workspace_path=str(workspace),
            immutable_base_sha=base,
            spec_reference=str(spec),
            spec_digest=spec_digest,
            template_id="bounded-card-v1",
            worker_profile="factory-runner",
            reviewer_profile="factory-qa",
            acceptance_argv=(
                str(LAUNCHER),
                "run",
                "--card",
                CARD,
                "--repo",
                str(workspace),
                "--compose",
                "compose.yml",
                "--test-service",
                "test",
                "--artifacts-root",
                str(artifacts_root),
            ),
            acceptance_artifacts=str(artifacts_root),
            engine_release_reference="installed:" + str(LAUNCHER),
            engine_fingerprint="sha256:" + "a" * 64,
            host_policy_digest="sha256:" + "b" * 64,
            max_repairs=0,
        )
        started = time.time()
        result = drive_until_settled(deps, assignment, attempt, deadline_s=args.timeout)
        elapsed = round(time.time() - started, 1)

    view = inspect_run(deps, thread_id(assignment))
    run_state = result.state
    summary.update(
        {
            "status": result.status,
            "stage": result.stage,
            "candidate": run_state.get("candidate_sha", ""),
            "evidence_reference": run_state.get("evidence_reference", ""),
            "evidence_digest": run_state.get("evidence_digest", ""),
            "approval_reference": run_state.get("approval_reference", ""),
            "elapsed_s": elapsed,
            "metrics": view["metrics"],
            "operations": [
                {key: op.get(key) for key in ("stage", "kind", "state", "repair_index")}
                for op in view["operations"]
            ],
            "events": deps.store.events(thread_id(assignment)),
        }
    )
    (out / "run.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    # Copy the durable receipts that make the run checkable after the scratch
    # tree is gone.
    for name in ("orchestration.sqlite", "checkpoints.sqlite"):
        source = state_dir / name
        if source.is_file():
            shutil.copyfile(source, out / name)
    for directory_name in ("review-artifacts", "worker-artifacts"):
        source = run_root / directory_name
        if source.is_dir():
            shutil.copytree(source, out / directory_name, dirs_exist_ok=True)
    evidence = Path(run_state.get("evidence_reference") or "")
    if evidence.is_dir():
        shutil.copytree(evidence, out / "acceptance", dirs_exist_ok=True)

    from factory import policy

    try:
        for container_id in policy.find_containers(
            {"hermes-agent": "1", "hermes-profile": "factory-reviewer"}
        ):
            policy.remove_own_container(
                container_id, expected_profile="factory-reviewer"
            )
    except policy.PolicyViolation:
        pass

    ok = (
        result.status == "ready_for_authorized_completion"
        and summary["candidate"] != base
        and summary["evidence_digest"]
        and summary["approval_reference"]
    )
    terminate_disposable_gateways(scratch)
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "status",
                    "stage",
                    "candidate",
                    "evidence_reference",
                    "evidence_digest",
                    "approval_reference",
                    "elapsed_s",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
