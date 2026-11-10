#!/usr/bin/env python3
"""Real-card pilot harness for the opt-in LangGraph orchestration.

This is the *installed, frozen* harness: it is part of the staged engine
release, so a pilot attempt runs exactly the revision it names. It is executed by
the claimed factory-runner worker of a pilot card, under that card's own current
dispatcher grant.

Properties that matter (each one fixed a real defect found on the first pilot):

* One attempt = one new run root. The root is created fresh and never deleted or
  reset, so failures stay inspectable and two attempts cannot collide. The
  status file and log live in that root; nothing the harness writes is inside a
  directory it rebuilds.
* stdout *and* stderr are parsed, because the CLI prints structured refusals to
  stderr.
* The harness waits only on the operation key the graph is actually awaiting
  (worker request or review result), never on "any transcript".
* ``effect_unknown`` is a safety hold: the harness stops and reports it. It never
  launches, replays or resubmits a worker.
* The summary always carries an explicit ``exit`` and ``final_status``.
* The acceptance gate really executes the candidate: python:3.12-slim runs the
  candidate's own unittest suite over a read-only ./src mount, after checking the
  worker's commit out in the subject repository.
* The first acceptance invocation is a deliberate, recorded fault injection so
  one bounded repair cycle is exercised. It consumes itself exactly once.

No credential is read, printed or written. Every path is under --run-root except
the disposable reviewer sandbox and the subject repository, which are also
created under it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SPEC_TEXT = """# Pilot card: implement normalize()

In `src/calc.py`, add `normalize(value)` that returns `value.strip()` and keep
the module docstring. Do not change `double()`.

`src/test_calc.py` already tests it. Make `python3 -m unittest discover -s src`
pass from the workspace root. Commit the change on the current branch and leave
the workspace clean.

If acceptance rejects your candidate, make one further bounded improvement (add
a test asserting `normalize("") == ""`) and commit again. Never rewrite history
and never touch files outside the workspace.
"""

COMPOSE_TEXT = (
    "services:\n"
    "  test:\n"
    "    image: python:3.12-slim\n"
    "    working_dir: /work\n"
    "    volumes:\n"
    '      - "./src:/work:ro"\n'
    '    command: ["python", "-m", "unittest", "discover", "-s", ".", "-v"]\n'
)

CALC_TEXT = (
    '"""Tiny arithmetic helpers."""\n\n\n'
    "def double(value):\n"
    '    """Return twice ``value``."""\n'
    "    return value * 2\n"
)

TEST_TEXT = (
    "import unittest\n\nfrom calc import double, normalize\n\n\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_double(self):\n"
    "        self.assertEqual(double(2), 4)\n\n"
    "    def test_normalize(self):\n"
    '        self.assertEqual(normalize("  x  "), "x")\n\n\n'
    'if __name__ == "__main__":\n'
    "    unittest.main()\n"
)


def now() -> float:
    return time.time()


class Pilot:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path(args.run_root).expanduser().resolve()
        self.launcher = Path(args.launcher).expanduser().resolve()
        self.staged = self.launcher.parent.parent
        self.repo = self.root / "repo"
        self.workspace = self.root / "workspace"
        self.artifacts = self.root / "artifacts"
        self.state = self.root / "state"
        self.evidence = self.root / "evidence"
        self.spec = self.root / "spec.md"
        self.assignment = self.root / "assignment.json"
        self.status_path = self.root / "status.json"
        self.log_path = self.root / "pilot.log"
        self.inject_marker = self.root / "inject-first-acceptance-failure"
        self.card = args.card
        self.run_id = str(args.attempt_run_id)
        self.profile = args.attempt_profile
        self.claim_lock = args.claim_lock or ""
        self.summary: dict = {"steps": []}

    # -- persistence -------------------------------------------------------
    def write_status(self, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )

    def record_step(self, name: str, payload: dict) -> None:
        directory = self.evidence / "steps"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )

    # -- subprocess --------------------------------------------------------
    def sh(self, argv, *, cwd=None, timeout=900):
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        done = self.sh(["git", *args], cwd=cwd, timeout=120)
        if done.returncode != 0:
            raise SystemExit(f"git {' '.join(args)} failed: {done.stderr[-800:]}")
        return done.stdout.strip()

    def cli(self, *args: str, timeout: int = 1800):
        return self.sh(
            [
                str(self.launcher),
                "orchestrate",
                "--state-dir",
                str(self.state),
                "--launcher",
                str(self.launcher),
                *args,
            ],
            timeout=timeout,
        )

    @staticmethod
    def parse(completed) -> dict:
        """Parse stdout, then stderr: the CLI prints refusals to stderr."""

        for stream in (completed.stdout, completed.stderr):
            text = (stream or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if isinstance(payload, dict):
                return payload
        return {}

    # -- preparation -------------------------------------------------------
    def check_grant(self) -> None:
        for name, value in (
            ("--card", self.card),
            ("--attempt-run-id", self.run_id),
            ("--attempt-profile", self.profile),
        ):
            if not value:
                raise SystemExit(f"{name} is required")
        if not self.claim_lock:
            raise SystemExit("--claim-lock is required (this attempt's own grant)")

    def prepare(self) -> tuple[str, Path]:
        if self.root.exists():
            raise SystemExit(
                f"run root already exists and is never reused: {self.root}"
            )
        self.root.mkdir(parents=True)
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "calc.py").write_text(CALC_TEXT)
        (self.repo / "src" / "test_calc.py").write_text(TEST_TEXT)
        (self.repo / "compose.yml").write_text(COMPOSE_TEXT)
        (self.repo / ".env.example").write_text("PILOT_MARKER=1\n")
        (self.repo / ".gitignore").write_text(
            "__pycache__/\n*.pyc\n.coverage\ncoverage.json\n.pytest_cache/\n*.log\n"
        )
        self.git("init", "-q", "-b", "main", cwd=self.repo)
        self.git("add", "-A", cwd=self.repo)
        self.git(
            "-c",
            "user.email=pilot@example.com",
            "-c",
            "user.name=pilot",
            "commit",
            "-qm",
            "base: normalize() is missing",
            cwd=self.repo,
        )
        base = self.git("rev-parse", "HEAD", cwd=self.repo)
        self.git(
            "worktree",
            "add",
            "-q",
            "-b",
            "work",
            str(self.workspace),
            base,
            cwd=self.repo,
        )
        self.spec.write_text(SPEC_TEXT)
        self.inject_marker.write_text("deliberate acceptance failure for one repair\n")
        wrapper = self.write_acceptance_wrapper()
        return base, wrapper

    def write_acceptance_wrapper(self) -> Path:
        wrapper = self.root / "acceptance.py"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            "from pathlib import Path\n\n"
            f"REPO = Path({str(self.repo)!r})\n"
            f"WORKSPACE = Path({str(self.workspace)!r})\n"
            f"LAUNCHER = Path({str(self.launcher)!r})\n"
            f"ARTIFACTS = Path({str(self.artifacts)!r})\n"
            f"INJECT = Path({str(self.inject_marker)!r})\n"
            f"RECORD = Path({str(self.root / 'repair-injection.json')!r})\n"
            "if INJECT.exists():\n"
            "    INJECT.unlink()\n"
            "    RECORD.write_text('injected acceptance failure consumed\\n')\n"
            "    sys.stderr.write('injected acceptance failure: repair required\\n')\n"
            "    raise SystemExit(1)\n"
            "candidate = subprocess.run(\n"
            "    ['git', '-C', str(WORKSPACE), 'rev-parse', 'HEAD'],\n"
            "    capture_output=True, text=True, check=True,\n"
            ").stdout.strip()\n"
            "checkout = subprocess.run(\n"
            "    ['git', '-C', str(REPO), '-c', 'advice.detachedHead=false',\n"
            "     'checkout', '-q', '--detach', candidate],\n"
            "    capture_output=True, text=True,\n"
            ")\n"
            "if checkout.returncode != 0:\n"
            "    sys.stderr.write(checkout.stderr)\n"
            "    raise SystemExit(1)\n"
            "sys.exit(subprocess.run([\n"
            "    str(LAUNCHER), 'run', '--card', sys.argv[1], '--repo', str(REPO),\n"
            "    '--compose', 'compose.yml', '--test-service', 'test',\n"
            "    '--artifacts-root', str(ARTIFACTS),\n"
            "]).returncode)\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def assignment_payload(self, base: str, wrapper: Path, spec_digest: str) -> dict:
        return {
            "board_id": "software-factory",
            "card_id": self.card,
            "repository": str(self.repo),
            "workspace_path": str(self.workspace),
            "immutable_base_sha": base,
            "spec_reference": str(self.spec),
            "spec_digest": spec_digest,
            "template_id": "bounded-card-v1",
            "worker_profile": self.profile,
            "reviewer_profile": "factory-qa",
            "acceptance_argv": [str(wrapper), self.card],
            "acceptance_artifacts": str(self.artifacts),
            "max_repairs": 2,
        }

    # -- operation-scoped waiting -----------------------------------------
    def operation_paths(self, awaiting: dict) -> tuple[str, Path | None]:
        kind = str(awaiting.get("kind") or "")
        key = str(awaiting.get("operation_key") or "")
        if not key:
            return kind, None
        safe = key.replace(":", "_")
        if kind == "await_worker":
            return kind, self.artifacts / "hermes-worker" / safe / "stream.jsonl"
        if kind == "await_review":
            return kind, self.artifacts / "hermes-reviewer" / safe
        return kind, None

    def operation_finished(self, awaiting: dict) -> bool:
        kind, path = self.operation_paths(awaiting)
        if path is None:
            return False
        if kind == "await_review":
            return (path / "review-result.json").is_file()
        if not path.is_file():
            return False
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return True
        return False

    def wait_operation(self, awaiting: dict, timeout: int) -> bool:
        deadline = now() + timeout
        while now() < deadline:
            if self.operation_finished(awaiting):
                return True
            time.sleep(5)
        return False

    # -- driving -----------------------------------------------------------
    def review_args(self) -> list[str]:
        return [
            "--artifacts",
            str(self.artifacts),
            "--attempt-run-id",
            self.run_id,
            "--attempt-profile",
            self.profile,
            "--reviewer-sandbox",
            "--review-provider-profile",
            self.profile,
            "--reviewer-root",
            str(self.root / "reviewer-sandbox"),
        ]

    def run_args(self) -> list[str]:
        return [
            "run",
            "--assignment",
            str(self.assignment),
            *self.review_args(),
            "--claim-lock",
            self.claim_lock,
        ]

    def record(self, name: str, completed, extra: dict | None = None) -> dict:
        payload = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-20000:],
            "stderr": completed.stderr[-8000:],
        }
        if extra:
            payload.update(extra)
        self.record_step(name, payload)
        return self.parse(completed)

    def execute(self) -> int:
        self.check_grant()
        self.write_status({"finished": False, "exit": None, "phase": "preparing"})
        base, wrapper = self.prepare()
        spec_digest = "sha256:" + hashlib.sha256(self.spec.read_bytes()).hexdigest()
        self.assignment.write_text(
            json.dumps(
                self.assignment_payload(base, wrapper, spec_digest),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        self.summary.update(
            {
                "card": self.card,
                "hermes_run_id": self.run_id,
                "worker_profile": self.profile,
                "claim_lock": self.claim_lock,
                "launcher": str(self.launcher),
                "staged_release": str((self.staged / ".runtime/current").resolve()),
                "base_sha": base,
                "spec_digest": spec_digest,
                "run_root": str(self.root),
                "started_at": now(),
            }
        )
        self.write_status({"finished": False, "exit": None, "phase": "admission"})
        admission = self.cli(
            "admission", "--enable", "--enroll", self.card, timeout=120
        )
        admission_payload = self.record("00-admission", admission)
        if admission.returncode != 0:
            return self.finish("admission_failed", 1, admission_payload)
        self.summary["admission"] = admission_payload

        self.write_status({"finished": False, "exit": None, "phase": "first_run"})
        first = self.cli(*self.run_args())
        payload = self.record("01-run", first)
        self.summary["steps"].append(
            {
                "step": "run",
                "exit_code": first.returncode,
                "status": payload.get("status"),
                "stage": payload.get("stage"),
            }
        )
        if first.returncode != 0:
            return self.finish("first_run_refused", 1, payload)
        if payload.get("status") != "awaiting_worker":
            return self.finish(
                "first_run_did_not_await",
                1 if payload.get("status") != "ready_for_authorized_completion" else 0,
                payload,
            )
        awaiting = payload.get("awaiting") or {}
        requests_after_submit = self.worker_request_count()
        self.summary["worker_requests_after_submit"] = requests_after_submit

        # Fail-closed proofs while the real worker is live.
        duplicate = self.cli(*self.run_args())
        duplicate_payload = self.record(
            "02-duplicate-run",
            duplicate,
            {"worker_requests": self.worker_request_count()},
        )
        self.summary["duplicate_run_created_no_second_worker"] = (
            duplicate.returncode == 0
            and duplicate_payload.get("status") == "awaiting_worker"
            and self.worker_request_count() == requests_after_submit
        )

        premature = self.cli(
            "resume",
            self.card,
            "--resolved-hold",
            "worker request awaited",
            *self.review_args(),
            timeout=300,
        )
        premature_payload = self.record(
            "03-premature-resume",
            premature,
            {"worker_requests": self.worker_request_count()},
        )
        self.summary["premature_resume_refused"] = (
            premature.returncode == 1
            and premature_payload.get("reason") == "effect_unknown"
            and self.worker_request_count() == requests_after_submit
            and not self.operation_finished(awaiting)
        )

        # Adopt the persisted hold exactly once per completed operation.
        hold = awaiting
        for attempt in range(8):
            self.write_status(
                {
                    "finished": False,
                    "exit": None,
                    "phase": f"waiting_{hold.get('kind')}",
                    "repair_index": attempt,
                }
            )
            if not self.wait_operation(hold, timeout=1800):
                return self.finish("operation_never_finished", 1, {"awaiting": hold})
            resumed = self.cli(
                "resume",
                self.card,
                "--resolved-hold",
                "pending operation finished",
                *self.review_args(),
            )
            payload = self.record(f"resume-{attempt + 1}", resumed)
            self.summary["steps"].append(
                {
                    "step": f"resume_{attempt + 1}",
                    "exit_code": resumed.returncode,
                    "status": payload.get("status"),
                    "stage": payload.get("stage"),
                    "reason": payload.get("reason"),
                }
            )
            if resumed.returncode != 0:
                reason = str(payload.get("reason") or "")
                if reason == "effect_unknown":
                    # A safety hold: never launch or replay a worker here.
                    return self.finish("effect_unknown_hold", 1, payload)
                return self.finish(f"resume_refused_{reason or 'unknown'}", 1, payload)
            status = str(payload.get("status") or "")
            if status in {"awaiting_worker", "awaiting_review"}:
                hold = payload.get("awaiting") or {}
                continue
            return self.finalize(status)

        return self.finish("resume_budget_exhausted", 1, {"awaiting": hold})

    def worker_request_count(self) -> int:
        directory = self.artifacts / "hermes-worker"
        return len(list(directory.glob("*/request.json"))) if directory.is_dir() else 0

    # -- completion --------------------------------------------------------
    def finalize(self, status: str) -> int:
        inspect = self.cli("inspect", self.card, timeout=300)
        inspect_payload = self.record("90-inspect", inspect)
        export = self.cli(
            "export",
            self.card,
            "--output",
            str(self.root / "run-export.json"),
            timeout=300,
        )
        self.record_step(
            "91-export",
            {
                "exit_code": export.returncode,
                "stdout": export.stdout[-4000:],
                "stderr": export.stderr[-2000:],
            },
        )
        state = inspect_payload.get("state") or {}
        candidate = state.get("candidate_sha") or ""
        evidence_path = state.get("evidence_reference") or ""
        self.summary.update(
            {
                "final_status": status,
                "state_status": state.get("status"),
                "candidate_sha": candidate,
                "repair_index": state.get("repair_index"),
                "evidence_reference": evidence_path,
                "approval_reference": state.get("approval_reference"),
                "metrics": inspect_payload.get("metrics"),
                "operations": [
                    {
                        k: record.get(k)
                        for k in ("operation_key", "kind", "state", "repair_index")
                    }
                    for record in (inspect_payload.get("operations") or [])
                ],
                "next_action": inspect_payload.get("next_action"),
                "events": [
                    event.get("kind") for event in (inspect_payload.get("events") or [])
                ][-40:],
                "inspect_exit": inspect.returncode,
                "export_exit": export.returncode,
            }
        )
        if candidate and evidence_path:
            for label, cand in (
                ("evidence_positive", candidate),
                ("stale_evidence_negative", "f" * 40),
            ):
                done = self.sh(
                    [
                        str(self.launcher),
                        "verify",
                        "--artifacts-dir",
                        evidence_path,
                        "--repo",
                        str(self.repo),
                        "--candidate",
                        cand,
                        "--base",
                        self.summary["base_sha"],
                        "--json",
                    ],
                    timeout=300,
                )
                parsed = self.parse(done)
                self.summary[label] = {
                    "exit_code": done.returncode,
                    "reason": parsed.get("reason"),
                }
        ok = (
            status == "ready_for_authorized_completion"
            and self.summary.get("evidence_positive", {}).get("exit_code") == 0
            and self.summary.get("stale_evidence_negative", {}).get("exit_code") == 1
        )
        return self.finish(status or "unknown", 0 if ok else 1, {})

    def finish(self, phase: str, exit_code: int, extra: dict) -> int:
        self.summary.update(
            {
                "phase": phase,
                "exit": exit_code,
                "finished_at": now(),
                "repair_injection_consumed": (
                    self.root / "repair-injection.json"
                ).is_file(),
            }
        )
        self.summary["elapsed_s"] = round(
            self.summary["finished_at"] - self.summary.get("started_at", now()), 1
        )
        if extra:
            self.summary["detail"] = extra
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "pilot-summary.json").write_text(
            json.dumps(self.summary, indent=2, sort_keys=True) + "\n"
        )
        self.write_status(
            {
                "finished": True,
                "exit": exit_code,
                "final_status": self.summary.get("final_status", ""),
                "phase": phase,
                "summary": str(self.evidence / "pilot-summary.json"),
            }
        )
        print(json.dumps(self.summary, indent=2, sort_keys=True, default=str)[:8000])
        return exit_code


def detach(argv: list[str]) -> int:
    """Start the pilot in its own session; the run root is created by the child."""

    known = argparse.ArgumentParser(add_help=False)
    known.add_argument("--run-root", required=True)
    parsed, _ = known.parse_known_args(argv)
    root = Path(parsed.run_root).expanduser().resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    log = (root.parent / f"{root.name}.log").open("ab")
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--execute", *argv],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(root.parent),
    )
    log.close()
    print(
        json.dumps(
            {
                "detached": True,
                "pid": child.pid,
                "run_root": str(root),
                "status": str(root / "status.json"),
                "log": str(root.parent / f"{root.name}.log"),
            },
            indent=2,
        )
    )
    return 0


def status(argv: list[str]) -> int:
    known = argparse.ArgumentParser(add_help=False)
    known.add_argument("--run-root", required=True)
    parsed, _ = known.parse_known_args(argv)
    path = Path(parsed.run_root).expanduser().resolve() / "status.json"
    print(path.read_text() if path.is_file() else '{"finished": false}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root", required=True, help="unique, never-reused attempt directory"
    )
    parser.add_argument(
        "--launcher", required=True, help="staged installation's bin/factory"
    )
    parser.add_argument("--card", default=os.environ.get("HERMES_KANBAN_TASK", ""))
    parser.add_argument(
        "--attempt-run-id", default=os.environ.get("HERMES_KANBAN_RUN_ID", "")
    )
    parser.add_argument(
        "--attempt-profile", default=os.environ.get("HERMES_PROFILE", "")
    )
    parser.add_argument(
        "--claim-lock", default=os.environ.get("HERMES_KANBAN_CLAIM_LOCK", "")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--status" in argv:
        return status(argv)
    if "--execute" not in argv:
        return detach(argv)
    argv = [item for item in argv if item != "--execute"]
    args = build_parser().parse_args(argv)
    pilot = Pilot(args)
    try:
        return pilot.execute()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - the status file is the interface
        pilot.finish(
            f"harness_error:{type(exc).__name__}", 1, {"error": str(exc)[:2000]}
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
