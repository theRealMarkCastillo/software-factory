"""Factory worker role contracts and a read-only Hermes profile conformance check.

The engine ships role definitions (``integrations/hermes/roles``): a host-neutral
SOUL template, the curated skills a role needs, and the toolsets it must and must
not have. An installation binds concrete Hermes profiles to those roles in a JSON
binding file, optionally adding a per-host SOUL supplement and a model pin.

``factory roles check`` compares installed profiles with that binding. It reads
only ``SOUL.md``, ``config.yaml`` and the ``skills/`` tree of each bound profile
and prints toolset names, model identifiers and paths — never other config
values. It never writes to a profile; ``factory roles render`` prints the
expected SOUL so an operator can apply it deliberately.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

DEFAULT_ROLES_DIR = Path(__file__).resolve().parents[1] / "integrations/hermes/roles"
SCHEMA = 1
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_BINDING_KEYS = {"role", "soul_supplement", "model"}
_MODEL_KEYS = {"provider", "default"}


class RoleError(ValueError):
    """Invalid role manifest, binding, or arguments (exit status 2)."""


def _relative(value, what):
    path = Path(value) if isinstance(value, str) and value else None
    if path is None or path.is_absolute() or ".." in path.parts:
        raise RoleError(f"{what} must be a relative path inside its directory")
    return path


def _names(value, what):
    if not isinstance(value, list) or not all(
        isinstance(v, str) and _NAME.fullmatch(v) for v in value
    ):
        raise RoleError(f"{what} must be a list of lowercase names")
    return list(value)


def load_manifest(roles_dir: Path = DEFAULT_ROLES_DIR) -> dict[str, dict]:
    roles_dir = Path(roles_dir)
    path = roles_dir / "roles.json"
    if not path.is_file():
        raise RoleError(
            f"no role manifest at {path}; pass --roles-dir for a wheel install"
        )
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise RoleError(f"{path}: expected schema {SCHEMA}")
    roles = data.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise RoleError(f"{path}: roles must be a non-empty object")
    result = {}
    for name, spec in roles.items():
        if not _NAME.fullmatch(name) or not isinstance(spec, dict):
            raise RoleError(f"{path}: invalid role {name!r}")
        soul = _relative(spec.get("soul"), f"role {name} soul")
        if not (roles_dir / soul).is_file():
            raise RoleError(f"role {name}: missing template {soul}")
        required = _names(spec.get("required_toolsets"), f"{name} required_toolsets")
        forbidden = _names(spec.get("forbidden_toolsets"), f"{name} forbidden_toolsets")
        if set(required) & set(forbidden):
            raise RoleError(f"role {name}: toolset both required and forbidden")
        result[name] = {
            "soul": str(soul),
            "skills": _names(spec.get("skills"), f"{name} skills"),
            "required_toolsets": required,
            "forbidden_toolsets": forbidden,
        }
    return result


def _role(role, roles_dir):
    manifest = load_manifest(roles_dir)
    if role not in manifest:
        raise RoleError(f"unknown role {role!r}; known: {', '.join(sorted(manifest))}")
    return manifest[role]


def render(
    role: str, *, roles_dir: Path = DEFAULT_ROLES_DIR, supplement: Path | None = None
) -> str:
    """Return the SOUL text a profile bound to ``role`` must contain."""
    template = (Path(roles_dir) / _role(role, roles_dir)["soul"]).read_text()
    if supplement is None:
        return template
    extra = Path(supplement).read_text().strip("\n")
    return template.rstrip("\n") + "\n\n" + extra + "\n"


def load_binding(path: Path, roles_dir: Path = DEFAULT_ROLES_DIR) -> dict[str, dict]:
    path = Path(path)
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise RoleError(f"{path}: expected schema {SCHEMA}")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise RoleError(f"{path}: profiles must be a non-empty object")
    manifest = load_manifest(roles_dir)
    result = {}
    for name, entry in profiles.items():
        if not _NAME.fullmatch(name) or not isinstance(entry, dict):
            raise RoleError(f"{path}: invalid profile name {name!r}")
        unknown = set(entry) - _BINDING_KEYS
        if unknown:
            raise RoleError(f"profile {name}: unknown keys {sorted(unknown)}")
        if entry.get("role") not in manifest:
            raise RoleError(f"profile {name}: unknown role {entry.get('role')!r}")
        bound = {"role": entry["role"], "soul_supplement": None, "model": {}}
        if "soul_supplement" in entry:
            supplement = path.parent / _relative(
                entry["soul_supplement"], f"profile {name} soul_supplement"
            )
            if not supplement.is_file():
                raise RoleError(f"profile {name}: missing supplement {supplement}")
            bound["soul_supplement"] = supplement
        model = entry.get("model", {})
        if (
            not isinstance(model, dict)
            or set(model) - _MODEL_KEYS
            or not all(isinstance(v, str) and v for v in model.values())
        ):
            raise RoleError(f"profile {name}: model takes provider/default strings")
        bound["model"] = model
        result[name] = bound
    return result


def _skill_name(skill_dir: Path) -> str:
    text = (skill_dir / "SKILL.md").read_text(errors="replace")
    match = re.match(r"---\n(.*?)\n---", text, re.S)
    if match:
        found = re.search(r"^name:\s*['\"]?([^'\"\s]+)", match.group(1), re.M)
        if found:
            return found.group(1)
    return skill_dir.name


def _installed_skills(skills_root: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    seen = set()
    for current, dirs, files in os.walk(skills_root, followlinks=True):
        real = os.path.realpath(current)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        if "SKILL.md" in files:
            path = Path(current)
            found.setdefault(_skill_name(path), []).append(path)
            dirs[:] = []
        else:
            dirs.sort()
    return found


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for current, dirs, files in sorted(os.walk(root, followlinks=True)):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            file = Path(current) / name
            digest.update(str(file.relative_to(root)).encode() + b"\0")
            digest.update(file.read_bytes())
    return digest.hexdigest()


def _check_profile(profile, bound, spec, roles_dir, skills_source):
    findings = []
    soul = profile / "SOUL.md"
    expected = render(
        bound["role"], roles_dir=roles_dir, supplement=bound["soul_supplement"]
    )
    if not soul.is_file():
        findings.append("SOUL.md missing")
    elif (actual := soul.read_text()) != expected:
        changed = sum(
            1
            for line in difflib.unified_diff(
                expected.splitlines(), actual.splitlines(), lineterm="", n=0
            )
            if line[:1] in "+-" and line[:3] not in {"+++", "---"}
        )
        findings.append(
            f"SOUL.md differs from rendered role {bound['role']} "
            f"({changed} changed lines; see `factory roles render`)"
        )

    config_path = profile / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) if config_path.is_file() else {}
    config = config if isinstance(config, dict) else {}
    platforms = config.get("platform_toolsets")
    platforms = platforms if isinstance(platforms, dict) else {}
    cli = platforms.get("cli")
    if not isinstance(cli, list):
        findings.append(
            "platform_toolsets.cli is not configured; implicit Hermes defaults "
            "are not accepted for factory roles"
        )
    else:
        missing = [t for t in spec["required_toolsets"] if t not in cli]
        if missing:
            findings.append(f"missing toolsets on cli: {', '.join(missing)}")
    for platform in sorted(platforms):
        enabled = platforms[platform]
        if isinstance(enabled, list):
            for toolset in spec["forbidden_toolsets"]:
                if toolset in enabled:
                    findings.append(
                        f"forbidden toolset {toolset} enabled on {platform}"
                    )
    model = config.get("model")
    model = model if isinstance(model, dict) else {}
    for key, want in sorted(bound["model"].items()):
        if model.get(key) != want:
            findings.append(
                f"model.{key} is {model.get(key)!r}; binding expects {want!r}"
            )

    skills_root = profile / "skills"
    installed = _installed_skills(skills_root) if skills_root.is_dir() else {}
    for skill in spec["skills"]:
        paths = installed.get(skill, [])
        if not paths:
            findings.append(f"missing skill {skill}")
        elif len(paths) > 1:
            where = ", ".join(str(p.relative_to(skills_root)) for p in paths)
            findings.append(f"duplicate skill {skill}: {where}")
        elif skills_source is not None:
            source = Path(skills_source) / skill
            if not (source / "SKILL.md").is_file():
                findings.append(f"skill {skill} absent from source {skills_source}")
            elif _tree_hash(paths[0]) != _tree_hash(source):
                findings.append(f"skill {skill} differs from {source}")
    return findings


def check(
    binding: Path,
    *,
    hermes_home: Path,
    roles_dir: Path = DEFAULT_ROLES_DIR,
    skills_source: Path | None = None,
) -> dict[str, list[str]]:
    """Return findings per bound profile; an empty list means it conforms."""
    manifest = load_manifest(roles_dir)
    report = {}
    for name, bound in load_binding(binding, roles_dir).items():
        profile = Path(hermes_home) / "profiles" / name
        if not profile.is_dir():
            report[name] = [f"profile not found: {profile}"]
            continue
        report[name] = _check_profile(
            profile, bound, manifest[bound["role"]], roles_dir, skills_source
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory roles", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "render", "check"):
        p = sub.add_parser(command)
        p.add_argument("--roles-dir", type=Path, default=DEFAULT_ROLES_DIR)
        if command == "render":
            p.add_argument("role")
            p.add_argument("--supplement", type=Path)
        if command == "check":
            p.add_argument("--binding", type=Path, required=True)
            p.add_argument(
                "--hermes-home",
                type=Path,
                default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser(),
            )
            p.add_argument(
                "--skills-source",
                type=Path,
                help="also require installed role skills to match this tree",
            )
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for role, spec in sorted(load_manifest(args.roles_dir).items()):
                print(role, " ".join(spec["skills"]))
            return 0
        if args.command == "render":
            sys.stdout.write(
                render(args.role, roles_dir=args.roles_dir, supplement=args.supplement)
            )
            return 0
        bindings = load_binding(args.binding, args.roles_dir)
        report = check(
            args.binding,
            hermes_home=args.hermes_home,
            roles_dir=args.roles_dir,
            skills_source=args.skills_source,
        )
    except (RoleError, OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"factory roles: {exc}", file=sys.stderr)
        return 2
    drift = False
    for name, findings in report.items():
        role = bindings[name]["role"]
        print(f"{'DRIFT' if findings else 'OK'} {name} ({role})")
        for finding in findings:
            print(f"  - {finding}")
        drift = drift or bool(findings)
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
