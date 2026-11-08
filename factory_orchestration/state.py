"""Versioned state schema, assignment validation and typed outcomes.

The schema is data, not executable behaviour: no field admits Python, shell or
graph structure. Identifiers, digests, paths and enums are validated before any
effect, and the version constants are recorded in every checkpoint so a changed
schema/graph fails closed instead of being reinterpreted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from .errors import OrchestrationError

GRAPH_VERSION = "factory-card/v1"
STATE_SCHEMA_VERSION = 1
MAX_REPAIRS_PILOT = 2

_CARD_RE = re.compile(r"^t_[0-9a-f]{4,32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

STATUSES = frozenset(
    {
        "running",
        "awaiting_worker",
        "awaiting_review",
        "held",
        "cancelled",
        "ready_for_authorized_completion",
    }
)

OUTCOMES = frozenset({"completed", "failed", "unavailable", "ambiguous", "cancelled"})


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require(condition: bool, reason: str, detail: str) -> None:
    if not condition:
        raise OrchestrationError(reason, detail)


def _full_sha(value: Any, label: str) -> str:
    _require(
        isinstance(value, str) and bool(_SHA_RE.match(value)),
        "invalid_assignment",
        f"{label} must be a full 40-hex commit SHA",
    )
    return value


@dataclass(frozen=True)
class Attempt:
    """The Hermes attempt this graph executes inside.

    Identity is ``(task_id, run_id)``, both granted by the dispatcher; the claim
    lock is carried for diagnostics only because ``kanban show --json`` does not
    expose it (see ``docs/langgraph-hybrid/g0-capabilities.md``).
    """

    task_id: str
    run_id: int
    profile: str
    claim_lock: str | None = None

    def __post_init__(self) -> None:
        _require(
            isinstance(self.task_id, str) and bool(_CARD_RE.match(self.task_id)),
            "invalid_assignment",
            "attempt task_id must look like a Kanban card id",
        )
        _require(
            isinstance(self.run_id, int)
            and not isinstance(self.run_id, bool)
            and self.run_id > 0,
            "invalid_assignment",
            "attempt run_id must be a positive integer",
        )
        _require(
            isinstance(self.profile, str) and bool(_PROFILE_RE.match(self.profile)),
            "invalid_assignment",
            "attempt profile must be a profile slug",
        )

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Attempt":
        task = env.get("HERMES_KANBAN_TASK", "").strip()
        raw_run = env.get("HERMES_KANBAN_RUN_ID", "").strip()
        profile = env.get("HERMES_PROFILE", "").strip()
        _require(bool(task), "invalid_assignment", "HERMES_KANBAN_TASK is absent")
        _require(bool(raw_run), "invalid_assignment", "HERMES_KANBAN_RUN_ID is absent")
        _require(bool(profile), "invalid_assignment", "HERMES_PROFILE is absent")
        try:
            run_id = int(raw_run)
        except ValueError as exc:  # pragma: no cover - defensive
            raise OrchestrationError(
                "invalid_assignment", "HERMES_KANBAN_RUN_ID is not an integer"
            ) from exc
        return cls(
            task_id=task,
            run_id=run_id,
            profile=profile,
            claim_lock=env.get("HERMES_KANBAN_CLAIM_LOCK") or None,
        )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunAssignment:
    """Everything an admission binds, captured before the first effect."""

    board_id: str
    card_id: str
    repository: str
    workspace_path: str
    immutable_base_sha: str
    spec_reference: str
    spec_digest: str
    template_id: str
    worker_profile: str
    reviewer_profile: str
    acceptance_argv: tuple[str, ...]
    acceptance_artifacts: str
    engine_release_reference: str
    engine_fingerprint: str
    host_policy_digest: str
    graph_version: str = GRAPH_VERSION
    state_schema_version: int = STATE_SCHEMA_VERSION
    max_repairs: int = MAX_REPAIRS_PILOT
    cost_budget: str = "unknown"
    entire_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(
            isinstance(self.board_id, str) and bool(_SLUG_RE.match(self.board_id)),
            "invalid_assignment",
            "board_id must be a board slug",
        )
        _require(
            isinstance(self.card_id, str) and bool(_CARD_RE.match(self.card_id)),
            "invalid_assignment",
            "card_id must look like a Kanban card id",
        )
        for label, value in (
            ("repository", self.repository),
            ("workspace_path", self.workspace_path),
            ("spec_reference", self.spec_reference),
            ("acceptance_artifacts", self.acceptance_artifacts),
        ):
            _require(
                isinstance(value, str) and Path(value).is_absolute(),
                "invalid_assignment",
                f"{label} must be an absolute path",
            )
        _require(
            Path(self.workspace_path) != Path(self.repository),
            "invalid_assignment",
            "workspace_path must differ from the subject repository root",
        )
        _full_sha(self.immutable_base_sha, "immutable_base_sha")
        _require(
            isinstance(self.spec_digest, str)
            and bool(_DIGEST_RE.match(self.spec_digest)),
            "invalid_assignment",
            "spec_digest must be sha256:<64 hex>",
        )
        _require(
            isinstance(self.template_id, str)
            and bool(_SLUG_RE.match(self.template_id)),
            "invalid_assignment",
            "template_id must be a reviewed template slug",
        )
        for label, value in (
            ("worker_profile", self.worker_profile),
            ("reviewer_profile", self.reviewer_profile),
        ):
            _require(
                isinstance(value, str) and bool(_PROFILE_RE.match(value)),
                "invalid_assignment",
                f"{label} must be a profile slug",
            )
        _require(
            self.worker_profile != self.reviewer_profile,
            "invalid_assignment",
            "implementer and independent reviewer must be different profiles",
        )
        _require(
            isinstance(self.acceptance_argv, tuple)
            and bool(self.acceptance_argv)
            and all(isinstance(part, str) and part for part in self.acceptance_argv),
            "invalid_assignment",
            "acceptance_argv must be a non-empty tuple of argv strings",
        )
        _require(
            self.graph_version == GRAPH_VERSION,
            "version_mismatch",
            f"unsupported graph version {self.graph_version!r}",
        )
        _require(
            self.state_schema_version == STATE_SCHEMA_VERSION,
            "version_mismatch",
            f"unsupported state schema {self.state_schema_version!r}",
        )
        _require(
            isinstance(self.max_repairs, int)
            and not isinstance(self.max_repairs, bool)
            and 0 <= self.max_repairs <= 10,
            "invalid_assignment",
            "max_repairs must be an integer in 0..10",
        )
        _require(
            isinstance(self.engine_fingerprint, str)
            and bool(_DIGEST_RE.match(self.engine_fingerprint)),
            "invalid_assignment",
            "engine_fingerprint must be sha256:<64 hex>",
        )
        _require(
            isinstance(self.host_policy_digest, str)
            and bool(_DIGEST_RE.match(self.host_policy_digest)),
            "invalid_assignment",
            "host_policy_digest must be sha256:<64 hex>",
        )

    def binding(self) -> dict:
        """Immutable fields compared on every resume (T11 mismatch detection)."""

        return {
            "board_id": self.board_id,
            "card_id": self.card_id,
            "repository": self.repository,
            "workspace_path": self.workspace_path,
            "immutable_base_sha": self.immutable_base_sha,
            "spec_digest": self.spec_digest,
            "template_id": self.template_id,
            "worker_profile": self.worker_profile,
            "reviewer_profile": self.reviewer_profile,
            "graph_version": self.graph_version,
            "state_schema_version": self.state_schema_version,
            "max_repairs": self.max_repairs,
            "engine_fingerprint": self.engine_fingerprint,
            "host_policy_digest": self.host_policy_digest,
        }

    def as_dict(self) -> dict:
        return asdict(self)


class FactoryRunState(TypedDict, total=False):
    """``FactoryRun v1``. References and bounded summaries only, never secrets."""

    board_id: str
    card_id: str
    run_id: str
    hermes_attempt_id: str
    hermes_assignee: str
    attempt_claim_lock: str
    owner_token_reference: str
    graph_version: str
    state_schema_version: int
    spec_reference: str
    spec_digest: str
    template_id: str
    repository: str
    workspace_id: str
    workspace_path: str
    immutable_base_sha: str
    candidate_sha: str
    acceptance_argv: tuple[str, ...]
    acceptance_artifacts: str
    engine_release_reference: str
    engine_fingerprint: str
    host_policy_digest: str
    stage: str
    stage_sequence: int
    status: str
    worker_profile: str
    worker_session_id: str
    worker_request_id: str
    reviewer_profile: str
    reviewer_request_id: str
    reviewer_result: dict
    repair_count: int
    max_repairs: int
    deadline: str
    cost_budget: str
    operations: dict
    artifact_references: dict
    evidence_reference: str
    evidence_digest: str
    approval_reference: str
    entire_references: dict
    last_reason_code: str
    last_reason_detail: str
    cancellation_requested: bool
    route: str


def initial_state(
    assignment: RunAssignment, attempt: Attempt, *, owner_token: str
) -> FactoryRunState:
    return FactoryRunState(
        board_id=assignment.board_id,
        card_id=assignment.card_id,
        run_id=f"{assignment.board_id}:{assignment.card_id}",
        hermes_attempt_id=str(attempt.run_id),
        hermes_assignee=attempt.profile,
        attempt_claim_lock=attempt.claim_lock or "",
        owner_token_reference=owner_token,
        graph_version=assignment.graph_version,
        state_schema_version=assignment.state_schema_version,
        spec_reference=assignment.spec_reference,
        spec_digest=assignment.spec_digest,
        template_id=assignment.template_id,
        repository=assignment.repository,
        workspace_id=assignment.workspace_path,
        workspace_path=assignment.workspace_path,
        immutable_base_sha=assignment.immutable_base_sha,
        acceptance_argv=assignment.acceptance_argv,
        acceptance_artifacts=assignment.acceptance_artifacts,
        engine_release_reference=assignment.engine_release_reference,
        engine_fingerprint=assignment.engine_fingerprint,
        host_policy_digest=assignment.host_policy_digest,
        stage="admit",
        stage_sequence=0,
        status="running",
        worker_profile=assignment.worker_profile,
        reviewer_profile=assignment.reviewer_profile,
        repair_count=0,
        max_repairs=assignment.max_repairs,
        deadline="",
        cost_budget=assignment.cost_budget,
        operations={},
        artifact_references={},
        evidence_reference="",
        evidence_digest="",
        entire_references={ref: "unresolved" for ref in assignment.entire_references},
        last_reason_code="",
        last_reason_detail="",
        cancellation_requested=False,
        route="continue",
    )


# ---------------------------------------------------------------------------
# Typed adapter outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerRequest:
    operation_key: str
    attempt: Attempt
    profile: str
    spec_digest: str
    workspace: str
    base_sha: str
    allowed_scope: tuple[str, ...]
    repair_index: int
    timeout_s: int
    artifact_dir: str
    prompt: str


@dataclass(frozen=True)
class WorkerReceipt:
    operation_key: str
    attempt: Attempt
    session_id: str
    outcome: str
    candidate_sha: str | None = None
    artifact_references: tuple[str, ...] = ()
    reported_tests: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        _require(
            self.outcome in OUTCOMES,
            "protocol_error",
            f"unknown worker outcome {self.outcome!r}",
        )
        if self.outcome == "completed":
            _require(
                bool(self.candidate_sha),
                "protocol_error",
                "a completed worker must report a candidate commit",
            )
            _full_sha(self.candidate_sha, "candidate_sha")

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["attempt"] = self.attempt.as_dict()
        return payload


@dataclass(frozen=True)
class GateOutcome:
    outcome: str
    reason: str = ""
    detail: str = ""
    evidence_reference: str = ""
    evidence_digest: str = ""

    def __post_init__(self) -> None:
        _require(
            self.outcome in {"pass", "fail", "unavailable", "inconclusive"},
            "protocol_error",
            f"unknown gate outcome {self.outcome!r}",
        )


@dataclass(frozen=True)
class VerifyOutcome:
    certified: bool
    reason: str
    detail: str = ""
    evidence_digest: str = ""


@dataclass(frozen=True)
class ReviewResult:
    operation_key: str
    attempt: Attempt
    reviewer: str
    outcome: str
    candidate_sha: str
    base_sha: str
    spec_digest: str
    evidence_digest: str
    graph_version: str
    comment: str = ""

    def __post_init__(self) -> None:
        _require(
            self.outcome in {"approve", "changes_required", "hold", "inconclusive"},
            "protocol_error",
            f"unknown review outcome {self.outcome!r}",
        )
        _full_sha(self.candidate_sha, "review candidate_sha")
        _full_sha(self.base_sha, "review base_sha")

    def binding(self) -> dict:
        return {
            "candidate_sha": self.candidate_sha,
            "base_sha": self.base_sha,
            "spec_digest": self.spec_digest,
            "evidence_digest": self.evidence_digest,
            "graph_version": self.graph_version,
            "reviewer": self.reviewer,
        }


@dataclass
class AdmissionDefaults:
    """Values filled from the installed release, never from model prose."""

    base_sha: str
    spec_digest: str
    worker_profile: str
    reviewer_profile: str
    acceptance_argv: tuple[str, ...]
    acceptance_artifacts: str
    engine_release_reference: str
    engine_fingerprint: str
    host_policy_digest: str
    template_id: str = "bounded-card-v1"
    entire_references: tuple[str, ...] = field(default_factory=tuple)


def approval_matches(
    approval: dict | None,
    *,
    candidate_sha: str,
    base_sha: str,
    spec_digest: str,
    evidence_digest: str,
    implementer: str,
) -> bool:
    """An approval releases acceptance only for the exact bound candidate.

    Changing the candidate, base, spec or evidence digest invalidates it, and an
    implementer's own approval never releases acceptance.
    """

    if not approval or approval.get("outcome") != "approve":
        return False
    if approval.get("reviewer") == implementer:
        return False
    return (
        approval.get("candidate_sha") == candidate_sha
        and approval.get("base_sha") == base_sha
        and approval.get("spec_digest") == spec_digest
        and approval.get("evidence_digest") == evidence_digest
    )
