"""Installed release identity for the pinned factory launcher.

The launcher (``software-factory-ops/bin/factory``) resolves
``.runtime/current`` and refuses to start without ``ready.json``; there is no
PATH fallback. This module records that identity so a checkpoint and its
operator report can name the exact release, engine fingerprint and policy digest
that gates actually ran under.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def engine_fingerprint(engine_dir: Path, *, version: str, python: str) -> str:
    """Recompute ``factory.evidence.engine_identity`` over an installed release.

    Canonical text: ``name|version|python_major.minor|`` then one
    ``name:sha256`` line per ``factory/*.py`` sorted by path.
    """

    package = engine_dir / "factory"
    files = sorted(package.glob("*.py"))
    lines = [f"{path.name}:{_sha256(path)}" for path in files]
    canonical = "\n".join([f"hermes-software-factory|{version}|{python}|", *lines])
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstalledRelease:
    ops_root: Path
    launcher: Path
    deployment: dict
    release_path: Path
    ready: dict
    engine_dir: Path
    engine_version: str
    python: str
    fingerprint: str
    ready_sha256: str
    deployment_sha256: str
    host_policy_sha256: str

    def as_dict(self) -> dict:
        payload = asdict(self)
        for key in ("ops_root", "launcher", "release_path", "engine_dir"):
            payload[key] = str(payload[key])
        return payload

    @property
    def reference(self) -> str:
        return f"installed:{self.release_path}@{self.fingerprint}"

    def launcher_help_ok(self, subcommand: str = "verify") -> bool:
        completed = subprocess.run(
            [str(self.launcher), subcommand, "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return completed.returncode == 0 and "usage" in completed.stdout.lower()


def default_ops_root() -> Path:
    """Resolve host installation configuration at use time, never on import."""

    configured = os.environ.get("FACTORY_OPS_ROOT")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / "git" / "software-factory-ops"
    )


def default_launcher() -> Path:
    return default_ops_root() / "bin" / "factory"


def discover(ops_root: str | Path | None = None) -> InstalledRelease:
    root = (
        (Path(ops_root) if ops_root is not None else default_ops_root())
        .expanduser()
        .resolve()
    )
    launcher = root / "bin" / "factory"
    deployment_path = root / "deployment.json"
    policy_path = root / "host-policy.json"
    release_path = (root / ".runtime" / "current").resolve(strict=True)
    ready_path = release_path / "ready.json"
    engine_dir = release_path / "engine"
    if not launcher.is_file():
        raise FileNotFoundError(f"installed launcher missing: {launcher}")
    if not ready_path.is_file():
        raise FileNotFoundError(f"release is not validated: {ready_path}")
    deployment = json.loads(deployment_path.read_text())
    ready = json.loads(ready_path.read_text())
    pyproject = tomllib.loads((engine_dir / "pyproject.toml").read_text())
    version = str(pyproject.get("project", {}).get("version", "0.0.0"))
    cfg = engine_dir / ".venv" / "pyvenv.cfg"
    python = "3.12"
    if cfg.is_file():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("version"):
                parts = line.split("=", 1)[1].strip().split(".")
                python = ".".join(parts[:2])
    return InstalledRelease(
        ops_root=root,
        launcher=launcher,
        deployment=deployment,
        release_path=release_path,
        ready=ready,
        engine_dir=engine_dir,
        engine_version=version,
        python=python,
        fingerprint=engine_fingerprint(engine_dir, version=version, python=python),
        ready_sha256="sha256:" + _sha256(ready_path),
        deployment_sha256="sha256:" + _sha256(deployment_path),
        host_policy_sha256="sha256:" + _sha256(policy_path),
    )
