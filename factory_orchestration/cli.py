"""Operator interface: ``run``, ``inspect``, ``resume``, ``cancel``, ``export``.

Transport is a local Python CLI over the state directory; no daemon, socket or
second scheduler exists. ``resume`` refuses unless it names the hold it resolves
and the caller is the current Hermes attempt.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

from .adapters.gates import InstalledLauncherGate
from .adapters.hermes import (
    HermesBoard,
    HermesReviewer,
    HermesWorker,
    NoReadonlyEnforcement,
    read_profile_model_config,
    read_profile_provider_env,
)
from .adapters.installed import default_launcher, discover
from .errors import OrchestrationError
from .graph import Deps
from .runner import cancel, drive, export_run, inspect_run, resume, write_export
from .state import Attempt, RunAssignment
from .store import RunStore


def _review_max_turns(value: str) -> int:
    try:
        turns = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "review max turns must be an integer from 1 to 200"
        ) from exc
    if not 1 <= turns <= 200:
        raise argparse.ArgumentTypeError(
            "review max turns must be an integer from 1 to 200"
        )
    return turns


def _add_review_options(parser: argparse.ArgumentParser) -> None:
    """Explicit opt-in for the enforced reviewer sandbox.

    The default is the refusal attestor: a live review only runs when the
    operator names the sandbox and the executed attestation proves it.
    """

    parser.add_argument(
        "--reviewer-sandbox",
        action="store_true",
        help="run independent review in the attested Docker sandbox (default: refuse)",
    )
    parser.add_argument(
        "--reviewer-root",
        default="",
        help="factory-owned disposable reviewer root (default: <state-dir>/reviewer-sandbox)",
    )
    parser.add_argument(
        "--reviewer-image",
        default="",
        help="reviewer image tag; resolved to a repo digest before use",
    )
    parser.add_argument(
        "--review-provider-profile",
        default="factory-runner",
        help="profile whose model endpoint/provider credentials the sandbox reuses",
    )
    parser.add_argument("--review-provider", default="")
    parser.add_argument("--review-base-url", default="")
    parser.add_argument("--review-model", default="")
    parser.add_argument("--review-api-mode", default="")
    parser.add_argument(
        "--review-max-turns",
        type=_review_max_turns,
        default=40,
        help="independent reviewer turn budget, 1–200 (default: 40)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-orchestrate",
        description=(
            "Opt-in LangGraph orchestration for one bounded factory card. "
            "Hermes Kanban remains the scheduler and lifecycle owner."
        ),
    )
    parser.add_argument("--state-dir", required=True, help="durable state directory")
    parser.add_argument("--launcher", default=str(default_launcher()))
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="admit and drive one enrolled card")
    run.add_argument("--assignment", required=True, help="RunAssignment JSON file")
    run.add_argument("--artifacts", help="artifact directory (defaults to state dir)")
    run.add_argument("--card", help="Kanban card id (defaults to assignment)")
    run.add_argument("--attempt-run-id", type=int, help="Hermes run id")
    run.add_argument("--attempt-profile", help="Hermes assignee profile")
    run.add_argument("--claim-lock", default="")
    _add_review_options(run)

    for name, help_text in (
        ("inspect", "show stage, attempt, effects, evidence and next action"),
        ("export", "write the full run record as JSON"),
        ("cancel", "request cancellation and reconcile owned effects"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("card", help="Kanban card id")
        if name == "export":
            command.add_argument("--output", required=True)
        if name == "cancel":
            command.add_argument("--by", default="operator")
            command.add_argument("--reason", default="")

    resume_command = sub.add_parser("resume", help="resume a paused run")
    resume_command.add_argument("card")
    resume_command.add_argument(
        "--payload",
        default=None,
        help=(
            "resume JSON file; omit to adopt the pending operation's durable "
            "receipt (a finished child is proved from its own transcript)"
        ),
    )
    resume_command.add_argument(
        "--resolved-hold", required=True, help="the hold/approval being resolved"
    )
    resume_command.add_argument("--attempt-run-id", type=int)
    resume_command.add_argument("--attempt-profile")
    resume_command.add_argument("--board", default=None)
    resume_command.add_argument(
        "--artifacts",
        default=None,
        help=(
            "artifact directory holding the durable worker/reviewer requests "
            "(default: the state directory, matching run's default)"
        ),
    )
    _add_review_options(resume_command)

    admission = sub.add_parser("admission", help="inspect or change the opt-in policy")
    admission.add_argument("--enable", action="store_true")
    admission.add_argument("--disable", action="store_true")
    admission.add_argument("--enroll", action="append", default=[])
    admission.add_argument("--unenroll", action="append", default=[])
    admission.add_argument(
        "--lock-timeout",
        type=float,
        default=5.0,
        help="maximum seconds to wait for admission.lock (default: 5)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = Path(args.state_dir).expanduser().resolve()
    try:
        store = RunStore(state_dir)
    except OSError as exc:
        print(f"factory-orchestrate: cannot open state dir: {exc}", file=sys.stderr)
        return 2
    with store:
        try:
            return _dispatch(args, store, state_dir)
        except OrchestrationError as exc:
            print(
                json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}),
                file=sys.stderr,
            )
            return 1


def _deps(args, store: RunStore, state_dir: Path) -> Deps:
    release = None
    try:
        release = discover(Path(args.launcher).parent.parent)
    except (OSError, ValueError):
        release = None
    if release is not None:
        fingerprint = release.fingerprint
        release_reference = release.reference
    else:
        fingerprint = "sha256:" + "0" * 64
        release_reference = f"uninstalled:{args.launcher}"
    artifacts = Path(getattr(args, "artifacts", None) or state_dir)
    return Deps(
        store=store,
        board=HermesBoard(board=getattr(args, "board", None)),
        worker=HermesWorker(artifact_root=artifacts),
        gate=InstalledLauncherGate(Path(args.launcher), artifacts),
        reviewer=_reviewer(args, store, artifacts),
        state_dir=state_dir,
        artifact_dir=artifacts,
        release_identity=(
            (release_reference, fingerprint, release.host_policy_sha256)
            if release
            else (release_reference, fingerprint, "sha256:" + "0" * 64)
        ),
    )


def _reviewer(args, store: RunStore, artifacts: Path) -> HermesReviewer:
    """Select the reviewer boundary. Refusal is the default, not a fallback."""

    worker = HermesWorker(artifact_root=artifacts)
    if not getattr(args, "reviewer_sandbox", False):
        return HermesReviewer(
            worker,
            NoReadonlyEnforcement(),
            max_turns=getattr(args, "review_max_turns", 40),
        )

    from .adapters.reviewer_sandbox import SandboxedReadonlyAttestor

    mode = read_profile_model_config(args.review_provider_profile)
    model = args.review_model or mode.get("default", "")
    base_url = args.review_base_url or mode.get("base_url", "")
    if not model or not base_url:
        raise OrchestrationError(
            "invalid_assignment",
            "reviewer sandbox needs a model and endpoint "
            "(--review-model/--review-base-url or a readable --review-provider-profile)",
        )
    root = (
        Path(args.reviewer_root).expanduser().resolve()
        if args.reviewer_root
        else (Path(store.state_dir) / "reviewer-sandbox").resolve()
    )
    attestor = SandboxedReadonlyAttestor(root=root, image=args.reviewer_image or "")
    return HermesReviewer(
        worker,
        attestor,
        max_turns=args.review_max_turns,
        review_provider=args.review_provider or mode.get("provider", ""),
        review_base_url=base_url,
        review_model=model,
        review_api_mode=args.review_api_mode or mode.get("api_mode", ""),
        provider_env=read_profile_provider_env(args.review_provider_profile),
    )


def _assignment_payload(payload: dict) -> dict:
    """The JSON boundary for a reviewed assignment file.

    JSON has no tuple type, but the versioned state requires tuples for the two
    argv/reference lists. Coerce exactly those fields (and only those) so a
    reviewed ``assignment.json`` can be admitted; anything else still goes
    through ``RunAssignment``'s validation unchanged.
    """

    converted = dict(payload)
    for name in ("acceptance_argv", "entire_references"):
        value = converted.get(name)
        if isinstance(value, list):
            converted[name] = tuple(value)
    return converted


def _dispatch(args, store: RunStore, state_dir: Path) -> int:
    if args.command == "admission":
        return _admission(args, state_dir)
    deps = _deps(args, store, state_dir)
    if args.command == "run":
        payload = json.loads(Path(args.assignment).read_text())
        release_reference, fingerprint, policy_digest = deps.release_identity
        payload.setdefault("engine_release_reference", release_reference)
        payload.setdefault("engine_fingerprint", fingerprint)
        payload.setdefault("host_policy_digest", policy_digest)
        assignment = RunAssignment(**_assignment_payload(payload))
        attempt = _attempt(args, assignment, store)
        result = drive(deps, assignment, attempt)
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        return 0
    card = args.card
    thread = _thread_for_card(store, card)
    if args.command == "inspect":
        print(
            json.dumps(inspect_run(deps, thread), indent=2, sort_keys=True, default=str)
        )
        return 0
    if args.command == "export":
        destination = write_export(args.output, export_run(deps, thread))
        print(json.dumps({"ok": True, "path": str(destination)}))
        return 0
    if args.command == "cancel":
        outcome = cancel(deps, thread, by=args.by, reason=args.reason)
        print(json.dumps(outcome, indent=2, sort_keys=True))
        return 0
    if args.command == "resume":
        run_row = store.run(thread)
        if run_row is None:
            raise OrchestrationError("invalid_assignment", f"no admitted run {thread}")
        attempt = Attempt(
            task_id=run_row["hermes_task_id"],
            run_id=int(args.attempt_run_id or run_row["hermes_run_id"]),
            profile=args.attempt_profile or run_row["profile"],
            claim_lock=run_row["claim_lock"],
        )
        payload = json.loads(Path(args.payload).read_text()) if args.payload else None
        result = resume(
            deps,
            thread,
            attempt,
            payload=payload,
            resolved_hold=args.resolved_hold,
        )
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        return 0
    return 2


def _admission(args, state_dir: Path) -> int:
    """Cooperate with other admission writers using the stable sidecar lock."""
    if set(args.enroll) & set(args.unenroll):
        raise OrchestrationError(
            "invalid_assignment", "cannot enroll and unenroll the same card"
        )
    if not math.isfinite(args.lock_timeout) or args.lock_timeout < 0:
        raise OrchestrationError(
            "invalid_assignment", "lock timeout must be finite and non-negative"
        )
    path = state_dir / "admission.json"
    try:
        with (state_dir / "admission.lock").open("a+") as lock:
            deadline = time.monotonic() + args.lock_timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OrchestrationError(
                            "workspace_conflict", "timed out waiting for admission.lock"
                        ) from None
                    time.sleep(min(0.05, remaining))
            try:
                payload = _read_admission_payload(path)
                updated = dict(payload)
                if args.enable:
                    updated["enabled"] = True
                if args.disable:
                    updated["enabled"] = False
                cards = set(payload.get("enrolled_cards", []))
                changed_cards = (cards | set(args.enroll)) - set(args.unenroll)
                if changed_cards != cards:
                    updated["enrolled_cards"] = sorted(changed_cards)
                if updated != payload:
                    _replace_admission(path, updated)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except (OSError, ValueError) as exc:
        raise OrchestrationError(
            "invalid_assignment", f"cannot update admission policy: {exc}"
        ) from exc
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def _read_admission_payload(path: Path) -> dict:
    """Read fresh under the writer lock, retaining fields unknown to this CLI."""
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return {"enabled": False, "enrolled_cards": []}
    if not isinstance(payload, dict):
        raise ValueError("admission policy must be a JSON object")
    cards = payload.get("enrolled_cards", [])
    if not isinstance(cards, list) or not all(isinstance(card, str) for card in cards):
        raise ValueError("enrolled_cards must be a list of card ids")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise ValueError("enabled must be a boolean")
    return payload


def _replace_admission(path: Path, payload: dict) -> None:
    """Publish a complete policy atomically and durably before unlocking."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".admission-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _attempt(args, assignment: RunAssignment, store: RunStore) -> Attempt:
    run_id = getattr(args, "attempt_run_id", None)
    profile = getattr(args, "attempt_profile", None)
    if run_id is None or profile is None:
        env = {
            "HERMES_KANBAN_TASK": assignment.card_id,
            "HERMES_KANBAN_RUN_ID": str(run_id or ""),
            "HERMES_PROFILE": profile or "",
        }
        try:
            return Attempt.from_env(env)
        except OrchestrationError as exc:
            raise OrchestrationError(
                "invalid_assignment",
                "run must execute inside a current Hermes attempt: "
                f"pass --attempt-run-id/--attempt-profile ({exc.detail})",
            ) from exc
    return Attempt(
        task_id=assignment.card_id,
        run_id=int(run_id),
        profile=profile,
        claim_lock=getattr(args, "claim_lock", "") or None,
    )


def _thread_for_card(store: RunStore, card: str) -> str:
    rows = [
        row for row in store.connection.execute("SELECT thread_id FROM runs").fetchall()
    ]
    for row in rows:
        if str(row["thread_id"]).endswith(f":{card}"):
            return str(row["thread_id"])
    raise OrchestrationError("invalid_assignment", f"no admitted run for card {card}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
