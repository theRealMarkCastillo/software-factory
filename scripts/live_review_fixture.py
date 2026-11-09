#!/usr/bin/env python3
"""Reproduce one live review through the attested R2-A sandbox.

Unlike the offline suite, this script deliberately exercises Docker and the
configured model provider. It is explicit, host-side acceptance evidence:

1. build a disposable candidate/evidence fixture under ``--scratch``;
2. execute the attestation (effective config, scripted preflight through the
   real Hermes tool stack, container inspection) **in the same adapter instance**
   that then runs the review;
3. write the transcript, the adapter's durable result, the attestation receipt
   and a run summary to ``--out``.

The result file is written by the adapter from the same transcript it names, so
the pair cannot drift. The review is bound to the exact candidate/evidence tree
digests recorded in the attestation; ``verify_terminal`` re-checks them after
the model endpoint is rewritten for the real provider.

Usage::

    .venv/bin/python scripts/live_review_fixture.py --run \\
        --provider-profile factory-runner \\
        --out docs/langgraph-hybrid/evidence/reviewer-sandbox \\
        --scratch /path/to/disposable/scratch

Requires Docker and a provider credential in the named profile's ``.env``.
Without ``--run`` it only prints the plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from factory_orchestration.adapters.hermes import (  # noqa: E402
    HermesReviewer,
    HermesWorker,
    audit_review_transcript,
    read_profile_model_config,
    read_profile_provider_env,
)
from factory_orchestration.adapters.reviewer_sandbox import (  # noqa: E402
    DEFAULT_IMAGE,
    SandboxedReadonlyAttestor,
)
from factory_orchestration.errors import OrchestrationError  # noqa: E402
from factory_orchestration.operations import digest_parts  # noqa: E402
from factory_orchestration.state import Attempt  # noqa: E402


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


def build_fixture(scratch: Path) -> dict:
    """A small, coherent candidate: a bug fix, a test, and a spec."""

    repo = scratch / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text(
        "# Calculator fixture\n\nOne deterministic function, one test.\n"
    )
    (repo / "module.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_module.py").write_text(
        "from module import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    base = commit(repo, "base: add() subtracts")

    worktree = scratch / "candidate"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), base],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (worktree / "module.py").write_text("def add(a, b):\n    return a + b\n")
    candidate = commit(worktree, "candidate: add() adds")

    spec = scratch / "card-spec.md"
    spec.write_text(
        "# Fixture card\n\n"
        "`add(a, b)` must return the sum. The candidate must keep the test green\n"
        "and must not change any other behaviour.\n"
    )

    evidence = scratch / "evidence"
    evidence.mkdir()
    test_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    (evidence / "pytest-output.txt").write_text(
        "$ python -m pytest -q\n"
        f"exit_code: {test_run.returncode}\n\n{test_run.stdout}{test_run.stderr}"
    )
    (evidence / "git-log.txt").write_text(
        git(worktree, "log", "--oneline", "-3") + "\n"
    )
    (evidence / "git-status.txt").write_text(
        git(worktree, "status", "--porcelain") + "\n"
    )
    (evidence / "spec.md").write_text(spec.read_text())
    (evidence / "commits.json").write_text(
        json.dumps({"base": base, "candidate": candidate}, indent=2) + "\n"
    )
    (evidence / "candidate.diff").write_text(
        subprocess.run(
            ["git", "diff", f"{base}..{candidate}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    )
    (evidence / "summary.json").write_text(
        json.dumps(
            {
                "schema": "factory-live-review-fixture/v1",
                "note": "Transport fixture, not a factory acceptance record.",
                "base": base,
                "candidate": candidate,
                "spec_sha256": "sha256:"
                + hashlib.sha256(spec.read_bytes()).hexdigest(),
                "test_exit": test_run.returncode,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    # Mount a plain tree: the worktree's .git file points at a host path, which
    # would leak a host location into the review root for no benefit.
    mounted = scratch / "candidate-tree"
    shutil.copytree(
        worktree,
        mounted,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    return {
        "repo": repo,
        "worktree": worktree,
        "candidate_tree": mounted,
        "spec": spec,
        "evidence": evidence,
        "base": base,
        "candidate": candidate,
        "test_exit": test_run.returncode,
    }


def run_review(args, fixture: dict) -> dict:
    reviewer_profile = args.provider_profile
    mode = read_profile_model_config(reviewer_profile)
    provider_env = read_profile_provider_env(reviewer_profile)
    model = args.review_model or mode.get("default", "")
    base_url = args.review_base_url or mode.get("base_url", "")
    if not model or not base_url:
        raise SystemExit(
            f"profile {reviewer_profile!r} has no readable model/base_url; "
            "pass --review-model/--review-base-url"
        )
    if not provider_env:
        raise SystemExit(
            f"profile {reviewer_profile!r} has no allowlisted provider credential in .env"
        )

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    artifact_root = (
        Path(args.scratch).expanduser().resolve()
        / f"review-artifacts-{int(time.time())}"
    )
    artifact_root.mkdir(parents=True)

    attestor = SandboxedReadonlyAttestor(
        root=Path(args.scratch).expanduser().resolve() / "sandbox-root",
        image=args.image or DEFAULT_IMAGE,
        executable=args.hermes,
    )
    reviewer = HermesReviewer(
        HermesWorker(artifact_root=artifact_root),
        attestor,
        review_provider=args.review_provider or mode.get("provider", "custom"),
        review_base_url=base_url,
        review_model=model,
        review_api_mode=args.review_api_mode or mode.get("api_mode", ""),
        provider_env=provider_env,
    )
    attempt = Attempt(task_id="t_abcd1234", run_id=1, profile="factory-runner")
    candidate = fixture["candidate"]
    spec_digest = "sha256:" + hashlib.sha256(fixture["spec"].read_bytes()).hexdigest()
    # The tree digest is computed over the mounted tree, exactly as attestation
    # will; this is the digest the review is bound to.
    from factory_orchestration.adapters.reviewer_sandbox import tree_digest

    evidence_digest = tree_digest(fixture["evidence"])
    operation_key = digest_parts(
        "live-review-fixture/v1",
        candidate,
        fixture["base"],
        spec_digest,
        evidence_digest,
    )
    started = int(time.time())
    handle, inline = reviewer.submit(
        operation_key=operation_key,
        attempt=attempt,
        reviewer=args.reviewer_label,
        workspace=str(fixture["candidate_tree"]),
        candidate_sha=candidate,
        base_sha=fixture["base"],
        spec_digest=spec_digest,
        evidence_digest=evidence_digest,
        artifacts=str(fixture["evidence"]),
        timeout_s=args.timeout,
    )
    review = inline
    deadline = time.time() + args.timeout
    while review is None and time.time() < deadline:
        review = reviewer.poll(handle)
        if review is None:
            time.sleep(0.5)
    if review is None:
        reviewer.cancel(handle)
        raise SystemExit("reviewer did not produce a result before the timeout")

    directory = artifact_root / "hermes-reviewer" / operation_key.replace(":", "_")
    transcript = directory / "stream.jsonl"
    result_path = directory / "review-result.json"
    attestation_path = directory / "attestation.json"
    request_path = directory / "request.json"

    # Recompute the audit from the committed transcript and require the durable
    # result to agree with it: the pair must never drift.
    audit = audit_review_transcript(transcript)
    durable = json.loads(result_path.read_text())
    if durable.get("audit") != audit:
        raise SystemExit("committed review result does not match its transcript")
    if durable.get("candidate_sha") != candidate:
        raise SystemExit("review result is not bound to the reviewed candidate")
    if durable.get("outcome") != review.outcome:
        raise SystemExit("review result outcome differs from the adapter result")

    for source, name in (
        (transcript, "live-review-fixture.jsonl"),
        (result_path, "live-review-fixture-result.json"),
        (attestation_path, "attestation.json"),
        (request_path, "live-review-fixture-request.json"),
    ):
        shutil.copyfile(source, out / name)

    summary = {
        "schema": "factory-live-review/v1",
        "revision": git(ROOT, "rev-parse", "HEAD"),
        "base": fixture["base"],
        "candidate": candidate,
        "spec_digest": spec_digest,
        "evidence_digest": evidence_digest,
        "operation_key": operation_key,
        "reviewer_label": args.reviewer_label,
        "provider_profile": reviewer_profile,
        "provider": reviewer.review_provider,
        "model": reviewer.review_model,
        "started_at": started,
        "finished_at": int(time.time()),
        "outcome": review.outcome,
        "comment": review.comment,
        "tool_uses": audit["tool_uses"],
        "violations": audit["violations"],
        "verdict": audit["verdict"],
        "attestation_digest": durable.get("attestation_digest", ""),
        "fixture": {
            "note": "transport fixture; not a factory acceptance record",
            "test_exit": fixture["test_exit"],
        },
        "reproduce": (
            ".venv/bin/python scripts/live_review_fixture.py --run "
            f"--provider-profile {reviewer_profile} --out {out}"
        ),
    }
    (out / "live-review-run.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return summary


def cleanup_containers(name: str = "factory-reviewer") -> None:
    from factory import policy

    try:
        ids = policy.find_containers({"hermes-agent": "1", "hermes-profile": name})
    except policy.PolicyViolation:
        return
    for container_id in ids:
        try:
            policy.remove_own_container(container_id, expected_profile=name)
        except policy.PolicyViolation:
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="actually execute")
    parser.add_argument(
        "--scratch",
        default=str(
            Path.home() / ".hermes/profiles/factory-runner/cache/scratch/lg-live-review"
        ),
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs/langgraph-hybrid/evidence/reviewer-sandbox"),
    )
    parser.add_argument("--provider-profile", default="factory-runner")
    parser.add_argument("--review-provider", default="")
    parser.add_argument("--review-base-url", default="")
    parser.add_argument("--review-model", default="")
    parser.add_argument("--review-api-mode", default="")
    parser.add_argument("--reviewer-label", default="factory-qa")
    parser.add_argument("--image", default="")
    parser.add_argument("--hermes", default="hermes")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if not args.run:
        print(
            "would build a disposable candidate/evidence fixture, execute the "
            f"R2-A attestation and one live review into {args.out}"
        )
        return 0
    scratch = Path(args.scratch).expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    # One fixture tree per run; the sandbox root is kept so the disposable
    # runtime bootstrap is not repeated.
    run_root = scratch / time.strftime("run-%Y%m%dT%H%M%S")
    run_root.mkdir(parents=True, exist_ok=False)
    fixture = build_fixture(run_root)
    try:
        summary = run_review(args, fixture)
    except OrchestrationError as exc:
        print(
            json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}),
            file=sys.stderr,
        )
        return 1
    finally:
        cleanup_containers()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["violations"] == [] else 2


if __name__ == "__main__":
    raise SystemExit(main())
