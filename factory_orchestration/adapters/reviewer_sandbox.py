"""R2-A: a disposable, sandboxed reviewer configuration on Hermes's Docker backend.

The reviewer is *not* an existing Hermes profile. It is a throwaway home created
and owned by the factory under an artifact root, whose ``terminal.backend`` is
the supported Docker sandbox and whose only host mounts are the candidate and a
selected evidence tree, both read-only.

``SandboxedReadonlyAttestor.attest`` is the T10 gate. It executes three layers
and refuses (``capability_unenforced``) if any one fails:

1. **Effective configuration** read through the supported ``hermes config get``
   interface, compared with the expected pinned values.
2. **Preflight through the real tool stack** driven by the loopback scripted
   provider (not by prompt compliance), with negative cases for write, patch,
   symlink escape, host paths and out-of-surface tools.
3. **Container inspection and in-container probes** through
   :mod:`factory.policy` — network mode, read-only rootfs, uid, effective
   capabilities, a read-only ``/evidence``, no Docker socket and no egress.

What this is not: whole-agent containment. The Hermes launcher and its profile
home run as the host user; the container constrains *tool effects*. See the
residual list recorded in every receipt.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..errors import OrchestrationError
from .base import AttestationReceipt

CANDIDATE_MOUNT = "/evidence/candidate"
EVIDENCE_MOUNT = "/evidence/evidence"
PROBE_MOUNT = "/evidence/probe"
SANDBOX_MARKER = ".factory-reviewer-sandbox"
SANDBOX_SCHEMA = "factory-reviewer-attestation/v1"

#: The irreducible read-capable surface on this Hermes build (``-t file``).
EXPECTED_TOOL_NAMES = frozenset({"read_file", "search_files", "write_file", "patch"})

#: Loopback provider credential for the executed preflight only (never a real key).
PREFLIGHT_KEY = "factory-preflight-token"

#: Tools that must not exist on the reviewer surface. Each is attempted in its
#: own run because Hermes aborts a run on the first invalid tool call.
REJECTION_PROBES: dict[str, dict] = {
    "terminal": {"command": "id"},
    "execute_code": {"code": "print('escape')"},
    "kanban_complete": {"summary": "self-approved"},
    "delegate_task": {"goal": "bypass"},
}

#: ``python:3.12-slim`` as present on this host; the tag alone is not a pin.
DEFAULT_IMAGE = os.environ.get("FACTORY_REVIEWER_IMAGE", "")

RESIDUALS = (
    "Reading is exfiltration: anything mounted under /evidence can reach the "
    "configured model provider with the operator's credential.",
    "Hermes, the provider client, the sandbox image, the Docker daemon and the "
    "container runtime are trusted code; a malicious image or compromised daemon "
    "defeats the boundary.",
    "Kernel exec is not denied inside the container: Hermes's file tools are "
    "implemented as docker exec commands, so a Hermes bug could run code inside "
    "the container (not on the host).",
    "Ephemeral writes inside container tmpfs are allowed and discarded with the "
    "per-session container; they are not source/evidence mutation.",
    "The host-side Hermes launcher, its profile home and session DB run as the "
    "host user; the container constrains tool effects, not the launcher process.",
    "This is not graph-level isolation; the reviewer is a sibling process the "
    "adapter owns.",
    "Provider retention of candidate/evidence is outside this mechanism.",
)


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def tree_digest(root: Path, *, limit: int = 2000) -> str:
    """Deterministic digest of a small tree (paths + contents), bounded."""

    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        count += 1
        if count >= limit:
            break
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class SandboxSpec:
    """A disposable reviewer configuration the factory owns."""

    name: str
    root: Path
    candidate: Path
    evidence: Path
    image: str
    uid: int = 10001
    gid: int = 10001
    probe: Path | None = None

    @property
    def home(self) -> Path:
        return Path(self.root) / "profiles" / self.name

    @property
    def marker(self) -> Path:
        return self.home / SANDBOX_MARKER

    def config_path(self) -> Path:
        return self.home / "config.yaml"

    @property
    def candidate_digest(self) -> str:
        return tree_digest(Path(self.candidate))

    @property
    def evidence_digest(self) -> str:
        return tree_digest(Path(self.evidence))


def _assert_owned_root(root: Path) -> None:
    """Refuse to manage a real profile home as the sandbox root.

    The root may live *under* a profile's own scratch tree (that is where the
    factory's artifacts are kept), but it may not be the profile store itself
    or a direct child of it (which is a real, operator-owned profile home).
    """

    resolved = Path(root).expanduser().absolute()
    profile_store = (Path.home() / ".hermes" / "profiles").absolute()
    if resolved == profile_store or resolved.parent == profile_store:
        raise OrchestrationError(
            "capability_unenforced",
            f"refusing to use a real Hermes profile store as a sandbox root: {resolved}",
        )
    if resolved.exists() and resolved.is_symlink():
        raise OrchestrationError(
            "capability_unenforced", f"sandbox root must not be a symlink: {resolved}"
        )


def sandbox_config_yaml(
    spec: SandboxSpec,
    *,
    base_url: str,
    model: str,
    provider: str = "custom",
    api_mode: str = "",
    api_max_retries: int = 1,
) -> str:
    """Exact reviewer configuration (no host home, no socket, no credential env)."""

    volumes = (
        f'    - "{Path(spec.candidate).absolute()}:{CANDIDATE_MOUNT}:ro"\n'
        f'    - "{Path(spec.evidence).absolute()}:{EVIDENCE_MOUNT}:ro"\n'
    )
    if spec.probe is not None:
        volumes += f'    - "{Path(spec.probe).absolute()}:{PROBE_MOUNT}:ro"\n'
    uid = f"{spec.uid}:{spec.gid}"
    api_mode_line = f"  api_mode: {api_mode}\n" if api_mode else ""
    return (
        "model:\n"
        f"  provider: {provider}\n"
        f"  base_url: {base_url}\n"
        f"  default: {model}\n"
        f"{api_mode_line}"
        "  context_length: 128000\n"
        "agent:\n"
        f"  api_max_retries: {api_max_retries}\n"
        '  coding_context: "off"\n'
        "telemetry:\n"
        "  shared_metrics:\n"
        "    enabled: false\n"
        "    send: false\n"
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /evidence\n"
        "  timeout: 120\n"
        "  container_persistent: false\n"
        "  docker_persist_across_processes: false\n"
        f'  docker_image: "{spec.image}"\n'
        "  docker_network: false\n"
        "  docker_mount_cwd_to_workspace: false\n"
        "  docker_forward_env: []\n"
        "  docker_env:\n"
        "    HOME: /home/reviewer\n"
        "    XDG_CACHE_HOME: /home/reviewer/.cache\n"
        "  docker_volumes:\n"
        f"{volumes}"
        "  docker_extra_args:\n"
        '    - "--read-only"\n'
        '    - "--user"\n'
        f'    - "{uid}"\n'
        '    - "--tmpfs"\n'
        f'    - "/home/reviewer:rw,exec,nosuid,nodev,uid={spec.uid},gid={spec.gid},size=256m"\n'
        '    - "--cap-drop"\n'
        '    - "ALL"\n'
    )


def image_digest(image: str) -> str:
    """Resolve an image reference to a local ``repo@sha256:`` digest.

    Container work goes through :mod:`factory.policy` — the sanctioned Docker
    path — and an invalid, failing or unpinnable reference fails closed as
    ``capability_unenforced`` rather than falling back to a mutable tag.
    """

    from factory import policy

    try:
        return policy.container_image_digest(image)
    except policy.PolicyViolation as exc:
        raise OrchestrationError("capability_unenforced", str(exc)) from exc


def prepare_sandbox(
    spec: SandboxSpec,
    *,
    base_url: str,
    model: str,
    api_key: str | None,
    provider: str = "custom",
    api_mode: str = "",
    api_max_retries: int = 1,
    env_values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create or refresh the disposable home. Refuses to touch anything else."""

    _assert_owned_root(Path(spec.root))
    home = spec.home
    home.parent.mkdir(parents=True, exist_ok=True)
    adopted = False
    if home.exists() and not spec.marker.is_file():
        # A factory-owned root may contain a home Hermes bootstrapped before a
        # crash (state.db/.local and no config). Adopt it only inside our own
        # root; never touch a real profile home (guarded above).
        if spec.config_path().exists():
            raise OrchestrationError(
                "capability_unenforced",
                f"{home} has a config but no factory sandbox marker; refusing to reuse it",
            )
        adopted = True
    home.mkdir(parents=True, exist_ok=True)
    spec.marker.write_text(
        json.dumps(
            {"schema": SANDBOX_SCHEMA, "created_by": "factory-orchestration"}, indent=2
        )
        + "\n"
    )
    config = sandbox_config_yaml(
        spec,
        base_url=base_url,
        model=model,
        provider=provider,
        api_mode=api_mode,
        api_max_retries=api_max_retries,
    )
    spec.config_path().write_text(config)
    env_file = home / ".env"
    lines = [f"{key}={value}" for key, value in sorted((env_values or {}).items())]
    if api_key:
        lines.append(f"OPENAI_API_KEY={api_key}")
    if lines:
        env_file.write_text("\n".join(lines) + "\n")
        env_file.chmod(0o600)
    elif env_file.exists():
        env_file.unlink()
    return {
        "home": str(home),
        "config_digest": _sha256_bytes(config.encode()),
        "base_url": base_url,
        "model": model,
        "adopted_existing_home": adopted,
    }


def _run(
    argv: list[str], *, env: dict[str, str], timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
    )


def _load_fake_provider():
    """Use the packaged stub unless an operator explicitly selects a checkout."""

    repo = os.environ.get("FACTORY_HERMES_REPO")
    if repo is None:
        from .loopback_provider import FakeLLMServer, Text, ToolCall

        return FakeLLMServer, Text, ToolCall
    path = Path(repo) / "tests" / "fakes" / "fake_llm_provider.py"
    if not repo or not path.is_file():
        raise OrchestrationError(
            "capability_unenforced",
            f"loopback fake provider not found under {repo}; the executed preflight "
            "cannot run, so the sandbox is not attested",
        )
    # Load the exact selected file, not an unrelated cached `tests` package.
    name = (
        "_factory_hermes_fake_"
        + hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    )
    if name not in sys.modules:
        # The selected checkout's fake imports its own packages; they resolve
        # from the checkout root, exactly as when the checkout runs its tests.
        root = str(Path(repo).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    module = sys.modules[name]
    return module.FakeLLMServer, module.Text, module.ToolCall


def _stream_events(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events


def _tool_results(events: list[dict]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for event in events:
        if event.get("type") == "tool_result" and event.get("tool_call_id"):
            results[str(event["tool_call_id"])] = event
    return results


@dataclass
class SandboxedReadonlyAttestor:
    """Execute the R2-A attestation; refuse on any missing proof."""

    root: Path
    name: str = "factory-reviewer"
    image: str = ""
    executable: str = "hermes"
    timeout_s: int = 600
    preflight_timeout_s: int = 600
    _last_receipt: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        resolved = shutil.which(self.executable)
        if resolved:
            self.executable = resolved

    def _generation_root(self) -> Path:
        """A disposable root for one attestation generation.

        Hermes treats the parent of ``profiles/<name>`` as its data root and
        keeps a gateway session DB there. The generation is reused so the
        dependency bootstrap happens once; a generation that Hermes itself
        rejected (stale WAL generation after an abnormal exit) is marked
        ``.poisoned`` and the next attestation starts a fresh one.
        """

        base = Path(self.root)
        base.mkdir(parents=True, exist_ok=True)
        for candidate in sorted(base.glob("gen-*"), reverse=True):
            if not (candidate / ".poisoned").exists():
                return candidate
        return base / f"gen-{int(time.time())}-{os.getpid()}"

    def spec_for(self, candidate: str, evidence: str) -> SandboxSpec:
        root = getattr(self, "_active_root", None) or self._generation_root()
        probe = Path(root) / "probe"
        probe.mkdir(parents=True, exist_ok=True)
        probe_file = probe / "probe.txt"
        if not probe_file.exists():
            probe_file.write_text("factory review probe\n")
        return SandboxSpec(
            name=self.name,
            root=Path(root),
            candidate=Path(candidate),
            evidence=Path(evidence),
            image=self.image or image_digest("python:3.12-slim"),
            probe=probe,
        )

    # -- public ------------------------------------------------------------
    def verify_terminal(self, spec: SandboxSpec) -> None:
        """Re-assert the terminal posture **and** the attested bindings.

        Called after the model endpoint is rewritten for the real review. A
        changed mount source, image, home or tree digest fails closed; the
        preflight and the review must be the same sandbox over the same bytes.
        """

        attested = getattr(self, "_attested", None)
        if not attested:
            raise OrchestrationError(
                "capability_unenforced",
                "no executed attestation is bound to this attestor",
            )
        observed_paths = {
            "candidate": str(Path(spec.candidate).absolute()),
            "evidence": str(Path(spec.evidence).absolute()),
            "probe": str(Path(spec.probe).absolute()) if spec.probe else "",
            "image": spec.image,
            "home": str(spec.home),
        }
        for name, value in observed_paths.items():
            if value != attested[name]:
                raise OrchestrationError(
                    "capability_unenforced",
                    f"review {name} {value!r} is not the attested {attested[name]!r}",
                )
        for name in ("candidate_digest", "evidence_digest"):
            current = (
                spec.candidate_digest
                if name == "candidate_digest"
                else spec.evidence_digest
            )
            if current != attested[name]:
                raise OrchestrationError(
                    "capability_unenforced",
                    f"{name} changed between attestation and use "
                    f"({attested[name]} -> {current})",
                )
        env = _child_env(spec, api_key=None)
        observed = self._read_effective_config(spec, env)
        if observed != attested["effective"]:
            raise OrchestrationError(
                "capability_unenforced",
                "effective terminal configuration changed between attestation and use",
            )

    def attest(
        self, *, reviewer: str, candidate: str, evidence: str
    ) -> AttestationReceipt:
        started = time.time()
        try:
            spec = self.spec_for(candidate, evidence)
            self._active_root = Path(spec.root)
            self._validate_paths(spec)
            receipt = self._execute(spec, reviewer)
        except OrchestrationError as exc:
            self._poison_generation(exc.detail)
            return AttestationReceipt(
                enforced=False,
                reason=exc.reason or "capability_unenforced",
                detail=exc.detail,
                receipt={"attempted_at": int(started)},
            )
        except Exception as exc:  # pragma: no cover - defensive
            return AttestationReceipt(
                enforced=False,
                reason="capability_unenforced",
                detail=f"attestation raised {type(exc).__name__}: {exc}"[:500],
                receipt={"attempted_at": int(started)},
            )
        self._last_receipt = receipt
        self._attested = {
            "candidate": str(Path(spec.candidate).absolute()),
            "evidence": str(Path(spec.evidence).absolute()),
            "probe": str(Path(spec.probe).absolute()) if spec.probe else "",
            "candidate_digest": receipt["sandbox"]["candidate_digest"],
            "evidence_digest": receipt["sandbox"]["evidence_digest"],
            "image": receipt["sandbox"]["image"],
            "home": receipt["sandbox"]["home"],
            "uid": receipt["sandbox"]["uid"],
            "effective": receipt["findings"]["config"],
            "attested_at": receipt["attested_at"],
        }
        return AttestationReceipt(
            enforced=True,
            reason="readonly_sandbox_attested",
            detail="read-only sandbox attested by config, executed preflight and "
            "container inspection",
            receipt=receipt,
            home=str(spec.home),
        )

    # -- validation --------------------------------------------------------
    def _poison_generation(self, detail: str) -> None:
        """Retire a generation Hermes refused (stale WAL) so the next run rotates."""

        if "DeletedWalGeneration" not in detail and "did not open" not in detail:
            return
        root = getattr(self, "_active_root", None)
        if root is None:
            return
        try:
            (Path(root) / ".poisoned").write_text("stale hermes session storage\n")
        except OSError:  # pragma: no cover - defensive
            pass

    def _validate_paths(self, spec: SandboxSpec) -> None:
        for label, path in (("candidate", spec.candidate), ("evidence", spec.evidence)):
            if not Path(path).is_dir():
                raise OrchestrationError(
                    "capability_unenforced", f"{label} path is not a directory: {path}"
                )
            if Path(path).is_symlink():
                raise OrchestrationError(
                    "capability_unenforced",
                    f"{label} path must not be a symlink: {path}",
                )
        if Path(spec.candidate).resolve() == Path(spec.evidence).resolve():
            raise OrchestrationError(
                "capability_unenforced",
                "candidate and evidence must be different trees",
            )
        _assert_owned_root(Path(spec.root))

    # -- execution ---------------------------------------------------------
    def _execute(self, spec: SandboxSpec, reviewer: str) -> dict:
        FakeLLMServer, Text, ToolCall = _load_fake_provider()
        self._cleanup_containers(spec)
        candidate_before = spec.candidate_digest
        evidence_before = spec.evidence_digest
        candidate_file = _pick_probe_file(
            Path(spec.candidate), ("README.md", "AGENTS.md", "pyproject.toml")
        )
        evidence_file = _pick_probe_file(
            Path(spec.evidence),
            ("evidence-manifest.json", "manifest.json", "pytest.txt"),
        )
        if candidate_file is None or evidence_file is None:
            raise OrchestrationError(
                "capability_unenforced",
                "preflight needs at least one readable file in the candidate and evidence trees",
            )
        host_canary = Path(spec.root) / "host-canary" / f"marker-{os.getpid()}"
        host_canary.parent.mkdir(parents=True, exist_ok=True)
        marker = f"host-canary-{os.getpid()}-{int(time.time())}"
        host_canary.write_text(marker)
        escape_link = Path(spec.candidate) / ".escape-link"
        try:
            if escape_link.exists() or escape_link.is_symlink():
                escape_link.unlink()
            escape_link.symlink_to(host_canary)

            allowed_script = [
                ToolCall("read_file", {"path": f"{CANDIDATE_MOUNT}/{candidate_file}"}),
                ToolCall("read_file", {"path": f"{EVIDENCE_MOUNT}/{evidence_file}"}),
                ToolCall(
                    "write_file",
                    {"path": f"{PROBE_MOUNT}/probe.txt", "content": marker},
                ),
                ToolCall(
                    "patch",
                    {
                        "path": f"{PROBE_MOUNT}/probe.txt",
                        "old_string": "factory review probe",
                        "new_string": "tampered",
                    },
                ),
                ToolCall(
                    "write_file",
                    {"path": f"{CANDIDATE_MOUNT}/.readonly-canary", "content": marker},
                ),
                ToolCall("read_file", {"path": str(host_canary)}),
                ToolCall(
                    "read_file",
                    {"path": str(Path.home() / ".hermes" / "auth.json")},
                ),
                ToolCall("read_file", {"path": f"{CANDIDATE_MOUNT}/.escape-link"}),
                ToolCall(
                    "search_files", {"pattern": "auth.json", "path": "/root/.hermes"}
                ),
                Text('{"outcome":"approve","comment":"preflight complete"}'),
            ]
            config = None
            preflight = self._phase(
                spec,
                allowed_script,
                Path(spec.root) / "preflight-stream.jsonl",
                "Run the preflight sequence and finish.",
                check_config=True,
            )
            config = preflight.pop("config")
            prepared = preflight.pop("prepared")
            container = self._inspect_and_probe(spec)
            refusal: dict[str, dict] = {}
            requests = list(preflight.pop("requests"))
            for name, args in REJECTION_PROBES.items():
                phase = self._phase(
                    spec,
                    [ToolCall(name, dict(args))],
                    Path(spec.root) / f"rejection-{name}.jsonl",
                    f"Attempt the {name} tool once and finish.",
                )
                phase.pop("config")
                requests.extend(phase.pop("requests"))
                events = phase.pop("events")
                texts = [
                    str(event.get("text") or "") + " " + str(event.get("error") or "")
                    for event in events
                    if event.get("type") == "result"
                ]
                executed = [
                    str(event.get("name"))
                    for event in events
                    if event.get("type") == "tool_use"
                ]
                text = " ".join(texts).lower()
                served = phase["script_unconsumed"] == 0 and phase["script_size"] >= 1
                refusal[name] = {
                    "exit_code": phase["exit_code"],
                    "served_to_hermes": served,
                    "invalid_tool_call_error": "invalid tool call" in text,
                    "executed": name in executed,
                    "refused": served and name not in executed,
                    "transcript_sha256": phase["transcript_sha256"],
                }
        finally:
            try:
                if escape_link.is_symlink() or escape_link.exists():
                    escape_link.unlink()
            except OSError:  # pragma: no cover - defensive
                pass
            host_canary.unlink(missing_ok=True)
            self._cleanup_containers(spec)

        findings = {
            "config": config,
            "preflight": preflight,
            "container": container,
            "refusal": refusal,
            "requests": requests,
            "probe_files": {
                "candidate": candidate_file,
                "evidence": evidence_file,
            },
            "unchanged": {
                "candidate_before": candidate_before,
                "candidate_after": spec.candidate_digest,
                "evidence_before": evidence_before,
                "evidence_after": spec.evidence_digest,
                "probe": (Path(spec.probe) / "probe.txt").read_text()
                if spec.probe
                else "",
            },
        }
        failures = _collect_failures(findings)
        if (
            findings["unchanged"]["candidate_before"]
            != findings["unchanged"]["candidate_after"]
        ):
            failures.append("the candidate tree changed during the preflight")
        if (
            findings["unchanged"]["evidence_before"]
            != findings["unchanged"]["evidence_after"]
        ):
            failures.append("the evidence tree changed during the preflight")
        if findings["unchanged"]["probe"] != "factory review probe\n":
            failures.append("the read-only probe mount was modified")
        if failures:
            raise OrchestrationError(
                "capability_unenforced",
                "sandbox attestation failed: " + "; ".join(failures[:6]),
            )
        return {
            "schema": SANDBOX_SCHEMA,
            "attested_at": int(time.time()),
            "reviewer": reviewer,
            "sandbox": {
                "name": spec.name,
                "home": str(spec.home),
                "image": spec.image,
                "uid": spec.uid,
                "config_digest": prepared["config_digest"],
                "candidate_digest": spec.candidate_digest,
                "evidence_digest": spec.evidence_digest,
                "candidate": str(spec.candidate),
                "evidence": str(spec.evidence),
            },
            "findings": findings,
            "residuals": list(RESIDUALS),
        }

    def _cleanup_containers(self, spec: SandboxSpec) -> None:
        from factory import policy

        try:
            ids = policy.find_containers(
                {"hermes-agent": "1", "hermes-profile": spec.name}
            )
        except policy.PolicyViolation:
            return
        for container_id in ids:
            try:
                policy.remove_own_container(container_id, expected_profile=spec.name)
            except policy.PolicyViolation:  # pragma: no cover - defensive
                continue

    def _read_effective_config(self, spec: SandboxSpec, env: dict[str, str]) -> dict:
        expected = {
            "terminal.backend": "docker",
            "terminal.container_persistent": False,
            "terminal.docker_network": False,
            "terminal.docker_forward_env": [],
            "terminal.docker_mount_cwd_to_workspace": False,
        }
        observed: dict[str, Any] = {}
        for key, want in expected.items():
            result = _run(
                [self.executable, "config", "get", key, "--json"],
                env=env,
                # A fresh disposable home bootstraps its runtime on first use
                # (uv builds the isolated environment). The timeout is not the
                # security boundary; every measured value still has to pass.
                timeout=900,
            )
            if result.returncode != 0:
                raise OrchestrationError(
                    "capability_unenforced",
                    f"config get {key} failed: {result.stderr.strip()[:200]}",
                )
            try:
                value = json.loads(result.stdout)
            except ValueError:
                raise OrchestrationError(
                    "capability_unenforced", f"config get {key} did not return JSON"
                ) from None
            observed[key] = value
            if value != want:
                raise OrchestrationError(
                    "capability_unenforced",
                    f"effective config {key}={value!r}, expected {want!r}",
                )
        volumes = _config_json(self.executable, "terminal.docker_volumes", env)
        extra = _config_json(self.executable, "terminal.docker_extra_args", env)
        image = _config_json(self.executable, "terminal.docker_image", env)
        observed_mounts = volume_map(volumes)
        expected_mounts = expected_volumes(spec)
        if observed_mounts != expected_mounts:
            raise OrchestrationError(
                "capability_unenforced",
                "configured docker_volumes do not match the code-owned mounts: "
                f"observed {sorted(observed_mounts.items())!r}, "
                f"expected {sorted(expected_mounts.items())!r}",
            )
        if image != spec.image:
            raise OrchestrationError(
                "capability_unenforced", f"effective image {image!r} is not the pin"
            )
        extra_text = [str(item) for item in (extra or [])]
        for required in ("--read-only", "--user"):
            if required not in extra_text:
                raise OrchestrationError(
                    "capability_unenforced",
                    f"terminal.docker_extra_args lacks {required!r}",
                )
        return {
            "values": observed,
            "volumes": volumes,
            "extra_args": extra,
            "image": image,
        }

    def _phase(
        self,
        spec: SandboxSpec,
        script: list,
        transcript: Path,
        prompt: str,
        *,
        check_config: bool = False,
    ) -> dict:
        """Run one scripted provider phase against the sandboxed profile."""

        FakeLLMServer, _Text, _ToolCall = _load_fake_provider()
        with FakeLLMServer(script, default_text="preflight finished") as server:
            prepared = prepare_sandbox(
                spec,
                base_url=server.base_url,
                model="fake-model",
                api_key=PREFLIGHT_KEY,
                api_max_retries=0,
            )
            env = _child_env(spec, api_key=PREFLIGHT_KEY)
            config = self._read_effective_config(spec, env) if check_config else None
            exit_code = self._run_chat(spec, env, transcript, prompt)
            unconsumed = server.drop_unconsumed()
            requests = [
                {
                    "kind": request["kind"],
                    "tool_names": sorted(
                        tool["function"]["name"]
                        for tool in ((request.get("body") or {}).get("tools") or [])
                    ),
                }
                for request in server.requests
            ]
        events = _stream_events(transcript)
        tool_uses = [event for event in events if event.get("type") == "tool_use"]
        results = _tool_results(events)
        final_text = ""
        session_id = ""
        reported = exit_code
        for event in events:
            if event.get("type") == "result":
                final_text = str(event.get("text") or "")
                session_id = str(event.get("session_id") or "")
                reported = int(event.get("exit_code") or 0)
        return {
            "exit_code": reported,
            "session_id": session_id,
            "config": config,
            "prepared": prepared,
            "script_size": len(script),
            "script_unconsumed": unconsumed,
            "requests": requests,
            "events": events,
            "transcript_sha256": _sha256_bytes(transcript.read_bytes()),
            "observed_surface": sorted(
                {
                    str(event.get("name"))
                    for event in events
                    if event.get("type") == "tool_use"
                }
            ),
            "tool_calls": _summarise_calls(tool_uses, results),
            "wrote_candidate_canary": (
                Path(spec.candidate) / ".readonly-canary"
            ).exists(),
            "read_escape_link": _result_for(
                tool_uses, results, f"{CANDIDATE_MOUNT}/.escape-link"
            ),
            "read_host_canary": _result_matching(tool_uses, results, "host-canary"),
            "final_text_chars": len(final_text),
        }

    def _run_chat(
        self, spec: SandboxSpec, env: dict[str, str], transcript: Path, prompt: str
    ) -> int:
        workspace = Path(spec.root) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        argv = [
            self.executable,
            "chat",
            "-q",
            prompt,
            "--oneshot",
            "--format",
            "stream-json",
            "--max-turns",
            "20",
            "-t",
            "file",
            "--ignore-rules",
            "--in",
            str(workspace),
        ]
        with open(transcript, "wb") as log:
            result = subprocess.run(
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(workspace),
                env=env,
                timeout=self.preflight_timeout_s,
                check=False,
            )
        return result.returncode

    def _inspect_and_probe(self, spec: SandboxSpec) -> dict:
        from factory import policy

        ids = policy.find_containers({"hermes-agent": "1", "hermes-profile": spec.name})
        if len(ids) != 1:
            raise OrchestrationError(
                "capability_unenforced",
                f"expected exactly one reviewer container, found {len(ids)}",
            )
        container_id = ids[0]
        info = policy.inspect_container(container_id)
        host = info.get("HostConfig") or {}
        config = info.get("Config") or {}
        mounts = info.get("Mounts") or []
        probes = {
            name: policy.exec_container_probe(
                container_id, name, expected_profile=spec.name, timeout=60
            )
            for name in (
                "identity",
                "capabilities",
                "routes",
                "docker_socket",
                "host_secret",
                "evidence_write",
                "ephemeral_write",
                "egress",
            )
        }
        return {
            "id": container_id,
            "network_mode": host.get("NetworkMode"),
            "readonly_rootfs": bool(host.get("ReadonlyRootfs")),
            "user": config.get("User"),
            "cap_add": host.get("CapAdd") or [],
            "cap_drop": host.get("CapDrop") or [],
            "privileged": bool(host.get("Privileged")),
            "pid_mode": host.get("PidMode"),
            "mounts": [
                {
                    "source": mount.get("Source"),
                    "destination": mount.get("Destination"),
                    "rw": bool(mount.get("RW")),
                    "type": mount.get("Type"),
                }
                for mount in mounts
            ],
            "probes": probes,
            "sandbox_home": str(spec.home),
            "candidate_mount": CANDIDATE_MOUNT,
            "evidence_mount": EVIDENCE_MOUNT,
            "expected_mounts": {
                destination: source
                for destination, (source, _mode) in expected_volumes(spec).items()
            },
        }


def _child_env(spec: SandboxSpec, *, api_key: str | None) -> dict[str, str]:
    from .hermes import child_environment

    env = child_environment(
        home=spec.home,
        tmpdir=Path(spec.root) / "tmp",
        executable=shutil.which("hermes") or "hermes",
    )
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    if api_key:
        env["OPENAI_API_KEY"] = api_key
    # Default-deny: HOME is the disposable sandbox home, never the operator's.
    return env


def _pick_probe_file(root: Path, names: tuple[str, ...]) -> str | None:
    """Return the relative path of the first readable probe file under *root*."""

    for name in names:
        candidate = root / name
        if candidate.is_file() and not candidate.is_symlink():
            return name
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 200_000:
            return str(path.relative_to(root))
    return None


def expected_volumes(spec: SandboxSpec) -> dict[str, tuple[str, str]]:
    """The exact, code-owned mount map for one sandbox specification."""

    expected = {
        CANDIDATE_MOUNT: (str(Path(spec.candidate).absolute()), "ro"),
        EVIDENCE_MOUNT: (str(Path(spec.evidence).absolute()), "ro"),
    }
    if spec.probe is not None:
        expected[PROBE_MOUNT] = (str(Path(spec.probe).absolute()), "ro")
    return expected


def volume_map(volumes: Any) -> dict[str, tuple[str, str]]:
    """Parse ``source:destination:mode`` strings; refuse anything ambiguous."""

    if not isinstance(volumes, list):
        raise OrchestrationError(
            "capability_unenforced", "configured docker_volumes are missing"
        )
    mapping: dict[str, tuple[str, str]] = {}
    for entry in volumes:
        text = str(entry)
        parts = text.rsplit(":", 2)
        if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
            raise OrchestrationError(
                "capability_unenforced", f"malformed docker volume: {text}"
            )
        source, destination, mode = parts
        if not destination.startswith("/") or mode not in {"ro", "rw"}:
            raise OrchestrationError(
                "capability_unenforced", f"malformed docker volume: {text}"
            )
        if destination in mapping:
            raise OrchestrationError(
                "capability_unenforced", f"duplicate docker volume for {destination}"
            )
        mapping[destination] = (source, mode)
    return mapping


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _config_json(executable: str, key: str, env: dict[str, str]) -> Any:
    result = _run([executable, "config", "get", key, "--json"], env=env, timeout=900)
    if result.returncode != 0:
        raise OrchestrationError(
            "capability_unenforced",
            f"config get {key} failed: {result.stderr.strip()[:200]}",
        )
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise OrchestrationError(
            "capability_unenforced", f"config get {key} did not return JSON"
        ) from None


def _summarise_calls(tool_uses: list[dict], results: dict[str, dict]) -> list[dict]:
    summary = []
    for event in tool_uses:
        call_id = str(event.get("tool_call_id") or "")
        result = results.get(call_id) or {}
        output = str(result.get("output") or "")
        summary.append(
            {
                "name": str(event.get("name") or ""),
                "input": event.get("input") or {},
                "is_error": bool(result.get("is_error")),
                "output_head": output[:300],
            }
        )
    return summary


def _result_for(tool_uses: list[dict], results: dict[str, dict], path: str) -> dict:
    for event in tool_uses:
        payload = event.get("input") or {}
        if isinstance(payload, dict) and payload.get("path") == path:
            return results.get(str(event.get("tool_call_id") or "")) or {}
    return {}


def _result_matching(
    tool_uses: list[dict], results: dict[str, dict], needle: str
) -> dict:
    for event in tool_uses:
        payload = event.get("input") or {}
        if not isinstance(payload, dict):
            continue
        path = payload.get("path")
        if isinstance(path, str) and needle in path:
            return results.get(str(event.get("tool_call_id") or "")) or {}
    return {}


def _collect_failures(findings: dict) -> list[str]:
    failures: list[str] = []
    preflight = findings["preflight"]
    container = findings["container"]

    main_requests = [item for item in findings["requests"] if item["kind"] == "main"]
    surface = (
        frozenset(main_requests[-1]["tool_names"]) if main_requests else frozenset()
    )
    expected = frozenset({"read_file", "search_files", "write_file", "patch"})
    if surface != expected:
        failures.append(f"resolved surface {sorted(surface)} != {sorted(expected)}")
    if preflight["exit_code"] != 0:
        failures.append(f"preflight exited {preflight['exit_code']}")
    if preflight["wrote_candidate_canary"]:
        failures.append("write_file created a file on the read-only candidate mount")

    calls = preflight["tool_calls"]
    by_name: dict[str, list[dict]] = {}
    for call in calls:
        by_name.setdefault(call["name"], []).append(call)

    for call in by_name.get("write_file", []) + by_name.get("patch", []):
        if "Read-only file system" not in call["output_head"]:
            failures.append(
                f"{call['name']} was not refused by the read-only mount: "
                f"{call['output_head'][:120]}"
            )
    for name in EXPECTED_TOOL_NAMES - {"read_file", "search_files"}:
        if name in by_name:
            continue
        failures.append(f"no {name} attempt was recorded in the preflight")

    for label, result in (
        ("host canary", preflight["read_host_canary"]),
        ("escape symlink", preflight["read_escape_link"]),
    ):
        output = str(result.get("output") or "")
        denied = (
            "not_found" in output
            or "File not found" in output
            or "not a regular file" in output
            or "No such file" in output
        )
        if not denied:
            failures.append(f"{label} was readable inside the reviewer")

    for name, outcome in findings["refusal"].items():
        if not outcome.get("served_to_hermes"):
            failures.append(f"{name} probe never reached Hermes (script unconsumed)")
        if outcome.get("executed"):
            failures.append(f"{name} executed inside the reviewer")

    if container["network_mode"] != "none":
        failures.append(f"container network mode is {container['network_mode']!r}")
    if not container["readonly_rootfs"]:
        failures.append("container root filesystem is writable")
    if container["user"] not in {"10001:10001", "10001"}:
        failures.append(f"container uid is {container['user']!r}, expected 10001")
    if container["privileged"]:
        failures.append("container is privileged")
    if container["pid_mode"] == "host":
        failures.append("container shares the host PID namespace")
    expected_mounts = container.get("expected_mounts") or {}
    sandbox_home = str(container["sandbox_home"]).rstrip("/")
    for mount in container["mounts"]:
        if mount["type"] != "bind":
            continue
        source = str(mount["source"] or "")
        destination = str(mount["destination"] or "")
        if destination in expected_mounts:
            if source != expected_mounts[destination]:
                failures.append(
                    f"{destination} is bound to {source}, "
                    f"not the attested {expected_mounts[destination]}"
                )
            if mount["rw"]:
                failures.append(f"{destination} is mounted read-write")
            continue
        if source != sandbox_home and not source.startswith(sandbox_home + os.sep):
            failures.append(f"unexpected host bind mount {source} -> {destination}")
        elif mount["rw"]:
            failures.append(
                f"writable host mount under the sandbox home: {destination}"
            )
    if any(
        mount["destination"] == "/var/run/docker.sock" for mount in container["mounts"]
    ):
        failures.append("the docker socket is mounted into the reviewer")

    probes = container["probes"]
    if "uid=10001" not in probes["identity"]["stdout"]:
        failures.append("container identity is not uid 10001")
    caps = probes["capabilities"]["stdout"]
    for line in caps.splitlines():
        if line.startswith("CapEff:") and line.split()[-1] != "0000000000000000":
            failures.append(f"effective capabilities are not empty: {line.strip()}")
        if line.startswith("NoNewPrivs:") and line.split()[-1] != "1":
            failures.append("no-new-privileges is not set")
    if (
        "docker.sock" not in probes["docker_socket"]["stdout"]
        and "no-docker-cli" not in probes["docker_socket"]["stdout"]
    ):
        failures.append("docker socket probe was inconclusive")
    if (
        "/Users" in probes["host_secret"]["stdout"]
        and "No such file" not in probes["host_secret"]["stdout"]
    ):
        failures.append("a host path is visible inside the container")
    if "Read-only file system" not in probes["evidence_write"]["stdout"]:
        failures.append("in-container write to /evidence was not refused as read-only")
    if (
        "tmp-ok" not in probes["ephemeral_write"]["stdout"]
        or "home-ok" not in probes["ephemeral_write"]["stdout"]
    ):
        failures.append("the ephemeral runtime is not writable (reviewer cannot run)")
    egress = probes["egress"]["stdout"]
    if "CONNECTED" in egress:
        failures.append("container egress reached the network")
    if "DENIED" not in egress:
        failures.append("egress probe was inconclusive")
    return failures
