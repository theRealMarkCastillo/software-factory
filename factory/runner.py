"""
Factory Compose execution wrapper.

This is the ONLY code path allowed to actually build/run/teardown Docker
Compose environments for factory cards. It sits on top of factory.policy
and adds no privilege the policy module doesn't already enforce -- it
just sequences preflight -> run -> postflight -> cleanup, and guarantees
cleanup runs even on failure.

Usage (CLI):
    python3 -m factory.runner \
        --card t_abc123 \
        --repo /path/to/repo \
        --compose compose.factory.yml \
        --test-service test \
        --timeout 300

Path arguments (--compose, --env-file, --env-example) are repo-relative by
default (e.g. --compose docker-compose.yml for a file in the repo root). An
absolute path, or a path relative to the current working directory that
already exists, is used as-is instead of being re-joined onto --repo.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from factory.audit import run_dependency_audit
from factory.coverage import _diff_added_lines, run_coverage_gate
from factory.datamodel import DATA_MODEL_FILENAME, run_data_model_gate
from factory.evidence import (
    EvidenceError,
    assert_assembled_evidence,
    write_evidence_manifest,
)
from factory.policy import (
    PolicyViolation,
    ProtectedProjectSnapshot,
    assert_safe_compose_file,
    assert_safe_repo_root,
    new_factory_project,
    run_compose,
    teardown,
)


@dataclass
class RunResult:
    card_id: str
    project_name: str
    exit_code: int | None = None
    passed: bool = False
    stage: str = "not_started"
    error: str | None = None
    artifacts_dir: str | None = None
    duration_s: float = 0.0
    collateral_damage: bool = False
    gates: dict = field(default_factory=dict)
    gates_run: bool = True
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "project_name": self.project_name,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "stage": self.stage,
            "error": self.error,
            "artifacts_dir": self.artifacts_dir,
            "duration_s": round(self.duration_s, 2),
            "collateral_damage": self.collateral_damage,
            "gates": self.gates,
            "gates_run": self.gates_run,
            "log": self.log,
        }


def _gate_dict(outcome) -> dict:
    """Normalize a gate result to a plain dict for RunResult/manifest folding."""
    if isinstance(outcome, dict):
        return outcome
    to_dict = getattr(outcome, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        return converted if isinstance(converted, dict) else {"raw": str(converted)}
    return {"raw": str(outcome)}


def _diff_has_added_python(repo: Path, base: str) -> bool:
    """True when the diff vs `base` adds at least one python line.

    If the diff cannot be computed (not a git repo, base missing), we err on
    the side of running the gate rather than skipping it.
    """
    added, _err = _diff_added_lines(repo, base)
    if added is None:
        return True
    return any(added.values())


def _run_preflight_gates(repo: Path, result: RunResult) -> bool:
    """Run the audit/coverage/datamodel gates and record their results.

    Returns True when all gates pass. Never raises: a gate that throws is
    itself a fail-closed condition. Called BEFORE anything touches Docker.

    Each gate records its full result on ``result.gates`` (folding into the
    evidence manifest happens in ``run_factory_card``). Failure is per-gate:
    a failing gate stops the pipeline at its own stage and Docker never runs.
    """
    # 1. Dependency audit gate. venv_path points at what ACTUALLY runs when a
    #    repo-local .venv exists; a confirmed vulnerability fails closed.
    venv_path = str(repo / ".venv") if (repo / ".venv").is_dir() else ""
    try:
        audit = run_dependency_audit(str(repo), venv_path=venv_path)
        result.gates["audit"] = _gate_dict(audit)
    except Exception as exc:  # noqa: BLE001 - gate crash is fail-closed evidence
        result.gates["audit"] = {
            "status": "error",
            "passed": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    if result.gates["audit"].get("status") not in (
        "pass",
        "unavailable",
        "no_manifest",
    ):
        result.stage = "audit_gate"
        result.passed = False
        result.error = f"audit gate failed: {result.gates['audit'].get('detail')}"
        return False
    if result.gates["audit"].get("status") == "unavailable":
        result.log.append(
            f"dependency audit unavailable (warning): {result.gates['audit'].get('detail')}"
        )
    # pass / no_manifest proceed silently.

    # 2. Coverage delta gate. Skipped entirely (a log line, not a pass) when
    #    the diff vs base adds no python lines -- nothing new to measure.
    base = "main"
    if _diff_has_added_python(repo, base):
        try:
            cov = run_coverage_gate(str(repo), base=base, require_fresh=True)
            result.gates["coverage"] = _gate_dict(cov)
        except Exception as exc:  # noqa: BLE001 - gate crash is fail-closed evidence
            result.gates["coverage"] = {
                "status": "error",
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        if not result.gates["coverage"].get("passed", False):
            result.stage = "coverage_gate"
            result.passed = False
            result.error = (
                f"coverage gate failed: {result.gates['coverage'].get('detail')}"
            )
            return False
    else:
        result.gates["coverage"] = {"status": "skipped", "reason": "no_added_python"}
        result.log.append(f"coverage gate skipped: no added python lines vs {base!r}")

    # 3. Data-model gate: run only when a data-model.yml is present.
    if (repo / DATA_MODEL_FILENAME).is_file():
        try:
            dm = run_data_model_gate(str(repo))
            result.gates["datamodel"] = _gate_dict(dm)
        except Exception as exc:  # noqa: BLE001 - gate crash is fail-closed evidence
            result.gates["datamodel"] = {
                "status": "error",
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        if result.gates["datamodel"].get("status") != "pass":
            result.stage = "datamodel_gate"
            result.passed = False
            result.error = (
                f"data-model gate failed: {result.gates['datamodel'].get('detail')}"
            )
            return False
    else:
        result.gates["datamodel"] = {"status": "skipped", "reason": "no_data_model"}
        result.log.append(f"data-model gate skipped: no {DATA_MODEL_FILENAME}")

    return True


def _resolve_input_path(
    repo: Path, raw: str, kind: str, *, required: bool = True
) -> Path:
    """Resolve a runner input path that may be repo-relative or as-given.

    ``raw`` is the path exactly as supplied by the caller. It is first tried
    repo-relative (``repo / raw``, the documented default), then as-given
    (absolute, or relative to the current working directory). When ``required``
    and neither interpretation exists, raise a PolicyViolation naming both
    tried paths and clarifying that compose/env paths are repo-relative. When
    ``required`` is False (used for optional env files), fall back to the
    repo-relative interpretation even if it does not exist so the caller can
    record its absence.
    """
    raw_path = Path(raw)
    repo_joined = repo / raw_path
    candidates = [repo_joined, raw_path]
    for cand in candidates:
        try:
            if cand.is_file():
                return cand.resolve()
        except OSError:
            continue
    if required:
        tried = ", ".join(str(c) for c in candidates)
        raise PolicyViolation(
            f"{kind} not found under either interpretation (tried: {tried}). "
            f"{kind} paths are repo-relative; pass a path relative to the "
            f"repo root (e.g. docker-compose.yml) or an absolute path."
        )
    return (repo / raw_path).resolve()


def _load_compose_files(
    compose_files: list[Path], repo_root: Path | None = None
) -> dict:
    """Validate every input, then merge mappings for final policy validation.

    Validation is deliberately conservative: overrides cannot legalize an
    unsafe declaration in an earlier file. This avoids relying on an
    incomplete reimplementation of Compose's sequence and reset semantics.

    ``compose_files`` must already be resolved absolute paths (see
    ``_resolve_input_path``).
    """
    if yaml is None:
        raise PolicyViolation("pyyaml is required to statically validate compose files")
    merged: dict = {"services": {}}

    def merge(left: dict, right: dict) -> dict:
        out = dict(left)
        for key, value in right.items():
            if isinstance(out.get(key), dict) and isinstance(value, dict):
                out[key] = merge(out[key], value)
            else:
                out[key] = value
        return out

    for full in compose_files:
        if not full.is_file():
            raise PolicyViolation(f"compose file not found: {full}")
        with open(full) as f:
            doc = yaml.safe_load(f) or {}
        if not isinstance(doc, dict):
            raise PolicyViolation(f"compose document must be a mapping: {full}")
        assert_safe_compose_file(
            doc,
            repo_root=repo_root or compose_files[0].parent,
            require_services=False,
        )
        merged = merge(merged, doc)
    return merged


def run_factory_card(
    card_id: str,
    repo_path: str,
    compose_files: list[str],
    test_service: str = "test",
    timeout: int = 900,
    artifacts_root: str = "~/.hermes/kanban/factory-artifacts",
    entry_point: str = "docker compose test service",
    env_file: str = ".env",
    env_example: str = ".env.example",
    fixture_paths: list[str] | None = None,
    run_gates: bool = True,
) -> RunResult:
    """
    Full sanctioned lifecycle for a factory Compose job:
      1. Statically validate the compose config (policy.assert_safe_compose_file)
      1b. Run the pre-Docker gates (audit / coverage / data-model) and fail
          closed at the first failing gate -- Docker never builds on top of a
          gate that did not pass. Gate results are recorded on the RunResult
          and folded into the evidence manifest.
      2. Snapshot all host containers (preflight)
      3. Generate a unique factory-* project name
      4. docker compose up --build --abort-on-container-exit --exit-code-from <test_service>
      5. Collect logs to an artifacts directory
      6. ALWAYS tear down (finally block) -- own resources only
      7. Snapshot all host containers again (postflight) and classify changes
      8. A known protected-project change is a safety incident with a durable
         marker; unknown-host churn is still a failed collateral warning.
      9. Write and then enforce the evidence manifest: a green run whose
         manifest cannot pass assert_assembled_evidence is itself a failure.

    Intentional breaking-behavior change : the pre-Docker gates
    run by DEFAULT (``run_gates=True``). Previously the runner never
    invoked the gates at all, so a green result carried no audit/coverage/data-
    model guarantee. To opt out, pass ``run_gates=False`` (or ``--no-gates`` on
    the CLI); a disabled-gate run is visibly second-class and records
    ``gates_run: false`` in its result and evidence manifest.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise PolicyViolation(f"repo path does not exist: {repo}")
    # Reject a sensitive subject root before resolving any compose/env input or
    # touching Docker. This closes the repo_root=/etc reachability path.
    assert_safe_repo_root(repo)

    result = RunResult(card_id=card_id, project_name="")
    start = time.time()

    # 1. Static validation before anything touches Docker.
    result.stage = "validating_compose"
    resolved_compose = [
        _resolve_input_path(repo, f, "compose file") for f in compose_files
    ]
    merged_compose = _load_compose_files(resolved_compose, repo_root=repo)
    assert_safe_compose_file(merged_compose, repo_root=repo)
    if test_service not in merged_compose.get("services", {}):
        raise PolicyViolation(
            f"test_service '{test_service}' not found in compose services: "
            f"{list(merged_compose.get('services', {}).keys())}"
        )

    project_name = new_factory_project(card_id)
    result.project_name = project_name

    # 1b. Pre-Docker gates: fail closed BEFORE anything is built or run.
    result.gates_run = run_gates
    if not run_gates:
        result.log.append(
            "GATES SKIPPED (--no-gates): this run is second-class evidence "
            "and cannot certify an assembled-system claim on its own"
        )
    elif not _run_preflight_gates(repo, result):
        result.duration_s = time.time() - start
        return result

    abs_compose_files = [str(p) for p in resolved_compose]

    artifacts_dir = Path(artifacts_root).expanduser() / project_name
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    result.artifacts_dir = str(artifacts_dir)

    # 2. Preflight snapshot of protected projects.
    result.stage = "preflight_snapshot"
    before = ProtectedProjectSnapshot.capture()

    try:
        # 3/4. Build and run, treating the test service's exit code as authoritative.
        result.stage = "running"
        proc = run_compose(
            project_name,
            abs_compose_files,
            [
                "up",
                "--build",
                "--abort-on-container-exit",
                "--exit-code-from",
                test_service,
            ],
            timeout=timeout,
        )
        result.exit_code = proc.returncode
        result.passed = proc.returncode == 0

        # 5. Collect logs regardless of outcome.
        result.stage = "collecting_logs"
        (artifacts_dir / "up.stdout.log").write_text(proc.stdout or "")
        (artifacts_dir / "up.stderr.log").write_text(proc.stderr or "")

        logs_proc = run_compose(
            project_name, abs_compose_files, ["logs", "--no-color"], timeout=60
        )
        (artifacts_dir / "compose.log").write_text(logs_proc.stdout or "")

    except Exception as exc:  # noqa: BLE001 - a crashed/hung run still leaves evidence
        result.stage = "run_failed"
        result.error = str(exc)
        result.passed = False
        # Capture whatever partial output exists (e.g. TimeoutExpired carries
        # stdout/stderr) so a crashed/hung run still leaves evidence behind.
        partial_out = getattr(exc, "stdout", "") or ""
        partial_err = getattr(exc, "stderr", "") or ""
        if isinstance(partial_out, bytes):
            partial_out = partial_out.decode(errors="replace")
        if isinstance(partial_err, bytes):
            partial_err = partial_err.decode(errors="replace")
        try:
            if partial_out:
                (artifacts_dir / "up.stdout.log").write_text(partial_out)
            if partial_err:
                (artifacts_dir / "up.stderr.log").write_text(partial_err)
            (artifacts_dir / "RUN_FAILED.txt").write_text(
                f"{type(exc).__name__}: {exc}\n"
            )
        except OSError:
            pass  # artifact write is best-effort; the exception itself is recorded

    finally:
        # 6. Always tear down our own resources, no matter what happened above.
        # Don't overwrite an already-terminal stage ("run_failed") -- the stage
        # field must keep telling QC where the run actually died.
        if not result.stage.startswith(("SAFETY_INCIDENT", "COLLATERAL_WARNING")):
            result.stage = "tearing_down"
        try:
            teardown_proc = teardown(project_name, abs_compose_files)
            if teardown_proc.returncode != 0:
                warning = f"teardown failed with exit code {teardown_proc.returncode}"
                result.log.append(warning)
                result.error = result.error or warning
                result.passed = False
        except Exception as teardown_exc:  # noqa: BLE001 - teardown failure is reportable
            # Teardown failure is itself a reportable event, but we still
            # proceed to the postflight safety check -- that check is more
            # important than a clean stage transition.
            warning = f"teardown warning: {teardown_exc}"
            result.log.append(warning)
            result.error = result.error or warning
            result.passed = False

    # 7/8. Postflight safety verification -- this can override everything else.
    result.stage = "postflight_verification"
    try:
        after = ProtectedProjectSnapshot.capture()
    except PolicyViolation as exc:
        result.passed = False
        result.error = str(exc)
        result.stage = "postflight_failed"
        result.duration_s = time.time() - start
        (artifacts_dir / "POSTFLIGHT_FAILURE.txt").write_text(str(exc))
        return result

    collateral = before.collateral_damage_report(after)
    protected_changes = collateral["protected"]
    unexpected_changes = collateral["unexpected"]
    if protected_changes:
        detail = (
            "PROTECTED PROJECT STATE CHANGED -- factory run must be treated as a "
            "safety incident, not a normal failure:\n" + "\n".join(protected_changes)
        )
        result.collateral_damage = True
        result.passed = False
        result.error = detail
        result.stage = "SAFETY_INCIDENT"
        result.duration_s = time.time() - start
        (artifacts_dir / "SAFETY_INCIDENT.txt").write_text(detail)
        _write_danger_marker(card_id, project_name, detail, artifacts_root)
        return result
    if unexpected_changes:
        detail = (
            "UNEXPECTED CONTAINER STATE CHANGED -- factory run failed pending review:\n"
            + "\n".join(unexpected_changes)
        )
        result.collateral_damage = True
        result.passed = False
        result.error = detail
        result.stage = "COLLATERAL_WARNING"
        result.duration_s = time.time() - start
        result.log.append(detail)
        (artifacts_dir / "COLLATERAL_WARNING.txt").write_text(detail)
        return result

    result.stage = "complete" if result.passed else "complete_with_errors"
    result.duration_s = time.time() - start
    resolved_env_file = str(
        _resolve_input_path(repo, env_file, "env file", required=False)
    )
    resolved_env_example = str(
        _resolve_input_path(repo, env_example, "env example", required=False)
    )
    try:
        manifest_path = write_evidence_manifest(
            artifacts_dir,
            repo,
            entry_point=entry_point,
            command=[
                "docker",
                "compose",
                "up",
                "--build",
                "--abort-on-container-exit",
                "--exit-code-from",
                test_service,
            ],
            env_file=resolved_env_file,
            env_example=resolved_env_example,
            fixture_paths=fixture_paths or [],
            audit=result.gates.get("audit"),
            coverage=result.gates.get("coverage"),
            datamodel=result.gates.get("datamodel"),
            gates_run=result.gates_run,
            execution={
                "exit_code": result.exit_code,
                "passed": result.passed,
                "stage": result.stage,
            },
        )
    except Exception as exc:  # noqa: BLE001 - evidence failures must fail the run
        result.passed = False
        result.stage = "evidence_generation"
        result.error = f"evidence generation failed: {exc}"
        result.log.append(result.error)
        return result
    # Evidence enforcement: the written manifest must itself pass the
    # assembled-evidence check. A manifest that cannot certify the claim it
    # records fails the run -- even a green one. The manifest and logs remain
    # in the artifacts dir for diagnosis.
    manifest = json.loads(manifest_path.read_text())
    # Failed runs retain diagnostic evidence and their original failure;
    # only a candidate acceptance pass needs certification here.
    if not result.passed:
        return result
    try:
        assert_assembled_evidence(manifest)
    except EvidenceError as exc:
        result.passed = False
        result.error = f"evidence_enforcement: {exc}"
        result.stage = "evidence_enforcement"
        result.log.append(f"ASSEMBLED EVIDENCE REJECTED: {exc}")
        (artifacts_dir / "EVIDENCE_REJECTED.txt").write_text(
            f"assembled evidence rejected: {exc}\n"
        )
    return result


def _write_danger_marker(
    card_id: str, project_name: str, detail: str, artifacts_root: str
) -> None:
    """
    Durable HITL fail-fast marker. On a
    protected-resource DANGER, write a timestamped `.danger` file so that even
    if the Telegram gateway is unreachable and no live agent notices, a human
    (or a later sweep) sees a durable on-disk alert that a protected workload
    changed and the factory must NOT auto-clean or auto-retry it.

    Path: <artifacts root>/pending-human/<project>.danger
    """
    pending = Path(artifacts_root).expanduser() / "pending-human"
    pending.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    danger = pending / f"{project_name}.danger"
    danger.write_text(
        f"[{now}] SAFETY INCIDENT\n"
        f"card: {card_id}\n"
        f"project: {project_name}\n"
        f"detail: {detail}\n"
        f"ACTION REQUIRED: protected Docker resource changed. Do NOT "
        f"auto-clean, do NOT auto-retry. Investigate before any further "
        f"factory work on this host.\n"
    )


def run_artifact_retention(
    artifacts_root: str = "~/.hermes/kanban/factory-artifacts",
    keep: int = 10,
) -> dict:
    """
    Retain only the most recent `keep` artifact dirs (newest-first), delete the
    rest. GC-boundary safety: this function ONLY ever deletes directories
    directly under the artifacts root -- never a path that resolves outside it.

    Returns {'deleted': [names], 'kept': [names]} for the caller/report.
    """
    root = Path(artifacts_root).expanduser().resolve()
    if not root.is_dir():
        return {"deleted": [], "kept": [], "reason": "no artifact root yet"}

    # Safety: never follow symlinks out of the root, and refuse a root that
    # is itself a top-level home dir (belt-and-suspenders against a bad config).
    if root == Path.home().resolve() or root == Path("/").resolve():
        raise PolicyViolation(
            f"refusing to run artifact retention with unsafe root: {root}"
        )

    dirs = sorted(
        (d for d in root.iterdir() if d.is_dir() and not d.is_symlink()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,  # newest first
    )

    keep_count = max(0, int(keep))
    # NEVER GC the pending-human marker dir — it is the durable HITL fail-fast
    # alert. Deleting it would hide a protected-resource DANGER from a human.
    # It is excluded from both the delete set and keep-count pressure.
    delete_dirs = [d for d in dirs[keep_count:] if d.name != "pending-human"]

    deleted = []
    for d in delete_dirs:
        # Double-check each target is really inside root before nuking.
        if d.resolve().is_relative_to(root.resolve()):
            shutil.rmtree(d, ignore_errors=True)
            if d.exists():
                raise PolicyViolation(f"retention could not delete path: {d}")
            deleted.append(d.name)
        else:
            raise PolicyViolation(f"refusing retention delete of out-of-root path: {d}")

    return {
        "deleted": deleted,
        "kept": sorted(
            d.name for d in dirs if d.name not in {x.name for x in delete_dirs}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a factory Compose card safely.")
    parser.add_argument("--card", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--compose",
        action="append",
        required=True,
        dest="compose_files",
        help=(
            "Compose file path. Repo-relative by default (e.g. docker-compose.yml "
            "for the repo root); an absolute or cwd-relative path is also accepted."
        ),
    )
    parser.add_argument("--test-service", default="test")
    parser.add_argument("--entry-point", default="docker compose test service")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Env file path (repo-relative by default; absolute/cwd-relative accepted).",
    )
    parser.add_argument(
        "--env-example",
        default=".env.example",
        help="Env example path (repo-relative by default; absolute/cwd-relative accepted).",
    )
    parser.add_argument("--fixture", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--artifacts-root", default="~/.hermes/kanban/factory-artifacts"
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=None,
        metavar="N",
        help="Keep the newest N artifact dirs, delete older ones (default: off).",
    )
    parser.add_argument(
        "--no-gates",
        action="store_true",
        help="Skip the pre-Docker gates (audit/coverage/datamodel). The run is "
        "marked second-class in its result and evidence manifest.",
    )
    args = parser.parse_args(argv)

    result = run_factory_card(
        card_id=args.card,
        repo_path=args.repo,
        compose_files=args.compose_files,
        test_service=args.test_service,
        timeout=args.timeout,
        entry_point=args.entry_point,
        env_file=args.env_file,
        env_example=args.env_example,
        fixture_paths=args.fixture,
        run_gates=not args.no_gates,
        artifacts_root=args.artifacts_root,
    )
    print(json.dumps(result.to_dict(), indent=2))

    if args.retention is not None:
        gc = run_artifact_retention(
            artifacts_root=args.artifacts_root, keep=args.retention
        )
        print(f"artifact-retention(keep={args.retention}): ", end="")
        if gc.get("deleted"):
            print(f"deleted {len(gc['deleted'])} old artifact dir(s): {gc['deleted']}")
        else:
            print("nothing to delete")

    return 0 if result.passed and not result.collateral_damage else 1


if __name__ == "__main__":
    sys.exit(main())
