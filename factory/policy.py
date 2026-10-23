"""
Docker Compose safety policy for the Software Factory.

This module is the ONLY sanctioned entry point for factory code to touch
Docker. Every docker/docker-compose invocation the factory runner makes
must pass through assert_factory_project() and assert_safe_compose_file()
first. No other module should shell out to `docker` or `docker compose`
directly.

Hard rule: the factory may only ever create, inspect, and destroy
resources whose project name it generated itself, prefixed `factory-`.
It must never target, stop, restart, or reconfigure any pre-existing
Compose project on this host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Example protected project names. Add host-specific protection using
# FACTORY_HOST_CONFIG; configuration never removes these defaults. Changes to any non-factory project already fail
# postflight; this set classifies those that require a durable incident marker.
PROTECTED_PROJECTS = {
    "example-production",
    "example-memory",
    "example-transcription",
}

FACTORY_PROJECT_PATTERN = re.compile(r"^factory-[a-z0-9][a-z0-9\-]*$")

# Docker commands that are permanently banned from the factory runner,
# regardless of arguments. These act host-wide, not project-scoped, so
# no amount of project-name checking makes them safe.
BANNED_COMMAND_FRAGMENTS = (
    "system prune",
    "volume prune",
    "network prune",
    "container prune",
    "builder prune",
    "docker restart",
    "docker kill",
    "compose down",  # only allowed via run_compose() with -p enforced, never raw
)


class PolicyViolation(RuntimeError):
    """Raised when a requested Docker operation violates factory policy."""


def _host_config() -> dict:
    """Read only an explicitly selected operator configuration, never repo discovery."""
    filename = os.environ.get("FACTORY_HOST_CONFIG")
    if filename is None:
        return {}
    try:
        path = Path(filename).expanduser()
        if not filename or not path.is_absolute():
            raise ValueError("path must be absolute")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) - {
            "protected_projects",
            "reserved_host_ports",
        }:
            raise ValueError("expected protected_projects and/or reserved_host_ports")
        projects = data.get("protected_projects", [])
        ports = data.get("reserved_host_ports", [])
        if not isinstance(projects, list) or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name)
            for name in projects
        ):
            raise ValueError(
                "protected_projects must be a list of Compose project names"
            )
        if not isinstance(ports, list) or any(
            type(port) is not int or not 1 <= port <= 65535 for port in ports
        ):
            raise ValueError("reserved_host_ports must be a list of ports in 1..65535")
        return data
    except (OSError, ValueError) as exc:
        raise PolicyViolation(f"invalid host config: {exc}") from exc


def protected_projects() -> set[str]:
    return PROTECTED_PROJECTS | set(_host_config().get("protected_projects", []))


def reserved_host_ports() -> set[int]:
    return FACTORY_RESERVED_HOST_PORTS | set(
        _host_config().get("reserved_host_ports", [])
    )


@dataclass
class ProtectedProjectSnapshot:
    """Pre/post state of ALL Docker containers on the host, for postflight
    verification.

    Keyed by Compose project label; containers not managed by a Compose
    project (bare ``docker run``) are tracked in ``unlabeled``. The factory
    owns only ``factory-*`` projects, so any other container whose state
    changes across a run is collateral damage -- this is default-deny, not an
    allowlist of the projects we happen to know about.
    """

    projects: dict = field(
        default_factory=dict
    )  # project -> sorted list of (name, id, status)
    unlabeled: list = field(default_factory=list)  # sorted list of (name, id, status)

    @classmethod
    def capture(cls) -> ProtectedProjectSnapshot:
        snap: dict = {}
        unlabeled: list = []
        try:
            out = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--format",
                    '{{.Label "com.docker.compose.project"}}\t{{.Names}}\t{{.ID}}\t{{.State}}',
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout
        except Exception as exc:  # pragma: no cover - defensive
            raise PolicyViolation(f"could not snapshot docker state: {exc}") from exc

        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            project, name, cid, status = parts
            if project:
                snap.setdefault(project, []).append((name, cid, status))
            else:
                unlabeled.append((name, cid, status))

        for containers in snap.values():
            containers.sort()
        unlabeled.sort()
        return cls(projects=snap, unlabeled=unlabeled)

    def diff(self, other: ProtectedProjectSnapshot) -> list[str]:
        """Return a list of human-readable violations, empty if all clear."""
        report = self.collateral_damage_report(other)
        return report["protected"] + report["unexpected"]

    def collateral_damage_report(
        self, other: ProtectedProjectSnapshot
    ) -> dict[str, list[str]]:
        """Classify pre/post changes without treating routine host churn as a
        protected-resource incident.

        Every non-factory change remains a failed factory run. Only changes to
        the explicit protected-project set merit a durable HITL danger marker;
        unknown Compose projects and bare containers are reportable collateral
        warnings because a busy host can change them independently of a run.
        """
        report = {"protected": [], "unexpected": []}
        protected = protected_projects()
        for project in sorted(set(self.projects) | set(other.projects)):
            if FACTORY_PROJECT_PATTERN.match(project) and project not in protected:
                # Factory-owned: its create/destroy lifecycle is the runner's own
                # responsibility, not collateral damage (a concurrent factory run
                # is legitimate and must not trip this check).
                continue
            before = self.projects.get(project, [])
            after = other.projects.get(project, [])
            if before != after:
                category = "protected" if project in protected else "unexpected"
                report[category].append(
                    f"project '{project}' changed: before={before} after={after}"
                )
        if self.unlabeled != other.unlabeled:
            report["unexpected"].append(
                f"non-compose containers changed: "
                f"before={self.unlabeled} after={other.unlabeled}"
            )
        return report


def assert_factory_project(project_name: str) -> None:
    """Raise PolicyViolation unless project_name is a factory-owned, non-protected name."""
    if not project_name:
        raise PolicyViolation("empty project name")
    if project_name in protected_projects():
        raise PolicyViolation(
            f"refusing to operate on protected project '{project_name}'"
        )
    if not FACTORY_PROJECT_PATTERN.match(project_name):
        raise PolicyViolation(
            f"project name '{project_name}' does not match required pattern "
            f"'{FACTORY_PROJECT_PATTERN.pattern}' -- factory only owns 'factory-*' projects"
        )


def new_factory_project(card_id: str, short_sha: str = "") -> str:
    """Generate a unique, policy-compliant factory project name."""
    nonce = uuid.uuid4().hex[:6]
    parts = ["factory", _slug(card_id)]
    if short_sha:
        parts.append(_slug(short_sha))
    parts.append(nonce)
    name = "-".join(parts).lower()
    assert_factory_project(name)  # sanity check our own generator
    return name


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def assert_safe_command(command_str: str) -> None:
    """Raise PolicyViolation if a raw docker/compose command string is host-wide dangerous."""
    lowered = command_str.lower()
    for fragment in BANNED_COMMAND_FRAGMENTS:
        if fragment in lowered:
            raise PolicyViolation(
                f"command contains banned fragment '{fragment}': {command_str}"
            )


# Host path roots no factory bind-mount may ever touch. Applies to absolute
# sources and to relative sources after resolution against the repo root.
SENSITIVE_HOST_ROOTS = (
    "/etc",
    "/var",
    "/home",
    "/Users",
    "/private/etc",
    "/root",
    "/proc",
    "/sys",
    "/dev",
)

# System roots a relative source may never resolve into. The narrow exception
# for repository-relative mounts covers only checkouts nested below a user's
# home directory (``/Users/<user>/...`` or ``/home/<user>/...``); a checkout
# rooted at or under one of these system paths gets no relative-mount
# exception. ``/Users`` and ``/home`` themselves are rejected as repository
# roots by the immediate-home check.
SENSITIVE_SYSTEM_ROOTS = (
    "/etc",
    "/private/etc",
    "/var",
    "/run",
    "/dev",
    "/root",
    "/proc",
    "/sys",
)

# Prefixes whose immediate child is a user home directory. A repository must
# be nested at least one level below the home directory to qualify for the
# relative-mount exception.
HOME_ROOT_PREFIXES = ("/Users", "/home")

# Repo-relative subtrees that carry credentials, VCS metadata, or registry
# tokens. A repository checked out under a home root may mount its own
# ordinary files, but never these subtrees.
SENSITIVE_REPO_SUBPATHS = (
    ".git",
    ".git-credentials",
    ".gitconfig",
    ".ssh",
    ".aws",
    ".gnupg",
    ".docker",
    ".kube",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".env",
    "secrets",
)

# The whole ``.env`` family (``.env.local``, ``.env.production``, ...) carries
# environment credentials, so these names match by prefix rather than by exact
# component identity.
SENSITIVE_REPO_PREFIXES = (".env",)

# The repo containing this policy module; relative bind-mount sources are
# resolved against it.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_sensitive_component(part: str) -> bool:
    lowered = part.lower()
    return lowered in SENSITIVE_REPO_SUBPATHS or lowered.startswith(
        SENSITIVE_REPO_PREFIXES
    )


def _is_immediate_home_dir(path: Path) -> bool:
    for prefix in HOME_ROOT_PREFIXES:
        base = Path(prefix)
        if path != base and path.is_relative_to(base):
            return len(path.relative_to(base).parts) == 1
    return False


def _is_home_checkout(root: Path) -> bool:
    """True only for a checkout nested below a user's home directory."""
    for system_root in SENSITIVE_SYSTEM_ROOTS:
        sroot = Path(system_root)
        if root == sroot or root == sroot.resolve() or root.is_relative_to(sroot):
            return False
    for prefix in HOME_ROOT_PREFIXES:
        base = Path(prefix)
        if root != base and root.is_relative_to(base):
            return len(root.relative_to(base).parts) >= 2
    return False


def assert_safe_repo_root(repo_root: Path | str) -> None:
    """Reject repository roots that must never anchor a relative bind mount.

    A subject repository may live in an ordinary checkout, but it may not be
    the host root, a sensitive host root, the operator's home directory, or an
    immediate home directory (``/Users/<user>``). The runner calls this before
    Compose validation so a sensitive ``repo_path`` never reaches file
    resolution or Docker.
    """
    root = Path(repo_root).expanduser().resolve()
    # Declaring a credential directory (or its child) as --repo must not
    # reclassify its contents as ordinary source. Inspect names, not contents.
    if any(_is_sensitive_component(part) for part in root.parts):
        raise PolicyViolation(
            f"repository root '{root}' is inside a sensitive credential or VCS path"
        )
    if root == Path("/"):
        raise PolicyViolation(
            f"repository root '{root}' is the host root; refusing to treat the "
            f"whole filesystem as a subject repository"
        )
    for sensitive in SENSITIVE_HOST_ROOTS:
        sroot = Path(sensitive)
        if root == sroot or root == sroot.resolve():
            raise PolicyViolation(
                f"repository root '{root}' is sensitive host path '{sensitive}'; "
                f"refusing to treat a system path as a subject repository"
            )
    if root == Path.home().resolve() or _is_immediate_home_dir(root):
        raise PolicyViolation(
            f"repository root '{root}' is a user home directory; a subject "
            f"repository must be nested below it"
        )


def _check_sensitive_repo_subpath(
    resolved: Path, root: Path, source: str, where: str
) -> None:
    """Reject repo-contained paths that hold credentials or VCS metadata."""
    try:
        relative = resolved.relative_to(root)
    except ValueError:  # pragma: no cover - callers prove containment first
        return
    for part in relative.parts:
        if _is_sensitive_component(part):
            raise PolicyViolation(
                f"{where}mounting sensitive repository path '{source}' "
                f"(resolves to '{resolved}') is forbidden"
            )


def _check_bind_contents(resolved: Path, root: Path, source: str, where: str) -> None:
    """Inspect a relative mount source by path metadata only.

    The mounted tree may not contain credential/VCS names at any depth, and no
    symlink may point at the repository root, out of the repository, or into a
    sensitive subtree. Directory entries are inspected with ``os.scandir``;
    file *contents* are never read. Traversal follows only directories and
    de-duplicates resolved paths, so symlink cycles terminate. Any entry that
    cannot be inspected fails closed.
    """
    try:
        exists = resolved.exists()
    except OSError as exc:
        raise PolicyViolation(
            f"{where}cannot stat relative bind-mount source '{source}' "
            f"('{resolved}': {exc}); failing closed"
        ) from exc
    if not exists:
        raise PolicyViolation(
            f"{where}relative bind-mount source '{source}' does not exist "
            f"under '{root}'; create it before validation or mount an "
            f"existing path"
        )
    if not resolved.is_dir():
        return

    pending = [resolved]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        try:
            real = current.resolve(strict=True)
            if real in seen:
                continue
            seen.add(real)
            if real != root and not real.is_relative_to(root):
                raise PolicyViolation(
                    f"{where}relative bind-mount source '{source}' resolves "
                    f"outside the repo root '{root}' at '{current}' -- "
                    f"relative escapes are forbidden"
                )
            with os.scandir(real) as entries:
                items = list(entries)
        except OSError as exc:
            raise PolicyViolation(
                f"{where}cannot inspect relative bind-mount source '{source}' "
                f"at '{current}' ({exc}); failing closed rather than mounting "
                f"a tree whose contents cannot be verified"
            ) from exc

        for entry in items:
            if _is_sensitive_component(entry.name):
                raise PolicyViolation(
                    f"{where}relative bind-mount source '{source}' contains "
                    f"sensitive path '{entry.path}' (credential or metadata "
                    f"subtree)"
                )
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise PolicyViolation(
                    f"{where}cannot inspect '{entry.path}' inside bind-mount "
                    f"source '{source}' ({exc}); failing closed"
                ) from exc
            if is_symlink:
                try:
                    target = Path(entry.path).resolve(strict=True)
                except OSError as exc:
                    raise PolicyViolation(
                        f"{where}bind-mount source '{source}' contains an "
                        f"unresolvable symlink '{entry.path}' ({exc}); failing "
                        f"closed"
                    ) from exc
                if target == root:
                    raise PolicyViolation(
                        f"{where}bind-mount source '{source}' contains symlink "
                        f"'{entry.path}' pointing at the repository root; "
                        f"repository-root mounts are forbidden"
                    )
                if not target.is_relative_to(root):
                    raise PolicyViolation(
                        f"{where}bind-mount source '{source}' contains symlink "
                        f"'{entry.path}' resolving outside the repo root "
                        f"'{root}' to '{target}' -- symlink escapes are "
                        f"forbidden"
                    )
                for part in target.relative_to(root).parts:
                    if _is_sensitive_component(part):
                        raise PolicyViolation(
                            f"{where}bind-mount source '{source}' contains "
                            f"symlink '{entry.path}' pointing into sensitive "
                            f"repository path '{target}'"
                        )
                try:
                    target_is_dir = target.is_dir()
                except OSError as exc:
                    raise PolicyViolation(
                        f"{where}cannot inspect symlink target '{target}' inside "
                        f"bind-mount source '{source}' ({exc}); failing closed"
                    ) from exc
                if target_is_dir:
                    pending.append(target)
            elif is_dir:
                pending.append(Path(entry.path))


def check_bind_source(
    source: str, repo_root: Path | None = None, name: str = ""
) -> None:
    """Gate a single bind-mount source string.

    Bare names (no path separators) are compose-internal named volumes and
    pass.

    Absolute sources must not point at sensitive host roots (/etc, /var,
    /Users, /home, ...), checked both raw and resolved so symlink indirection
    is caught.

    Relative sources are resolved against the repo root. The exception is
    deliberately narrow: the repo must be a real checkout nested below a user
    home directory (``/Users/<user>/...`` or ``/home/<user>/...``). The
    source must exist, stay inside the repo root (catching traversal and
    symlink escapes), not resolve to the repo root itself, and not name or
    contain a credential/VCS subtree. Anything else -- including repositories
    under system roots such as ``/etc`` or ``/var`` -- fails closed. The
    absolute spelling of a home-checked-out path is still rejected.
    """
    where = f"service '{name}': " if name else ""
    # Named volume: bare identifier, no separators at all.
    if "/" not in source and "\\" not in source and not source.startswith("."):
        return
    root = Path(repo_root or _REPO_ROOT).resolve()
    src_path = Path(source).expanduser()

    if not src_path.is_absolute():
        assert_safe_repo_root(root)
        if not _is_home_checkout(root):
            raise PolicyViolation(
                f"{where}relative bind-mount source '{source}' is only allowed "
                f"for a checkout nested under a user home directory "
                f"(/Users/<user>/... or /home/<user>/...); repo root '{root}' "
                f"is not"
            )
        resolved = (root / src_path).resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise PolicyViolation(
                f"{where}bind-mount source '{source}' resolves to "
                f"'{resolved}', outside the repo root '{root}' -- relative "
                f"escapes are forbidden"
            )
        if resolved == root:
            raise PolicyViolation(
                f"{where}mounting the repository root ('{source}' resolves to "
                f"'{resolved}') is forbidden; mount an ordinary subdirectory "
                f"instead"
            )
        _check_sensitive_repo_subpath(resolved, root, source, where)
        _check_bind_contents(resolved, root, source, where)
        return

    resolved = src_path.resolve()
    # Sensitive roots are checked against BOTH the raw path and the resolved
    # one (on macOS /etc is a symlink into /private/etc, so resolution can
    # move a path between roots).
    candidates = {src_path, resolved}
    for candidate in candidates:
        for sensitive in SENSITIVE_HOST_ROOTS:
            sroot = Path(sensitive)
            if candidate == sroot or candidate.is_relative_to(sroot):
                raise PolicyViolation(
                    f"{where}mounting sensitive host path '{source}' "
                    f"(resolves to '{resolved}') is forbidden "
                    f"(under '{sensitive}')"
                )


def _check_repo_path(
    source: str, repo_root: Path, *, kind: str, name: str = ""
) -> None:
    """Reject Compose file inputs that resolve outside the subject repo."""
    _assert_static_security_input(source, kind)
    path = Path(source).expanduser()
    resolved = (path if path.is_absolute() else repo_root / path).resolve()
    root = repo_root.resolve()
    if not resolved.is_relative_to(root):
        where = f"service '{name}': " if name else ""
        raise PolicyViolation(
            f"{where}{kind} path '{source}' resolves outside the repo root '{root}'"
        )


def _assert_static_security_input(value: object, field: str) -> None:
    """Security decisions must not precede Compose variable substitution."""
    if isinstance(value, str) and "$" in value:
        raise PolicyViolation(
            f"{field} uses interpolation and cannot be statically proven safe"
        )
    if isinstance(value, dict):
        for item in value.values():
            _assert_static_security_input(item, field)
    elif isinstance(value, list):
        for item in value:
            _assert_static_security_input(item, field)


def assert_safe_compose_file(
    compose_dict: dict,
    repo_root: Path | str | None = None,
    *,
    require_services: bool = True,
) -> None:
    """
    Statically reject dangerous Compose configuration before it is ever
    built or started. compose_dict is the parsed YAML (via yaml.safe_load).
    """
    services = compose_dict.get("services", {}) or {}
    if require_services and not services:
        raise PolicyViolation("compose file has no services")

    # The validator must inspect the complete model. These Compose features
    # load or inherit configuration from files that this parser does not
    # resolve, allowing a dangerous service to bypass the static checks.
    if compose_dict.get("include"):
        raise PolicyViolation(
            "compose include is forbidden; validate one self-contained file set"
        )

    for name, svc in services.items():
        for key in (
            "volumes",
            "env_file",
            "security_opt",
            "restart",
            "build",
            "network_mode",
            "pid",
            "ipc",
            "userns_mode",
        ):
            # Build args and inline Dockerfiles may legitimately reference
            # variables; only build paths and isolation controls are gated.
            value = svc.get(key)
            if key == "build" and isinstance(value, dict):
                value = {
                    k: v
                    for k, v in value.items()
                    if k not in ("args", "dockerfile_inline")
                }
            _assert_static_security_input(value, f"service '{name}': {key}")
        if svc.get("extends"):
            raise PolicyViolation(
                f"service '{name}': extends is forbidden; inherited Compose files are not validated"
            )
        build = svc.get("build")
        if build:
            context = build if isinstance(build, str) else build.get("context", ".")
            if not isinstance(context, str) or "$" in context:
                raise PolicyViolation(
                    f"service '{name}': build context must be a static repo-relative path"
                )
            _check_repo_path(
                context,
                Path(repo_root or _REPO_ROOT).resolve(),
                kind="build context",
                name=name,
            )
            if isinstance(build, dict) and build.get("dockerfile"):
                dockerfile = Path(str(build["dockerfile"]))
                if dockerfile.is_absolute() or ".." in dockerfile.parts:
                    raise PolicyViolation(
                        f"service '{name}': Dockerfile must remain inside the build context"
                    )

        env_files = svc.get("env_file", []) or []
        if isinstance(env_files, str):
            env_files = [env_files]
        for env_file in env_files:
            source = env_file if isinstance(env_file, str) else env_file.get("path", "")
            if not isinstance(source, str) or not source:
                raise PolicyViolation(f"service '{name}': invalid env_file path")
            _check_repo_path(
                source,
                Path(repo_root or _REPO_ROOT).resolve(),
                kind="env_file",
                name=name,
            )

        if svc.get("privileged"):
            raise PolicyViolation(f"service '{name}': privileged: true is forbidden")

        for host_mode_key in ("network_mode", "pid", "ipc", "userns_mode"):
            mode = svc.get(host_mode_key)
            if isinstance(mode, str) and "$" in mode:
                raise PolicyViolation(
                    f"service '{name}': {host_mode_key} uses interpolation "
                    "and cannot be statically proven safe"
                )
            if mode == "host":
                raise PolicyViolation(
                    f"service '{name}': {host_mode_key}: host is forbidden"
                )

        cap_add = svc.get("cap_add", []) or []
        if cap_add:
            raise PolicyViolation(
                f"service '{name}': cap_add is forbidden (any Linux capability "
                f"addition is a privilege escalation; the default container "
                f"capability set is already sufficient for factory jobs)"
            )

        devices = svc.get("devices", []) or []
        if devices:
            raise PolicyViolation(
                f"service '{name}': devices are forbidden (host device access "
                f"bypasses the isolation boundary)"
            )

        for opt in svc.get("security_opt", []) or []:
            lowered = str(opt).lower()
            if (
                "unconfined" in lowered
                or "label:disable" in lowered
                or "label=disable" in lowered
            ):
                raise PolicyViolation(
                    f"service '{name}': security_opt '{opt}' relaxes the container "
                    f"confinement boundary (seccomp/apparmor/label) and is forbidden"
                )

        if svc.get("container_name"):
            raise PolicyViolation(
                f"service '{name}': explicit container_name is forbidden "
                f"(factory requires project-scoped auto-generated names)"
            )

        restart_policy = svc.get("restart")
        if restart_policy in ("always", "unless-stopped"):
            raise PolicyViolation(
                f"service '{name}': restart: {restart_policy} is forbidden "
                f"for disposable factory jobs (use 'no' or omit)"
            )

        for vol in svc.get("volumes", []) or []:
            raw = vol if isinstance(vol, str) else vol.get("source", "")
            # Long-form "source:target[:mode]" strings need the source
            # isolated before we evaluate host-path risk; short-form named
            # volumes ("mydata") or dict form pass source through as-is.
            source = raw.split(":", 1)[0] if isinstance(vol, str) else raw
            if "/var/run/docker.sock" in raw:
                raise PolicyViolation(
                    f"service '{name}': mounting the Docker socket is forbidden"
                )
            if source.rstrip("/") == "":
                raise PolicyViolation(
                    f"service '{name}': mounting host root is forbidden"
                )
            # Named volumes (bare names, no path separators) are
            # compose-internal and skip the host-path check entirely --
            # resolving them against the CWD would falsely flag every
            # legitimate named volume on a /Users host. Anything with a
            # path separator (including "../x" and "./data") is a path and
            # is resolved + gated below.
            check_bind_source(source, name=name, repo_root=repo_root)

        ports = svc.get("ports", []) or []
        if ports:
            raise PolicyViolation(
                f"service '{name}': host port publication is forbidden by default "
                f"(use the internal compose network; escalate explicitly if a port is required)"
            )

    networks = compose_dict.get("networks", {}) or {}
    for net_name, net_cfg in networks.items():
        if isinstance(net_cfg, dict) and net_cfg.get("external"):
            raise PolicyViolation(
                f"network '{net_name}': external networks are forbidden "
                f"(factory networks must be created fresh per run)"
            )

    volumes = compose_dict.get("volumes", {}) or {}
    for vol_name, vol_cfg in volumes.items():
        if not isinstance(vol_cfg, dict):
            continue
        if vol_cfg.get("external"):
            raise PolicyViolation(
                f"volume '{vol_name}': external volumes are forbidden "
                f"(factory volumes must be ephemeral per run)"
            )
        if vol_cfg.get("name"):
            raise PolicyViolation(
                f"volume '{vol_name}': explicit name is forbidden "
                "(volume names must remain project-scoped)"
            )
        if vol_cfg.get("driver") not in (None, "local"):
            raise PolicyViolation(
                f"volume '{vol_name}': custom volume drivers are forbidden"
            )
        opts = vol_cfg.get("driver_opts") or {}
        if isinstance(opts, dict) and any(
            str(opts.get(k, "")).lower() in {"bind", "none"} or k == "device"
            for k in opts
        ):
            raise PolicyViolation(
                f"volume '{vol_name}': bind-style driver_opts are forbidden"
            )

    for section in ("secrets", "configs"):
        entries = compose_dict.get(section, {}) or {}
        for item_name, item in entries.items():
            if isinstance(item, dict) and item.get("external"):
                raise PolicyViolation(
                    f"{section} '{item_name}': external resources are forbidden"
                )
            if isinstance(item, dict) and item.get("file"):
                _check_repo_path(
                    item["file"],
                    Path(repo_root or _REPO_ROOT).resolve(),
                    kind=f"{section} file",
                )


# Compose global flags a caller may never re-supply: the project name and the
# compose file set are policy-controlled. Both the spaced, ``=``, and compact
# (``-pNAME`` / ``-fFILE``) spellings are rejected.
BANNED_COMPOSE_GLOBAL_FLAGS = (
    "-p",
    "--project-name",
    "--project-directory",
    "-f",
    "--file",
)


def _forbidden_global_flag(token: str) -> str | None:
    """Return the banned global flag a token supplies, if any."""
    name = token.partition("=")[0]
    if name in BANNED_COMPOSE_GLOBAL_FLAGS:
        return name
    # Short options accept a compact value (``-pNAME``, ``-fFILE``); long
    # options are already covered by the exact match above.
    if token.startswith("-") and not token.startswith("--") and len(token) > 2:
        if token[1] in ("p", "f"):
            return token[:2]
    return None


def run_compose(
    project_name: str,
    compose_files: list[str],
    args: list[str],
    timeout: int = 900,
) -> subprocess.CompletedProcess:
    """
    The ONLY sanctioned way to invoke `docker compose` from factory code.
    Enforces project-name policy before every call.
    """
    assert_factory_project(project_name)
    if not compose_files:
        raise PolicyViolation("no compose files specified")

    # Reject caller-supplied global Compose flags: project name and file set
    # are the factory's own enforced inputs, so letting args re-specify them
    # would let a caller bypass the policy checks above.
    for arg in args:
        token = str(arg)
        flag = _forbidden_global_flag(token)
        if flag:
            raise PolicyViolation(
                f"caller-supplied global Compose flag '{flag}' is forbidden in "
                f"args (project name and compose files are policy-controlled); "
                f"got {token!r}"
            )

    cmd = ["docker", "compose", "--project-name", project_name]
    for f in compose_files:
        cmd += ["--file", f]
    cmd += args

    assert_safe_command(" ".join(cmd))

    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def teardown(
    project_name: str, compose_files: list[str]
) -> subprocess.CompletedProcess:
    """Tear down ONLY the named factory project's own resources."""
    assert_factory_project(project_name)
    return run_compose(
        project_name,
        compose_files,
        ["down", "--volumes", "--remove-orphans", "--timeout", "30"],
    )


def get_host_ports_in_use() -> set[int]:
    """Return the set of host TCP ports currently bound by any container.

    Queries live Docker state so port allocation can avoid collisions with
    BOTH protected projects AND any concurrent factory run. Deterministic
    safety net for the rare case a card needs a real published host port
    (policy still rejects ports by default -- this is the explicit
    escalation path, not the norm).
    """
    used: set[int] = set()
    try:
        out = subprocess.run(
            [
                "docker",
                "ps",
                "--format",
                "{{.Ports}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except Exception as exc:  # pragma: no cover - defensive
        raise PolicyViolation(f"could not read host port bindings: {exc}") from exc

    import re as _re

    # Matches 0.0.0.0:5433->5432/tcp, [::]:5433->5432/tcp,
    # 127.0.0.1:5433->5432/tcp and bare 8000->8000/tcp
    for match in _re.finditer(r"(?:0\.0\.0\.0|127\.0\.0\.1|\[::\]):(\d+)->", out):
        used.add(int(match.group(1)))
    return used


# Host ports reserved by this factory as an off-spec convention, so no
# card ever picks a port a future protected project is likely to want.
# The live Docker scan above is the authoritative collision gate; this
# constant is a second, static, action-at-a-distance guard.
FACTORY_RESERVED_HOST_PORTS = {
    5432,
    5433,
    5434,  # postgres instances across projects
    6333,  # qdrant (example service)
    6379,  # redis (example service)
    7474,
    7687,  # neo4j (example service)
    8000,
    8001,  # example application ports
    8020,  # example service
    8086,  # influxdb (example service)
}


def _parse_port_allowlist(value: str) -> list[int]:
    """Parse an explicit comma-separated host-port allowlist.

    Raises PolicyViolation for empty, non-numeric, signed, decimal, or
    out-of-range entries so a malformed request fails before any host probe.
    """
    tokens = str(value).split(",")
    if any(not token.strip() for token in tokens):
        raise PolicyViolation(
            "port allowlist must be comma-separated integers in 1..65535; "
            f"got {value!r}"
        )
    ports: list[int] = []
    for token in tokens:
        text = token.strip()
        # isdigit() rejects the signs, decimals, and words int() would not.
        if not text.isdigit():
            raise PolicyViolation(
                "port allowlist must be comma-separated integers in 1..65535; "
                f"got {value!r}"
            )
        port = int(text)
        if not 1 <= port <= 65535:
            raise PolicyViolation(
                f"port allowlist entry {port} is outside the valid range 1..65535"
            )
        ports.append(port)
    return ports


def allocate_host_ports(n: int = 1, avoid: str = "CONFLICT") -> list[int]:
    """
    Allocate `n` free host TCP ports for a factory test that genuinely
    requires a published port. Lock-guarded (advisory atomic-file lock, per
    the reconciling convention from mac-mini-2's runner) so concurrent
    factory runs never hand out the same host port. Verifies against BOTH
    live host bindings (lsof) AND docker port mappings AND the reserved set.

    avoid:
      "CONFLICT"        -- selected ports must be not-in-use on the host
                           AND not in FACTORY_RESERVED_HOST_PORTS
      else              -- treated as an ALLOWLIST of specific ports to
                           check (each must be currently free); returns only
                           the ones that are free.

    Request and operator-configuration validation happen BEFORE any host or
    Docker inspection, so a bad allowlist or invalid FACTORY_HOST_CONFIG fails
    closed without probing the host.
    """
    if avoid != "CONFLICT":
        allowlist = _parse_port_allowlist(avoid)
        # reserved_host_ports() validates FACTORY_HOST_CONFIG, so it must run
        # before the live Docker scan below.
        reserved = reserved_host_ports()
        used = get_host_ports_in_use() | reserved
        free = [p for p in allowlist if p not in used]
        if len(free) != len(allowlist):
            taken = [p for p in allowlist if p not in free]
            raise PolicyViolation(f"requested ports already in use {taken}")
        return free

    with _acquire_port_lock():
        picked: list[int] = []
        for port in range(FACTORY_PORT_RANGE_START, FACTORY_PORT_RANGE_END):
            if _is_factory_port_free(port):
                picked.append(port)
                if len(picked) == n:
                    return sorted(picked)
    raise PolicyViolation(
        f"ran out of free host ports in factory range "
        f"{FACTORY_PORT_RANGE_START}-{FACTORY_PORT_RANGE_END}"
    )


# Where this factory hands out ephemeral test ports. Deliberately a compact,
# easy-to-grep range distinct from both the OS ephemeral range and the
# protected projects' low ports, matching the mac-mini-2 convention. Change
# both constant lines together if you need a different span.
FACTORY_PORT_RANGE_START = 49000
FACTORY_PORT_RANGE_END = 49151

_PORT_LOCK = Path(__file__).resolve().parent / "ports" / ".lock"
_PORT_LOCK_STALE_S = 120  # a lock older than this is a crashed process's marker


def _acquire_port_lock():
    """Context manager: advisory lock via atomic O_CREAT|O_EXCL. The lock file
    records the acquiring PID and creation time. A held lock is stolen only if
    it is provably stale (older than _PORT_LOCK_STALE_S); a live holder's lock
    is NEVER unlinked out from under it, so the critical section can't
    double-grant."""
    import contextlib
    import os
    import time

    @contextlib.contextmanager
    def _lock():
        _PORT_LOCK.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 60
        fd = None
        while fd is None:
            try:
                fd = os.open(str(_PORT_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    stat = _PORT_LOCK.stat()
                    age = time.time() - stat.st_mtime
                    # Steal only a provably stale marker; never break a live lock.
                    if age > _PORT_LOCK_STALE_S:
                        _PORT_LOCK.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue  # vanished between check and open; retry immediately
                if time.time() >= deadline:
                    raise PolicyViolation(
                        "factory port lock held for over 60s by another process "
                        "-- refusing to break a possibly-live lock"
                    )
                time.sleep(1)
        try:
            os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode())
        finally:
            os.close(fd)
        try:
            yield
        finally:
            # Only remove the lock we ourselves still own (content unchanged).
            try:
                if _PORT_LOCK.read_text().startswith(str(os.getpid()) + " "):
                    _PORT_LOCK.unlink(missing_ok=True)
            except OSError:
                pass

    return _lock()


def _is_factory_port_free(port: int) -> bool:
    if port in reserved_host_ports():
        return False
    # lsof: is any HOST process bound to this port?
    import subprocess as _sp

    lsof = _sp.run(
        ["lsof", "-i", f":{port}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if lsof.returncode == 0 and lsof.stdout.strip():
        return False
    # docker: is any container mapping this host port?
    ps = _sp.run(
        ["docker", "ps", "--format", "{{.Ports}}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    ).stdout
    return f":{port}->" not in ps


def assert_worker_uses_isolated_environment(repo_contract: dict) -> None:
    """
    Enforce language-level isolation for non-Compose (bare implementation)
    cards -- i.e. the dev-worker running tests directly on the host, not
    inside a Compose service. Compose already isolates at the container
    level; this guard is for the common case where a worker runs
    `pytest`/`npm test`/etc. directly in a repo worktree on the shared
    host Python/Node install.

    repo_contract is the parsed factory.contract.yml for the repository.
    Raises PolicyViolation if the declared package manager has no
    isolation marker recorded (venv path for Python, node_modules /
    lockfile-scoped install for JS/TS, etc.) -- forces the spec/contract
    to say how isolation is achieved rather than silently using whatever
    interpreter happens to be first on PATH.
    """
    language = (repo_contract or {}).get("language", "").lower()
    isolation = (repo_contract or {}).get("isolation", {}) or {}

    if language in ("python", "py"):
        venv_path = isolation.get("venv_path")
        if not venv_path:
            raise PolicyViolation(
                "python repo contract missing isolation.venv_path -- factory "
                "workers must create/use a repo-local venv (e.g. '.venv'), "
                "never the shared system or profile Python"
            )
    elif language in ("typescript", "javascript", "ts", "js", "node"):
        # node_modules is project-local by default (npm/yarn/pnpm), but the
        # contract must still declare the package manager and lockfile so
        # the runner knows to run an install scoped to THIS repo, not a
        # globally-linked package cache.
        pm = isolation.get("package_manager") or repo_contract.get("package_manager")
        lockfile = isolation.get("lockfile")
        if not pm or not lockfile:
            raise PolicyViolation(
                "js/ts repo contract missing isolation.package_manager and/or "
                "isolation.lockfile -- factory workers must install "
                "dependencies scoped to this repo's lockfile, never a global "
                "install"
            )
    # Other languages: no enforced isolation marker yet. Extend here as
    # new languages are onboarded (Go modules, Rust cargo, etc. are
    # isolated by default via their toolchains and don't need this gate).


def verify_no_collateral_damage(
    before: ProtectedProjectSnapshot, after: ProtectedProjectSnapshot
) -> None:
    """Raise PolicyViolation if any non-factory container state changed."""
    report = before.collateral_damage_report(after)
    violations = report["protected"] + report["unexpected"]
    if violations:
        raise PolicyViolation(
            "PROTECTED PROJECT STATE CHANGED -- factory run must be treated as a "
            "safety incident, not a normal failure:\n" + "\n".join(violations)
        )
