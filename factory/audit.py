"""
Dependency audit gate for the Software Factory.

Host-side vulnerability scan of a repo's dependency manifests, run as a
standard factory gate BEFORE a compose run or alongside a bare-venv test
run. Uses pip-audit for Python and `npm audit` for JS/TS.

Design notes:
- Runs on the HOST using pip-audit (already in the hermes venv) and npm
  (installed), inspecting the checked-out repo's manifests. This keeps
  audit tooling out of the ephemeral test containers and lets the gate run
  even on cards that need no Compose at all.
- Its failure mode is conservative: a pipeline NEVER advances past a
  confirmed-vulnerable dependency. If the audit tool cannot reach its
  vulnerability index (network/API issue), that is reported distinctly as
  `unavailable` and treated as a WARNING, not a hard fail -- so a transient
  offline condition doesn't block factory work, but a real finding does.

run_dependency_audit returns a flat dict, never raises on non-policy
conditions, so the orchestrator can read the result without exception
handling.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AuditResult:
    status: str  # "pass" | "fail" | "unavailable" | "no_manifest" | "unsupported"
    language: str
    findings: list = None
    detail: str = ""
    tool: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "language": self.language,
            "findings": self.findings or [],
            "detail": self.detail,
            "tool": self.tool,
        }


def _discover_manifests(repo_path: Path) -> tuple[list[Path], str]:
    """Return (manifest_paths, language). Prefer lock files; return ALL
    manifests found so a polyglot repo gets audited with each relevant tool."""
    checks: list[tuple[str, str]] = [
        ("requirements.txt", "python"),
        ("Pipfile.lock", "python"),
        ("poetry.lock", "python"),
        ("pyproject.toml", "python"),  # only counted if it has [project] deps
        ("package-lock.json", "typescript"),
        ("package.json", "typescript"),  # npm audit handles this directly
        ("yarn.lock", "typescript"),
        ("pnpm-lock.yaml", "typescript"),
    ]
    found = [
        (repo_path / fname, lang)
        for fname, lang in checks
        if (repo_path / fname).is_file()
    ]
    if not found:
        return [], ""
    languages = {lang for _, lang in found}
    # One language wins when both are present (lock file preferred); a repo
    # with BOTH python and js manifests returns both sets.
    manifests: list[Path] = []
    for lang in sorted(languages):
        lang_manifests = [p for p, language in found if language == lang]

        def is_lock(path):
            return path.name.endswith((".lock", "-lock.json", "lock.yaml"))

        locks = [p for p in lang_manifests if is_lock(p)]
        if locks:
            # Lock files are the authoritative dependency set.
            manifests.extend(locks)
            # For python keep requirements.txt alongside a lock/declaration
            # when both exist (they frequently drift).
            if lang == "python":
                manifests.extend(
                    p
                    for p in lang_manifests
                    if p.name == "requirements.txt" and not is_lock(p)
                )
        else:
            # No lock file: audit every declared manifest -- they can drift
            # against each other and each tells part of the story.
            manifests.extend(lang_manifests)
    primary_lang = "python" if "python" in languages else "typescript"
    return manifests, primary_lang


def run_dependency_audit(
    repo_path: str, language_hint: str = "", venv_path: str = ""
) -> AuditResult:
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        return AuditResult(
            "unavailable", language_hint or "unknown", detail=f"no repo at {repo}"
        )

    manifests, language = _discover_manifests(repo)
    language = language_hint.lower() or language

    has_python = any(
        p.suffix in {".txt", ".lock", ".toml"}
        and p.name not in {"package-lock.json", "pnpm-lock.yaml"}
        for p in manifests
    )
    has_node = any(
        p.name in {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
        for p in manifests
    )

    # Audit every ecosystem present. A Python venv is still the authoritative
    # Python input, but must not suppress npm audit for a polyglot repository.
    results: list[AuditResult] = []
    if venv_path and (has_python or not has_node):
        results.append(_run_pip_audit_env(repo, Path(venv_path).expanduser().resolve()))
    elif has_python:
        py_manifests = [
            p
            for p in manifests
            if p.name
            not in {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
        ]
        results.extend(_run_pip_audit(repo, p) for p in py_manifests)
    if has_node:
        results.append(_run_npm_audit(repo))
    if results:
        for status in ("fail", "unavailable", "unsupported", "no_manifest"):
            hit = next((r for r in results if r.status == status), None)
            if hit:
                hit.detail = f"{hit.detail} | other scans: " + ", ".join(
                    f"{r.tool}:{r.status}" for r in results if r is not hit
                )
                return hit
        return AuditResult(
            "pass",
            "+".join(sorted({r.language for r in results})),
            detail="all dependency scans passed",
            tool="pip-audit,npm audit",
        )

    # An explicit venv without manifests still audits the environment.
    if venv_path:
        return _run_pip_audit_env(repo, Path(venv_path).expanduser().resolve())

    if language in ("python", "py"):
        # The gate MUST audit what actually runs, not just what the manifest
        # declares. A manifest can pin a fixed version the interpreter can't
        # install (e.g. pytest 9.0.3 needs py3.10+, but a py3.9 venv must
        # run 8.4.2 which is still CVE-exposed). When a venv_path is given,
        # we scan the venv's installed site-packages; the manifest is a
        # cross-check for declared-but-not-installed drift.
        if manifests:
            results = [_run_pip_audit(repo, m) for m in manifests]
            # Worst status wins: any confirmed "fail" blocks the gate;
            # otherwise report the first non-pass so it's still visible.
            for want in ("fail", "unavailable", "no_manifest"):
                hit = next((r for r in results if r.status == want), None)
                if hit:
                    extra = [r for r in results if r is not hit]
                    hit.detail = (
                        f"{hit.detail} | other scans: "
                        + ", ".join(f"{r.tool}:{r.status}" for r in extra)
                        if extra
                        else hit.detail
                    )
                    return hit
            return AuditResult(
                "pass",
                "python",
                detail=f"no known vulnerabilities across {len(results)} manifest(s)",
                tool="pip-audit",
            )
        return AuditResult(
            "no_manifest",
            "python",
            detail="python contract has no manifest and no venv_path for env audit",
        )

    if language in ("typescript", "javascript", "ts", "js", "node"):
        return _run_npm_audit(repo)
    if not manifests:
        return AuditResult(
            "no_manifest",
            language or "unknown",
            detail="no python/js dependency manifest found in repo",
        )
    return AuditResult(
        "unsupported", language, detail=f"no audit tool wired for language '{language}'"
    )


def _pip_audit_binary() -> str | None:
    """Resolve the pip-audit binary without hardcoding a user-specific path.

    Precedence: explicit ``FACTORY_PIP_AUDIT`` env override, then PATH, then
    the Hermes venv's known location. Returns None if nothing usable is found
    (callers report ``unavailable`` rather than crashing).
    """
    override = os.environ.get("FACTORY_PIP_AUDIT")
    if override:
        return override
    candidates = [
        shutil.which("pip-audit"),
        str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "pip-audit"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return None


def _run_pip_audit_env(repo: Path, venv: Path) -> AuditResult:
    """Audit the packages actually installed in a repo-local venv."""
    pip_bin = venv / "bin" / "pip"
    if not pip_bin.is_file():
        return AuditResult(
            "unavailable",
            "python",
            detail=f"venv has no bin/pip at {venv}",
            tool="pip-audit",
        )
    pip_audit = _pip_audit_binary()
    if not pip_audit:
        return AuditResult(
            "unavailable",
            "python",
            detail="pip-audit binary not found on PATH or in the Hermes venv",
            tool="pip-audit",
        )
    # Point pip-audit at the venv's site-packages via --path and -l
    # (audit the local environment at that path). --no-deps is invalid
    # without -r, so we use -l here; it audits exactly the installed set.
    candidates = sorted((venv / "lib").glob("python*/site-packages"))
    # Keep a plain lib fallback for minimal test fixtures and nonstandard
    # virtual environments; real CPython venvs use site-packages above.
    if not candidates and (venv / "lib").is_dir():
        candidates = [venv / "lib"]
    if not candidates:
        return AuditResult(
            "unavailable",
            "python",
            detail=f"no site-packages under {venv / 'lib'}",
            tool="pip-audit",
        )
    cmd = [pip_audit, "--format", "json", "-l", "--path", str(candidates[0])]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, check=False
        )
    except subprocess.TimeoutExpired:
        return AuditResult(
            "unavailable",
            "python",
            detail="pip-audit env scan timed out (index unreachable)",
            tool="pip-audit",
        )
    except OSError as exc:
        # Binary missing/not executable (e.g. a bad FACTORY_PIP_AUDIT override).
        # This module's contract is to report, never raise.
        return AuditResult(
            "unavailable",
            "python",
            detail=f"could not execute pip-audit: {exc}",
            tool="pip-audit",
        )

    if proc.returncode in (0, 1):
        try:
            data = json.loads(proc.stdout or "{}")
            deps = data.get("dependencies", [])
            findings = [d for d in deps if d.get("vulns")]
            if findings:
                return AuditResult(
                    "fail",
                    "python",
                    findings=findings,
                    detail=f"{len(findings)} vulnerable package(s) in installed env",
                    tool="pip-audit",
                )
            return AuditResult(
                "pass",
                "python",
                detail="no known vulnerabilities in installed env",
                tool="pip-audit",
            )
        except json.JSONDecodeError:
            return AuditResult(
                "unavailable",
                "python",
                detail="pip-audit returned unparseable output",
                tool="pip-audit",
            )

    stderr = (proc.stderr or "").strip()[:400]
    return AuditResult(
        "unavailable",
        "python",
        detail=f"pip-audit could not complete: {stderr}",
        tool="pip-audit",
    )


def _run_pip_audit(repo: Path, manifest: Path) -> AuditResult:
    pip_audit = _pip_audit_binary()
    if not pip_audit:
        return AuditResult(
            "unavailable",
            "python",
            detail="pip-audit binary not found on PATH or in the Hermes venv",
            tool="pip-audit",
        )
    cmd = [pip_audit, "--format", "json", "--no-deps"]
    # pip-audit reads a requirements file when passed -r; otherwise scans
    # the local environment, which is NOT what we want for a repo gate.
    if manifest.name in ("requirements.txt", "Pipfile.lock"):
        cmd += ["-r", str(manifest)]
    else:
        # pyproject.toml / poetry.lock are scanned by pip-audit directly.
        cmd += [str(manifest)]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired:
        return AuditResult(
            "unavailable",
            "python",
            detail="pip-audit timed out (vulnerability index unreachable)",
            tool="pip-audit",
        )
    except OSError as exc:
        return AuditResult(
            "unavailable",
            "python",
            detail=f"could not execute pip-audit: {exc}",
            tool="pip-audit",
        )

    if proc.returncode in (0, 1):
        # pip-audit returns 0 (no vulns) or 1 (vulns found). Distinguish by parse.
        try:
            data = json.loads(proc.stdout or "{}")
            deps = data.get("dependencies", [])
            findings = [d for d in deps if d.get("vulns")]
            if findings:
                return AuditResult(
                    "fail",
                    "python",
                    findings=findings,
                    detail=f"{len(findings)} vulnerable package(s)",
                    tool="pip-audit",
                )
            return AuditResult(
                "pass", "python", detail="no known vulnerabilities", tool="pip-audit"
            )
        except json.JSONDecodeError:
            # Older/newer output shape or emitted to stderr.
            return AuditResult(
                "unavailable",
                "python",
                detail="pip-audit returned unparseable output",
                tool="pip-audit",
            )

    # Non-zero for non-vuln reasons (network, API down, manifest unreadable).
    stderr = (proc.stderr or "").strip()[:400]
    return AuditResult(
        "unavailable",
        "python",
        detail=f"pip-audit could not complete: {stderr}",
        tool="pip-audit",
    )


def _run_npm_audit(repo: Path) -> AuditResult:
    cmd = ["npm", "audit", "--json", "--audit-level", "moderate"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo), capture_output=True, text=True, timeout=180, check=False
        )
    except subprocess.TimeoutExpired:
        return AuditResult(
            "unavailable",
            "typescript",
            detail="npm audit timed out (registry unreachable)",
            tool="npm audit",
        )
    except OSError as exc:
        # npm not installed on this host -- report, never raise.
        return AuditResult(
            "unavailable",
            "typescript",
            detail=f"could not execute npm: {exc}",
            tool="npm audit",
        )

    try:
        data = json.loads(proc.stdout or "{}")
        if proc.returncode not in (0, 1):
            return AuditResult(
                "unavailable",
                "typescript",
                detail=f"npm audit exited {proc.returncode}: {(proc.stderr or '').strip()[:400]}",
                tool="npm audit",
            )
        # exit code 0 == no vulns; else vulns found meta.vulnerabilities
        meta = data.get("metadata", {}) or {}
        if "error" in data or "vulnerabilities" not in meta:
            return AuditResult(
                "unavailable",
                "typescript",
                detail="npm audit returned no vulnerability metadata",
                tool="npm audit",
            )
        vulns = meta.get("vulnerabilities", {}) or {}
        total = int(vulns.get("total", 0))
        if total > 0:
            return AuditResult(
                "fail",
                "typescript",
                findings=vulns,
                detail=f"{total} vulnerabilities (incl moderate+)",
                tool="npm audit",
            )
        return AuditResult(
            "pass", "typescript", detail="no vulnerabilities", tool="npm audit"
        )
    except json.JSONDecodeError:
        return AuditResult(
            "unavailable",
            "typescript",
            detail="npm audit returned unparseable output",
            tool="npm audit",
        )


def gate_summary(results: list[AuditResult]) -> str:
    """Human-readable one-line summary for a card's QC report."""
    if not results:
        return "dependency audit: nothing to scan"
    parts = [f"{r.language}:{r.status}" for r in results]
    return "dependency audit [" + ", ".join(parts) + "]"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m factory.audit --repo X [--venv Y].

    Prints a JSON result on stdout and exits 0 on pass (or warning-tier
    'unavailable', which never blocks a gate), nonzero on a confirmed
    vulnerability. Mirrors factory/coverage.py's CLI shape.
    """
    parser = argparse.ArgumentParser(
        description="Run the factory dependency audit gate on a repo."
    )
    parser.add_argument(
        "--repo", required=True, help="path to the repo whose dependencies to audit"
    )
    parser.add_argument(
        "--venv",
        default="",
        help="path to the repo's python venv; audits the installed packages "
        "there instead of just the declared manifest",
    )
    args = parser.parse_args(argv)

    result = run_dependency_audit(args.repo, venv_path=args.venv)
    # 'unavailable' is a warning (offline index), not a confirmed finding:
    # only a hard 'fail' blocks the gate.
    passed = result.status != "fail"
    print(json.dumps({"passed": passed, "result": result.to_dict()}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
