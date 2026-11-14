"""Gate adapters: installed launcher argv and the engine verifier boundary.

The installed launcher is the authoritative execution path. Development engine
imports cannot substitute for installed acceptance. Gate outcomes are typed:
``unavailable`` never becomes ``pass`` through a default branch, and a zero exit
without the expected evidence is a protocol error, not acceptance.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import OrchestrationError
from ..operations import EffectProbe
from ..state import Attempt, GateOutcome, VerifyOutcome
from .base import CancelReport


def _write_receipt(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


@dataclass
class InstalledLauncherGate:
    """Invoke the pinned ``bin/factory`` launcher with explicit argv.

    Never shell interpolation, never a model-authored command: the acceptance
    argv comes from the reviewed assignment. Exit status is captured separately
    from stdout, and a missing manifest after a zero exit is ``inconclusive``.
    """

    launcher: Path
    receipt_root: Path
    timeout_s: int = 3600
    terminate_grace_s: float = 10.0
    _processes: dict[str, subprocess.Popen] = field(default_factory=dict)

    def _receipt_path(self, key: str) -> Path:
        return (
            Path(self.receipt_root)
            / "launcher-receipts"
            / (key.replace(":", "_") + ".json")
        )

    def verify(
        self,
        *,
        artifacts: str,
        repository: str,
        candidate_sha: str,
        base_sha: str,
    ) -> VerifyOutcome:
        from ..evidence_guard import manifest_guard

        guarded, guard_reason = manifest_guard(artifacts)
        if not guarded:
            return VerifyOutcome(
                certified=False,
                reason=guard_reason,
                detail="manifest byte guard rejected the evidence before verification",
            )
        argv = [
            str(self.launcher),
            "verify",
            "--artifacts-dir",
            str(artifacts),
            "--repo",
            str(repository),
            "--candidate",
            candidate_sha,
            "--base",
            base_sha,
            "--json",
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(
                "gate_unavailable", f"installed launcher unavailable: {exc}"
            ) from exc
        if completed.returncode == 2:
            return VerifyOutcome(
                certified=False,
                reason="usage_error",
                detail=completed.stderr.strip()[:400],
            )
        payload: dict = {}
        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            payload = {}
        reason = str(payload.get("reason") or "")
        if completed.returncode == 0 and payload.get("ok") is True:
            return VerifyOutcome(
                certified=True,
                reason=payload.get("reason") or "ok",
                detail="",
            )
        return VerifyOutcome(
            certified=False,
            reason=reason or f"launcher_exit_{completed.returncode}",
            detail=str(payload.get("detail") or completed.stderr.strip())[:400],
        )

    def run_acceptance(
        self,
        *,
        operation_key: str,
        attempt: Attempt,
        workspace: str,
        base_sha: str,
        candidate_sha: str,
        argv: tuple[str, ...],
        artifacts: str,
        timeout_s: int,
    ) -> GateOutcome:
        if not argv:
            raise OrchestrationError("invalid_assignment", "empty acceptance argv")
        executable = Path(argv[0])
        if not executable.exists():
            raise OrchestrationError(
                "gate_unavailable", f"acceptance executable is missing: {executable}"
            )
        _write_receipt(
            self._receipt_path(operation_key),
            {
                "stage": "run_acceptance",
                "argv": list(argv),
                "workspace": workspace,
                "base_sha": base_sha,
                "candidate_sha": candidate_sha,
                "state": "submitted",
            },
        )
        log_dir = Path(artifacts) / "launcher-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / (operation_key.replace(":", "_") + ".stdout.log")
        stderr_path = log_dir / (operation_key.replace(":", "_") + ".stderr.log")
        try:
            with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
                process = subprocess.Popen(
                    list(argv),
                    cwd=workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=dict(os.environ, FACTORY_ORCHESTRATION_RUN=operation_key),
                    start_new_session=True,
                )
                self._processes[operation_key] = process
                try:
                    exit_code = process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    self._terminate(process)
                    _write_receipt(
                        self._receipt_path(operation_key),
                        {"state": "failed", "reason": "gate_timeout"},
                    )
                    return GateOutcome(
                        "unavailable",
                        reason="gate_timeout",
                        detail="acceptance timed out",
                    )
        except OSError as exc:
            raise OrchestrationError(
                "gate_unavailable", f"acceptance could not start: {exc}"
            ) from exc

        manifest = _latest_manifest(Path(artifacts))
        _write_receipt(
            self._receipt_path(operation_key),
            {
                "stage": "run_acceptance",
                "state": "confirmed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "evidence_reference": str(manifest.parent) if manifest else "",
                "evidence_digest": (
                    "sha256:" + _file_sha256(manifest) if manifest else ""
                ),
            },
        )
        if exit_code == 0:
            if manifest is None:
                return GateOutcome(
                    "inconclusive",
                    reason="protocol_error",
                    detail="launcher exited 0 without an evidence manifest",
                )
            return GateOutcome(
                "pass",
                evidence_reference=str(manifest.parent),
                evidence_digest="sha256:" + _file_sha256(manifest),
                log_reference=str(stderr_path),
            )
        if exit_code == 2:
            return GateOutcome(
                "inconclusive",
                reason="usage_error",
                detail="acceptance argv was refused",
            )
        return GateOutcome(
            "fail",
            reason=(_reason_from_manifest(manifest) if manifest else "")
            or f"launcher_exit_{exit_code}",
            detail=str(stderr_path),
            log_reference=str(stderr_path),
        )

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.time() + self.terminate_grace_s
        while time.time() < deadline and process.poll() is None:
            time.sleep(0.05)
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def probe(self, operation_key: str) -> EffectProbe:
        path = self._receipt_path(operation_key)
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return EffectProbe("absent")
        if payload.get("state") in {"confirmed", "failed"}:
            return EffectProbe("present", receipt=payload)
        return EffectProbe(
            "unknown", detail="acceptance was launched but has no terminal receipt"
        )

    def cancel(self, operation_key: str) -> CancelReport:
        process = self._processes.get(operation_key)
        if process is None:
            return CancelReport(
                operation_key,
                False,
                "process is not owned by this evaluation; reconcile the target",
            )
        if process.poll() is not None:
            return CancelReport(operation_key, True, "already exited")
        self._terminate(process)
        return CancelReport(operation_key, True, "terminated")


def _reason_from_manifest(manifest: Path) -> str:
    try:
        payload = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return ""
    evidence = payload.get("evidence") or payload
    return str(evidence.get("reason") or "")


def _file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latest_manifest(artifacts: Path) -> Path | None:
    """Find the manifest the installed runner wrote.

    ``factory-run`` writes into ``<artifacts-root>/<factory-project>/``; a direct
    manifest in the given directory is also accepted. More than one project
    directory with the same newest timestamp is ambiguous and returns ``None``
    so the caller reports ``inconclusive`` instead of guessing.
    """

    direct = artifacts / "evidence-manifest.json"
    if direct.is_file():
        return direct
    candidates = [
        path for path in artifacts.glob("*/evidence-manifest.json") if path.is_file()
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    newest_time = newest.stat().st_mtime
    tied = [path for path in candidates if path.stat().st_mtime == newest_time]
    return newest if len(tied) == 1 else None
