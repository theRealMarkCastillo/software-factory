"""
Dependency audit gate for the Software Factory.

Host-side vulnerability scan of a repo's dependency manifests, run as a
standard factory gate BEFORE a compose run or alongside a bare-venv test
run. Uses pip-audit for Python and `npm audit` for JS/TS.

Design notes:

- Runs on the HOST using pip-audit (installed with the engine environment) and
  npm (installed), inspecting the checked-out repo and the environment that
  actually runs. This keeps audit tooling out of the ephemeral test containers
  and lets the gate run even on cards that need no Compose at all.

- **Fail closed.** A gate that cannot verify the dependency set must not
  advance a pipeline. Only a schema-valid pip-audit JSON report that names at
  least one *audited* dependency, carries no vulnerability findings and no
  unexpected skipped dependency, and whose exit code agrees with the report
  (rc 0) can PASS. Empty or malformed stdout, usage errors, timeouts, tool
  failures, rc0-with-findings, rc1-without-findings, zero audited
  dependencies and unexpected skips are all blocking results. There is no
  warning tier for an unverified audit.

- **Deterministic inputs.** An installed environment is audited directly with
  the invocation pip-audit actually supports:
  ``--format json --path <site-packages>``. ``-l/--local`` is rejected by
  pip-audit when combined with ``--path`` and must never be passed. Lock files
  (``uv.lock``, ``poetry.lock``, ``Pipfile.lock``) are converted to a fully
  pinned requirement set parsed from the lock itself and audited with
  ``-r <file> --no-deps`` -- never re-resolved, never handed to pip-audit as
  an unsupported positional manifest. A declared-but-unpinned
  ``requirements.txt`` or a ``pyproject.toml`` without a lock fails closed
  with an actionable reason instead of silently resolving dependencies from
  an index.

- **Lock/environment binding.** When a repo-local environment is audited and
  a ``uv.lock`` is present, the installed package versions must agree with the
  lock. Drift is reported (with the ``uv sync --frozen`` remedy) rather than
  auditing a different dependency set.

- **Skips are not passes.** Audited and skipped dependencies are counted
  separately. Only the repository's own local project (the ``[project].name``
  declared in its ``pyproject.toml``) may be skipped by pip-audit, because it
  is not published to an index; every other skipped dependency blocks the gate
  and is reported by name. There is no blanket allowlist and no environment
  variable that can silence a third-party skip.

- **Provenance.** The result records the exact argv, the auditor version, the
  lock digest, the stdout digest, the vulnerability service and the
  audited/skipped counts so acceptance evidence can be re-checked.

``run_dependency_audit`` returns a flat dataclass, never raises on non-policy
conditions, so the orchestrator can read the result without exception
handling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: pip-audit's default ``--vulnerability-service`` is ``pypi``; the gate passes
#: it explicitly so the provenance record names the database it consulted.
VULNERABILITY_SERVICE = "pypi"

ENVIRONMENT_TIMEOUT_S = 180
LOCK_TIMEOUT_S = 300
NPM_TIMEOUT_S = 180

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_INVALID = "invalid"
STATUS_UNSUPPORTED = "unsupported"
STATUS_NO_MANIFEST = "no_manifest"

#: Anything not in this set blocks the pipeline. ``no_manifest`` is the one
#: permitted inapplicable result (evidence policy verifies it against the tree).
PASSING_STATUSES = frozenset({STATUS_PASS, STATUS_NO_MANIFEST})

PYTHON_LOCKS = ("uv.lock", "poetry.lock", "Pipfile.lock")
NODE_MANIFESTS = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")

#: ``name[extras] == version [; marker]`` -- a deterministic pin we can audit.
PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]+\])?"
    r"\s*==\s*(?P<version>[^\s;=<>!~]+)"
    r"\s*(?P<marker>;.*)?$"
)
COMMENT_RE = re.compile(r"(^|\s)#.*$")


@dataclass
class AuditResult:
    status: str  # pass | fail | invalid | unsupported | no_manifest
    language: str
    findings: list = field(default_factory=list)
    detail: str = ""
    tool: str = ""
    audited: int = 0
    skipped: list = field(default_factory=list)
    command: list = field(default_factory=list)
    tool_version: str = ""
    source: str = ""
    lock_sha256: str = ""
    report_sha256: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "language": self.language,
            "findings": self.findings or [],
            "detail": self.detail,
            "tool": self.tool,
            "audited": self.audited,
            "skipped": self.skipped or [],
            "command": self.command or [],
            "tool_version": self.tool_version,
            "source": self.source,
            "lock_sha256": self.lock_sha256,
            "report_sha256": self.report_sha256,
            "vulnerability_service": (
                VULNERABILITY_SERVICE if self.tool == "pip-audit" else ""
            ),
        }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """PEP 503 normalization for package-name comparisons."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_project_names(repo: Path) -> set[str]:
    """Names of the repository's own (unpublished) project, from pyproject."""
    names: set[str] = set()
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return names
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names
    project = data.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        names.add(_normalize(project["name"]))
    poetry = data.get("tool", {}).get("poetry") if isinstance(data, dict) else None
    if isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
        names.add(_normalize(poetry["name"]))
    return {name for name in names if name}


def _discover_manifests(repo_path: Path) -> tuple[list[Path], str]:
    """Return (manifests, language) using deterministic precedence.

    Python: a lock file is the authoritative dependency set. Without a lock a
    fully pinned ``requirements.txt`` is used; a bare ``pyproject.toml`` is
    kept only so the caller can explain that no deterministic input exists.
    Node: a lock file wins over ``package.json`` (npm audit reads either).
    """
    python: list[Path] = [
        repo_path / name for name in PYTHON_LOCKS if (repo_path / name).is_file()
    ]
    if not python:
        # No lock: a declared requirements file and the project declaration are
        # both candidates (they can drift). The gate audits only deterministic
        # inputs; see ``_select_python_manifests``.
        if (repo_path / "requirements.txt").is_file():
            python.append(repo_path / "requirements.txt")
        if (repo_path / "pyproject.toml").is_file():
            python.append(repo_path / "pyproject.toml")

    node: list[Path] = []
    node_locks = [
        name
        for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
        if (repo_path / name).is_file()
    ]
    if node_locks:
        node = [repo_path / name for name in node_locks]
    elif (repo_path / "package.json").is_file():
        node = [repo_path / "package.json"]

    languages = set()
    if python:
        languages.add("python")
    if node:
        languages.add("typescript")
    primary = "python" if "python" in languages else ("typescript" if languages else "")
    return python + node, primary


def _select_python_manifests(manifests: list[Path]) -> list[Path]:
    """Pick the deterministic inputs the gate audits.

    A lock file is authoritative; without one a pinned requirements file is
    used. A bare ``pyproject.toml`` is never handed to pip-audit positionally
    (that re-resolves dependencies from an index), so it is only kept when it
    is the sole candidate -- to explain that no deterministic input exists.
    """
    locks = [path for path in manifests if path.name in PYTHON_LOCKS]
    if locks:
        return locks
    requirements = [path for path in manifests if path.name == "requirements.txt"]
    if requirements:
        return requirements
    return [path for path in manifests if path.name == "pyproject.toml"]


def _site_packages(venv: Path) -> Path | None:
    candidates = sorted(
        path for path in (venv / "lib").glob("python*/site-packages") if path.is_dir()
    )
    if not candidates:
        windows = venv / "Lib" / "site-packages"
        return windows if windows.is_dir() else None
    return candidates[0]


def _installed_versions(site_packages: Path) -> dict[str, str]:
    """name -> version for the distributions installed at ``site_packages``."""
    installed: dict[str, str] = {}
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        if not metadata.is_file():
            continue
        name = version = ""
        try:
            with metadata.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
                    elif not line.strip():
                        break
        except OSError:
            continue
        if name and version:
            installed[_normalize(name)] = version
    return installed


# ---------------------------------------------------------------------------
# pip-audit discovery / version provenance
# ---------------------------------------------------------------------------


def _engine_pip_audit_candidates() -> list[str]:
    """pip-audit locations inside the interpreter running this engine.

    The gate must audit with the auditor installed alongside the engine
    interpreter. Ambient PATH and a historical Hermes installation are only
    fallbacks: on a clean deployment they may not exist, and on a shared host
    they may belong to a different installation.
    """
    candidates: list[str] = []
    executable = getattr(sys, "executable", "") or ""
    if executable:
        # Do not resolve(): a venv's python is usually a symlink to the base
        # interpreter, while the auditor lives beside the venv entry point.
        candidates.append(str(Path(executable).parent / "pip-audit"))
    try:
        scripts = sysconfig.get_path("scripts")
    except (KeyError, TypeError):  # pragma: no cover - unusual interpreter scheme
        scripts = None
    if scripts:
        candidates.append(str(Path(scripts) / "pip-audit"))
    return candidates


def _pip_audit_binary() -> str | None:
    """Resolve the pip-audit binary without hardcoding a user-specific path.

    Precedence: explicit ``FACTORY_PIP_AUDIT`` override (honoured as given, so
    a bad override fails closed instead of silently selecting another
    auditor), then the current engine interpreter's environment, then PATH,
    then the historical Hermes venv. Returns None if nothing usable is found.
    """
    override = os.environ.get("FACTORY_PIP_AUDIT")
    if override:
        return override
    candidates = [
        *_engine_pip_audit_candidates(),
        shutil.which("pip-audit"),
        str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "pip-audit"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return None


def _tool_version(tool: str) -> str:
    """Version of the auditor without executing it.

    Console scripts live in ``<env>/bin``; the distribution metadata lives in
    ``<env>/lib/python*/site-packages`` (or ``~/.local/lib`` for a user
    install). Reading it keeps the gate free of an extra subprocess (and of a
    mocked ``subprocess.run`` in the test suite).
    """
    path = Path(tool)
    roots = [path.parent.parent]
    for key in ("purelib", "platlib"):
        try:
            location = sysconfig.get_path(key)
        except (KeyError, TypeError):  # pragma: no cover - unusual scheme
            location = None
        if location:
            roots.append(Path(location).parent.parent)
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        patterns = (
            "lib/python*/site-packages/pip_audit-*.dist-info/METADATA",
            "lib/pip_audit-*.dist-info/METADATA",
            "pip_audit-*.dist-info/METADATA",
        )
        for pattern in patterns:
            for metadata in sorted(root.glob(pattern)):
                version = ""
                try:
                    with metadata.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if line.startswith("Version:"):
                                version = line.split(":", 1)[1].strip()
                                break
                except OSError:
                    continue
                if version:
                    return version
    return "unknown"


# ---------------------------------------------------------------------------
# report parsing / verdict
# ---------------------------------------------------------------------------


def _parse_report(text: str) -> tuple[dict | None, str]:
    """Validate the real pip-audit JSON shape; return (report, problem)."""
    if not text.strip():
        return None, "stdout is empty"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"stdout is not JSON: {exc}"
    if not isinstance(data, dict):
        return None, "stdout JSON is not an object"
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list):
        return None, "stdout JSON has no dependencies list"
    for index, entry in enumerate(dependencies):
        if not isinstance(entry, dict):
            return None, f"dependency entry {index} is not an object"
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            return None, f"dependency entry {index} has no name"
        if "vulns" in entry:
            if not isinstance(entry["vulns"], list):
                return None, f"dependency {name} vulns is not a list"
            if not isinstance(entry.get("version"), str) or not entry["version"]:
                return None, f"dependency {name} has no version"
            for vuln in entry["vulns"]:
                if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str):
                    return None, f"dependency {name} has a vulnerability without id"
        elif not isinstance(entry.get("skip_reason"), str) or not entry["skip_reason"]:
            return None, (
                f"dependency {name} has neither a vulns list nor a skip_reason"
            )
    return data, ""


def _interpret(
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    command: list[str],
    local_skips: set[str],
    source: str,
    tool_version: str,
    lock_sha256: str = "",
    extra_skipped: list | None = None,
) -> AuditResult:
    """Turn one tool invocation into a verdict. Never returns ``pass`` unless
    the report is readable, complete and consistent with the exit code."""
    result = AuditResult(
        STATUS_INVALID,
        "python",
        tool="pip-audit",
        command=list(command),
        tool_version=tool_version,
        source=source,
        lock_sha256=lock_sha256,
        skipped=list(extra_skipped or []),
    )
    stderr = (stderr or "").strip()
    if returncode not in (0, 1):
        result.detail = (
            f"pip-audit exited {returncode} (tool failure or usage error): "
            f"{stderr[:400] or 'no stderr'}"
        )
        return result

    data, problem = _parse_report(stdout or "")
    if data is None:
        result.detail = (
            f"pip-audit produced an unusable report (exit {returncode}): {problem}"
        )
        return result

    result.report_sha256 = _sha256_bytes((stdout or "").encode("utf-8"))
    entries = data["dependencies"]
    audited = [
        entry
        for entry in entries
        if isinstance(entry.get("vulns"), list)
        and (entry["vulns"] or not entry.get("skip_reason"))
    ]
    findings = [entry for entry in entries if entry.get("vulns")]
    skipped = [
        {"name": entry["name"], "skip_reason": entry["skip_reason"]}
        for entry in entries
        if not entry.get("vulns") and entry.get("skip_reason")
    ]
    result.audited = len(audited)
    result.findings = findings
    result.skipped.extend(skipped)

    unexpected = [
        item for item in result.skipped if _normalize(item["name"]) not in local_skips
    ]
    if unexpected:
        names = ", ".join(item["name"] for item in unexpected)
        result.detail = (
            "pip-audit skipped dependencies that are neither the local project "
            f"nor explicitly audited: {names}. A skipped third-party dependency "
            "cannot be reported as clean."
        )
        return result

    if result.audited == 0:
        result.detail = (
            "pip-audit audited zero dependencies "
            f"({len(entries)} reported, {len(result.skipped)} skipped); an "
            "all-skipped or empty report cannot be reported as clean."
        )
        return result

    if returncode == 0 and findings:
        names = ", ".join(item["name"] for item in findings)
        result.status = STATUS_FAIL
        result.detail = (
            "pip-audit exited 0 but reported vulnerabilities for "
            f"{names}; the finding is authoritative and fails the gate"
        )
        return result

    if returncode == 1 and not findings:
        result.detail = (
            "pip-audit exited 1 without any vulnerability findings; refusing "
            "to report the inconsistent result as clean."
        )
        return result

    if findings:
        names = ", ".join(
            f"{item['name']} {item.get('version', '')}".strip() for item in findings
        )
        result.status = STATUS_FAIL
        result.detail = f"{len(findings)} vulnerable package(s): {names}"
        if stderr:
            result.detail += f" | {stderr[:200]}"
        return result

    result.status = STATUS_PASS
    skipped_note = ""
    if result.skipped:
        skipped_note = "; not audited (local project): " + ", ".join(
            item["name"] for item in result.skipped
        )
    result.detail = (
        f"no known vulnerabilities across {result.audited} audited "
        f"dependenc{'y' if result.audited == 1 else 'ies'}{skipped_note}"
    )
    return result


def _run_tool(
    command: list[str],
    *,
    timeout: int,
    local_skips: set[str],
    source: str,
    tool_version: str,
    lock_sha256: str = "",
    extra_skipped: list | None = None,
) -> AuditResult:
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return AuditResult(
            STATUS_INVALID,
            "python",
            detail=(
                f"pip-audit timed out after {timeout}s; the vulnerability index "
                "may be unreachable and the dependency set was not audited"
            ),
            tool="pip-audit",
            command=list(command),
            tool_version=tool_version,
            source=source,
            lock_sha256=lock_sha256,
            skipped=list(extra_skipped or []),
        )
    except OSError as exc:
        return AuditResult(
            STATUS_INVALID,
            "python",
            detail=f"could not execute pip-audit ({command[0]}): {exc}",
            tool="pip-audit",
            command=list(command),
            tool_version=tool_version,
            source=source,
            lock_sha256=lock_sha256,
            skipped=list(extra_skipped or []),
        )
    return _interpret(
        proc.returncode,
        proc.stdout or "",
        proc.stderr or "",
        command=command,
        local_skips=local_skips,
        source=source,
        tool_version=tool_version,
        lock_sha256=lock_sha256,
        extra_skipped=extra_skipped,
    )


# ---------------------------------------------------------------------------
# manifest -> deterministic pinned dependency set
# ---------------------------------------------------------------------------


def _pin_from_requirement_line(line: str) -> tuple[tuple[str, str] | None, str]:
    """Return ((name, version), "") or (None, problem) for one requirement."""
    text = COMMENT_RE.sub("", line).strip()
    if not text:
        return None, ""
    if text.endswith("\\"):
        return None, f"line continuations are not supported: {text!r}"
    # Hashes pin the artifact but not the version; the version pin is what the
    # vulnerability database is queried with, so they are safe to drop here.
    text = re.sub(r"(?:^|\s)--hash[= ]\S+", "", text).strip()
    match = PIN_RE.match(text)
    if not match:
        return (
            None,
            f"requirement is not deterministically pinned (`name==version`): {text!r}",
        )
    return (match.group("name"), match.group("version")), ""


def _requirements_pins(
    repo: Path, path: Path
) -> tuple[list[tuple[str, str]], list[dict], list[str]]:
    """Parse a requirements file into (pins, local skips, problems)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [], [f"could not read {path.name}: {exc}"]
    local = _local_project_names(repo)
    pins: list[tuple[str, str]] = []
    skips: list[dict] = []
    problems: list[str] = []
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith(("-e ", "--editable ")):
            target = text.split(None, 1)[1].strip()
            if target in (".", "./") and local:
                skips.append(
                    {
                        "name": sorted(local)[0],
                        "skip_reason": "local project installed editable from the repo",
                    }
                )
                continue
            problems.append(f"editable requirement is not auditable: {text!r}")
            continue
        if text.startswith("-"):
            problems.append(
                f"unsupported requirements option (cannot be converted "
                f"deterministically): {text!r}"
            )
            continue
        pin, problem = _pin_from_requirement_line(text)
        if problem:
            problems.append(problem)
        elif pin:
            pins.append(pin)
    return pins, skips, problems


def _uv_lock_pins(path: Path) -> tuple[list[tuple[str, str]], list[dict], list[str]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [], [f"could not parse {path.name}: {exc}"]
    packages = data.get("package")
    if not isinstance(packages, list) or not packages:
        return [], [], [f"{path.name} has no [[package]] entries"]
    pins: list[tuple[str, str]] = []
    skips: list[dict] = []
    problems: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            problems.append(f"{path.name} contains a non-table package entry")
            continue
        name, version = package.get("name"), package.get("version")
        source = package.get("source") or {}
        if not isinstance(name, str) or not name:
            problems.append(f"{path.name} contains a package without a name")
            continue
        if not isinstance(version, str) or not version:
            problems.append(f"{path.name} package {name} has no version")
            continue
        if isinstance(source, dict) and "registry" in source:
            pins.append((name, version))
        else:
            kind = ",".join(sorted(source)) if isinstance(source, dict) else "unknown"
            skips.append(
                {
                    "name": name,
                    "version": version,
                    "source": kind or "unknown",
                    "skip_reason": (
                        f"not a PyPI registry package in {path.name} "
                        f"(source: {kind or 'unknown'})"
                    ),
                }
            )
    return pins, skips, problems


def _poetry_lock_pins(
    path: Path,
) -> tuple[list[tuple[str, str]], list[dict], list[str]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [], [f"could not parse {path.name}: {exc}"]
    packages = data.get("package")
    if not isinstance(packages, list) or not packages:
        return [], [], [f"{path.name} has no [[package]] entries"]
    pins: list[tuple[str, str]] = []
    skips: list[dict] = []
    problems: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            problems.append(f"{path.name} contains a non-table package entry")
            continue
        name, version = package.get("name"), package.get("version")
        source = package.get("source") or {}
        if not isinstance(name, str) or not name:
            problems.append(f"{path.name} contains a package without a name")
            continue
        if not isinstance(version, str) or not version:
            problems.append(f"{path.name} package {name} has no version")
            continue
        kind = source.get("type") if isinstance(source, dict) else None
        if kind in ("git", "directory", "file", "url"):
            skips.append(
                {
                    "name": name,
                    "version": version,
                    "source": str(kind),
                    "skip_reason": (
                        f"not a PyPI registry package in {path.name} (source: {kind})"
                    ),
                }
            )
        else:
            pins.append((name, version))
    return pins, skips, problems


def _pipfile_lock_pins(
    path: Path,
) -> tuple[list[tuple[str, str]], list[dict], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [], [f"could not parse {path.name}: {exc}"]
    if not isinstance(data, dict):
        return [], [], [f"{path.name} is not a JSON object"]
    pins: list[tuple[str, str]] = []
    skips: list[dict] = []
    problems: list[str] = []
    for section in ("default", "develop"):
        entries = data.get(section) or {}
        if not isinstance(entries, dict):
            problems.append(f"{path.name} section {section} is not an object")
            continue
        for name, spec in entries.items():
            if not isinstance(spec, dict):
                problems.append(f"{path.name} entry {name} is not an object")
                continue
            if any(key in spec for key in ("git", "path", "file")):
                skips.append(
                    {
                        "name": name,
                        "version": str(spec.get("version", "")),
                        "source": "vcs/path",
                        "skip_reason": (
                            f"not a PyPI registry package in {path.name} "
                            "(source: vcs/path)"
                        ),
                    }
                )
                continue
            version = str(spec.get("version", ""))
            if not version.startswith("=="):
                problems.append(
                    f"{path.name} entry {name} is not exactly pinned: {version!r}"
                )
                continue
            pins.append((name, version[2:]))
    return pins, skips, problems


def _pin_groups(pins: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Split pins so a name locked at several versions is never written into
    the same requirements file (pip-audit rejects duplicate requirements)."""
    by_name: dict[str, list[str]] = {}
    order: list[str] = []
    for name, version in pins:
        key = _normalize(name)
        if key not in by_name:
            by_name[key] = []
            order.append(key)
        if version not in by_name[key]:
            by_name[key].append(version)

    unique: list[tuple[str, str]] = []
    multi: list[tuple[str, list[str]]] = []
    seen_display: dict[str, str] = {}
    for name, version in pins:
        key = _normalize(name)
        seen_display.setdefault(key, name)
        if len(by_name[key]) == 1 and (name, version) not in unique:
            unique.append((name, version))
    for key in order:
        versions = by_name[key]
        if len(versions) > 1:
            multi.append((seen_display[key], versions))
    if not multi:
        return [unique]
    groups = [list(unique)]
    for name, versions in multi:
        for version in versions:
            groups.append([*unique, (name, version)])
    return groups


def _merge_scans(results: list[AuditResult], language: str) -> AuditResult:
    """Worst status wins; audited counts and skips are summed."""
    if not results:
        return AuditResult(STATUS_INVALID, language, detail="no scan produced a result")
    for wanted in (STATUS_FAIL, STATUS_INVALID, STATUS_UNSUPPORTED):
        hits = [result for result in results if result.status == wanted]
        if not hits:
            continue
        primary = hits[0]
        findings: list = []
        for hit in hits:
            findings.extend(hit.findings or [])
        primary.findings = findings
        others = [
            f"{result.source or result.tool}:{result.status}"
            for result in results
            if result is not primary
        ]
        if others:
            primary.detail = f"{primary.detail} | other scans: {', '.join(others)}"
        return primary
    if all(result.status == STATUS_PASS for result in results):
        first = results[0]
        skipped: list = []
        seen: set[tuple[str, str]] = set()
        for result in results:
            for item in result.skipped or []:
                key = (str(item.get("name")), str(item.get("skip_reason")))
                if key in seen:
                    continue
                seen.add(key)
                skipped.append(item)
        first.skipped = skipped
        first.audited = sum(result.audited for result in results)
        first.findings = []
        note = ""
        local_note = [item["name"] for item in skipped]
        if local_note:
            note = "; not audited (local project): " + ", ".join(local_note)
        first.detail = (
            f"no known vulnerabilities across {first.audited} audited "
            f"dependenc{'y' if first.audited == 1 else 'ies'}{note}"
        )
        return first
    return results[0]


# ---------------------------------------------------------------------------
# python scans
# ---------------------------------------------------------------------------


def _run_pip_audit_env(repo: Path, venv: Path) -> AuditResult:
    """Audit the packages actually installed in a repo-local environment."""
    pip_audit = _pip_audit_binary()
    if not pip_audit:
        return AuditResult(
            STATUS_INVALID,
            "python",
            detail=(
                "pip-audit binary not found in the engine environment, on PATH, "
                "or in the Hermes venv; the dependency set was not audited"
            ),
            tool="pip-audit",
            source="environment",
        )
    site_packages = _site_packages(venv)
    if site_packages is None:
        return AuditResult(
            STATUS_INVALID,
            "python",
            detail=(
                f"no site-packages under {venv / 'lib'}; the environment cannot "
                "be audited"
            ),
            tool="pip-audit",
            source="environment",
        )

    local = _local_project_names(repo)
    lock = repo / "uv.lock"
    lock_sha = ""
    if lock.is_file():
        lock_sha = _sha256_file(lock)
        pins, _skips, problems = _uv_lock_pins(lock)
        if problems:
            return AuditResult(
                STATUS_INVALID,
                "python",
                detail=f"cannot bind the environment to {lock.name}: {problems[0]}",
                tool="pip-audit",
                source="environment",
                lock_sha256=lock_sha,
            )
        locked = {}
        for name, version in pins:
            locked.setdefault(_normalize(name), set()).add(version)
        drift = []
        for name, version in _installed_versions(site_packages).items():
            if name in local or name not in locked:
                continue
            if version not in locked[name]:
                drift.append(
                    f"{name} {version} (lock: {', '.join(sorted(locked[name]))})"
                )
        if drift:
            return AuditResult(
                STATUS_INVALID,
                "python",
                detail=(
                    "installed environment does not match uv.lock: "
                    + "; ".join(drift[:5])
                    + (f" (+{len(drift) - 5} more)" if len(drift) > 5 else "")
                    + "; run `uv sync --frozen` so the audited environment is "
                    "the locked one"
                ),
                tool="pip-audit",
                source="environment",
                lock_sha256=lock_sha,
            )

    command = [
        pip_audit,
        "--format",
        "json",
        "--vulnerability-service",
        VULNERABILITY_SERVICE,
        "--path",
        str(site_packages),
    ]
    return _run_tool(
        command,
        timeout=ENVIRONMENT_TIMEOUT_S,
        local_skips=local,
        source="environment",
        tool_version=_tool_version(pip_audit),
        lock_sha256=lock_sha,
    )


def _audit_pins(
    repo: Path,
    pins: list[tuple[str, str]],
    skipped: list[dict],
    *,
    source: str,
    lock_sha256: str,
) -> AuditResult:
    """Audit a deterministic pinned set through temporary requirements files."""
    local = _local_project_names(repo)
    third_party = [item for item in skipped if _normalize(item["name"]) not in local]
    if third_party:
        names = ", ".join(
            f"{item['name']} ({item.get('source', 'unknown')})" for item in third_party
        )
        return AuditResult(
            STATUS_INVALID,
            "python",
            detail=(
                f"{source} contains dependencies pip-audit cannot audit from a "
                f"PyPI database: {names}. They cannot be silently omitted; pin "
                "them to published releases or audit them explicitly."
            ),
            tool="pip-audit",
            source=source,
            lock_sha256=lock_sha256,
            skipped=list(skipped),
        )
    if not pins:
        return AuditResult(
            STATUS_UNSUPPORTED,
            "python",
            detail=(
                f"{source} contains no PyPI registry packages to audit; there is "
                "no dependency set to verify"
            ),
            tool="pip-audit",
            source=source,
            lock_sha256=lock_sha256,
            skipped=list(skipped),
        )

    pip_audit = _pip_audit_binary()
    if not pip_audit:
        return AuditResult(
            STATUS_INVALID,
            "python",
            detail=(
                "pip-audit binary not found in the engine environment, on PATH, "
                "or in the Hermes venv; the dependency set was not audited"
            ),
            tool="pip-audit",
            source=source,
            lock_sha256=lock_sha256,
            skipped=list(skipped),
        )
    tool_version = _tool_version(pip_audit)
    results: list[AuditResult] = []
    with tempfile.TemporaryDirectory(prefix="factory-audit-") as tempdir:
        for index, group in enumerate(_pin_groups(pins)):
            path = Path(tempdir) / f"requirements-{index}.txt"
            path.write_text(
                "\n".join(f"{name}=={version}" for name, version in group) + "\n",
                encoding="utf-8",
            )
            command = [
                pip_audit,
                "--format",
                "json",
                "--vulnerability-service",
                VULNERABILITY_SERVICE,
                "-r",
                str(path),
                "--no-deps",
                # A pinned requirements file is read directly: no throwaway
                # virtualenv is created and nothing is installed. pip-audit
                # accepts --disable-pip exactly when --no-deps (or hashes) is
                # given, and rejects it otherwise.
                "--disable-pip",
            ]
            results.append(
                _run_tool(
                    command,
                    timeout=LOCK_TIMEOUT_S,
                    local_skips=local,
                    source=source,
                    tool_version=tool_version,
                    lock_sha256=lock_sha256,
                    extra_skipped=list(skipped),
                )
            )
    merged = _merge_scans(results, "python")
    merged.source = source
    merged.lock_sha256 = lock_sha256
    if merged.status == STATUS_PASS:
        local_skips = [item for item in skipped if _normalize(item["name"]) in local]
        note = ""
        if local_skips:
            note = "; not audited (local project): " + ", ".join(
                item["name"] for item in local_skips
            )
        merged.detail = (
            f"no known vulnerabilities across {merged.audited} audited "
            f"dependenc{'y' if merged.audited == 1 else 'ies'}{note}"
        )
    return merged


def _run_pip_audit(repo: Path, manifest: Path) -> AuditResult:
    """Audit one Python manifest through a deterministic conversion."""
    name = manifest.name
    if name == "pyproject.toml":
        return AuditResult(
            STATUS_UNSUPPORTED,
            "python",
            detail=(
                "pyproject.toml declares the project but no lock file is present; "
                "commit uv.lock, poetry.lock, Pipfile.lock or a fully pinned "
                "requirements.txt so the audited dependency set is deterministic"
            ),
            tool="pip-audit",
            source="pyproject.toml",
        )
    if name == "requirements.txt":
        pins, skips, problems = _requirements_pins(repo, manifest)
        if problems:
            return AuditResult(
                STATUS_UNSUPPORTED,
                "python",
                detail=problems[0],
                tool="pip-audit",
                source="requirements.txt",
                skipped=skips,
            )
        return _audit_pins(
            repo,
            pins,
            skips,
            source="requirements.txt",
            lock_sha256=_sha256_file(manifest),
        )
    if name == "uv.lock":
        pins, skips, problems = _uv_lock_pins(manifest)
    elif name == "poetry.lock":
        pins, skips, problems = _poetry_lock_pins(manifest)
    elif name == "Pipfile.lock":
        pins, skips, problems = _pipfile_lock_pins(manifest)
    else:
        return AuditResult(
            STATUS_UNSUPPORTED,
            "python",
            detail=f"no audit path is wired for manifest {name}",
            tool="pip-audit",
            source=name,
        )
    if problems:
        return AuditResult(
            STATUS_UNSUPPORTED,
            "python",
            detail=problems[0],
            tool="pip-audit",
            source=name,
            lock_sha256=_sha256_file(manifest),
        )
    return _audit_pins(
        repo,
        pins,
        skips,
        source=name,
        lock_sha256=_sha256_file(manifest),
    )


# ---------------------------------------------------------------------------
# node scan
# ---------------------------------------------------------------------------


def _run_npm_audit(repo: Path) -> AuditResult:
    cmd = ["npm", "audit", "--json", "--audit-level", "moderate"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=NPM_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AuditResult(
            STATUS_INVALID,
            "typescript",
            detail="npm audit timed out (registry unreachable)",
            tool="npm audit",
            command=list(cmd),
            source="package-lock",
        )
    except OSError as exc:
        # npm not installed on this host -- report, never raise.
        return AuditResult(
            STATUS_INVALID,
            "typescript",
            detail=f"could not execute npm: {exc}",
            tool="npm audit",
            command=list(cmd),
            source="package-lock",
        )

    try:
        data = json.loads(proc.stdout or "{}")
        if proc.returncode not in (0, 1):
            return AuditResult(
                STATUS_INVALID,
                "typescript",
                detail=(
                    f"npm audit exited {proc.returncode}: "
                    f"{(proc.stderr or '').strip()[:400]}"
                ),
                tool="npm audit",
                command=list(cmd),
                source="package-lock",
            )
        meta = data.get("metadata", {}) or {}
        if "error" in data or "vulnerabilities" not in meta:
            return AuditResult(
                STATUS_INVALID,
                "typescript",
                detail="npm audit returned no vulnerability metadata",
                tool="npm audit",
                command=list(cmd),
                source="package-lock",
            )
        vulns = meta.get("vulnerabilities", {}) or {}
        total = int(vulns.get("total", 0))
        dependencies = int(meta.get("dependencies", 0) or 0)
        result = AuditResult(
            STATUS_PASS,
            "typescript",
            tool="npm audit",
            command=list(cmd),
            source="package-lock",
            audited=dependencies,
            report_sha256=_sha256_bytes((proc.stdout or "").encode("utf-8")),
        )
        if total > 0:
            result.status = STATUS_FAIL
            result.findings = [vulns]
            result.detail = f"{total} vulnerabilities (incl moderate+)"
            return result
        result.detail = f"no vulnerabilities across {dependencies} audited dependencies"
        return result
    except (ValueError, TypeError):
        return AuditResult(
            STATUS_INVALID,
            "typescript",
            detail="npm audit returned unparseable output",
            tool="npm audit",
            command=list(cmd),
            source="package-lock",
        )


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def run_dependency_audit(
    repo_path: str, language_hint: str = "", venv_path: str = ""
) -> AuditResult:
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        return AuditResult(
            STATUS_INVALID, language_hint or "unknown", detail=f"no repo at {repo}"
        )

    manifests, language = _discover_manifests(repo)
    language = language_hint.lower() or language

    python_manifests = _select_python_manifests(
        [p for p in manifests if p.name not in NODE_MANIFESTS]
    )
    has_node = any(p.name in NODE_MANIFESTS for p in manifests)

    results: list[AuditResult] = []
    if venv_path and (python_manifests or not has_node):
        results.append(_run_pip_audit_env(repo, Path(venv_path).expanduser().resolve()))
    elif python_manifests:
        results.extend(_run_pip_audit(repo, p) for p in python_manifests)
    if has_node:
        results.append(_run_npm_audit(repo))

    if results:
        return _merge_scans(results, language or "python")

    # An explicit venv without manifests still audits the environment.
    if venv_path:
        return _run_pip_audit_env(repo, Path(venv_path).expanduser().resolve())

    if language in ("python", "py"):
        return AuditResult(
            STATUS_NO_MANIFEST,
            "python",
            detail="python contract has no manifest and no venv_path for env audit",
        )
    if language in ("typescript", "javascript", "ts", "js", "node"):
        return _run_npm_audit(repo)
    return AuditResult(
        STATUS_NO_MANIFEST,
        language or "unknown",
        detail="no python/js dependency manifest found in repo",
    )


def gate_summary(results: list[AuditResult]) -> str:
    """Human-readable one-line summary for a card's QC report."""
    if not results:
        return "dependency audit: nothing to scan"
    parts = [f"{r.language}:{r.status}" for r in results]
    return "dependency audit [" + ", ".join(parts) + "]"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m factory.audit --repo X [--venv Y].

    Prints a JSON result on stdout and exits 0 only when the audit passed (or
    is inapplicable because the repo has no dependency manifest). Every
    unverified result -- including a tool error, an empty report or a skipped
    dependency -- exits nonzero so a caller cannot treat it as clean.
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
    passed = result.status in PASSING_STATUSES
    print(json.dumps({"passed": passed, "result": result.to_dict()}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
