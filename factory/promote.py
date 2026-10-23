"""Fail-closed local promotion: consume live verification, advance one ref.

``factory-promote`` is the only sanctioned way to move a local branch from a
verified base to a verified candidate. It is *not* a second verifier: it calls
:func:`factory.evidence.verify_evidence` in process, immediately before acting,
so there is no receipt to replay, reuse or forge. There are deliberately no
``--force``, ``--skip-gate``, ``--allow-legacy`` or ``--push`` flags.

The command:

* accepts only a full ``refs/heads/<name>`` target that currently exists and is
  not symbolic, and only full 40-hex candidate/base commits;
* refuses a dirty promoting worktree and a target checked out in **any**
  worktree (Git's own ``update-ref`` would silently leave that tree stale);
* refuses unless the target is exactly the expected base, the candidate is a
  real descendant and the tip of some branch;
* updates only that one ref with a compare-and-swap
  (``update-ref --no-deref <ref> <new> <expected-old>``), then reads it back;
* mutates no index, worktree, config, remote or tag.

Failure means zero ref/index/worktree/config change. The only intentional
temporary side effect is the scoped coordination lock; an *existing* lock always
fails closed (it is never stolen or aged out) and only a lock this invocation
created is removed.

Trust boundary (stated, not hidden): SHA-256 checks and the compare-and-swap
cannot stop a same-UID actor from rewriting the artifacts directory between the
final hash read and the ref update, and raw ``git`` run outside this command is
not intercepted. Clean start/end snapshots do not bind ignored files, container
images or the inherited environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from factory.evidence import (
    FULL_SHA_RE,
    VerificationResult,
    verify_evidence,
)
from factory.policy import PolicyViolation, assert_safe_repo_root

TARGET_PREFIX = "refs/heads/"
LOCK_FILENAME = "factory-promote.lock"

#: Command-scoped Git hardening only: neutralizes repository hooks and
#: fsmonitor for every Git call this command makes, and keeps read-only probes
#: from refreshing the index. No persistent configuration is ever written.
GIT_SAFETY_ARGUMENTS = (
    "--no-optional-locks",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
)

#: Closed reason-code set. Evidence rejections are reported as
#: ``evidence_rejected:<verifier reason>`` so the promotion verdict never hides
#: which verifier check failed.
REASON_CODES = frozenset(
    {
        "ok",
        # inputs
        "invalid_argument",
        "repo_invalid",
        "repo_unsafe",
        # target ref
        "target_missing",
        "target_symbolic",
        "target_moved",
        # worktree state
        "target_checked_out",
        "worktree_dirty",
        # fast-forward preconditions
        "not_a_commit",
        "not_fast_forward",
        "candidate_not_a_branch_tip",
        # coordination
        "lock_busy",
        # evidence
        "evidence_rejected",
        # ref update
        "ref_update_failed",
        "post_update_mismatch",
    }
)


@dataclass
class PromotionResult:
    promoted: bool = False
    dry_run: bool = False
    reason: str = "ok"
    detail: str = ""
    target: str = ""
    before: str = ""
    after: str = ""
    candidate: str = ""
    base: str = ""
    verification: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "promoted": self.promoted,
            "dry_run": self.dry_run,
            "reason": self.reason,
            "detail": self.detail,
            "target": self.target,
            "before": self.before,
            "after": self.after,
            "candidate": self.candidate,
            "base": self.base,
            "verification": dict(self.verification),
        }


# ---------------------------------------------------------------------------
# git plumbing (command-scoped hardening; never persistent config)
# ---------------------------------------------------------------------------


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *GIT_SAFETY_ARGUMENTS, *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - host without git
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired:  # pragma: no cover - pathological
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="git timed out")


def _out(repo: Path, args: list[str]) -> tuple[int, str, str]:
    proc = _git(repo, args)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _resolve_repo(repo_path: str | Path) -> tuple[Path, str, str]:
    """Return ``(resolved_repo, reason, detail)``; reason ``""`` means usable."""
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        return repo, "repo_invalid", f"repository path does not exist: {repo}"
    code, toplevel, err = _out(repo, ["rev-parse", "--show-toplevel"])
    if code != 0 or not toplevel:
        return repo, "repo_invalid", f"not a git worktree: {err or 'no toplevel'}"
    if Path(toplevel).resolve() != repo:
        return (
            repo,
            "repo_invalid",
            f"--repo must be a worktree toplevel; {repo} is inside {toplevel}",
        )
    try:
        assert_safe_repo_root(repo)
    except PolicyViolation as exc:
        return repo, "repo_unsafe", str(exc)
    return repo, "", ""


def _check_target(repo: Path, target: str) -> tuple[str, str, str]:
    """Validate target shape and existence via Git; return ``(sha, reason, detail)``."""
    if not target.startswith(TARGET_PREFIX) or target == TARGET_PREFIX:
        return "", "invalid_argument", "--target must be a full refs/heads/<name> ref"
    code, _, err = _out(repo, ["check-ref-format", target])
    if code != 0:
        return "", "invalid_argument", f"--target is not a valid ref: {err or target}"
    code, _, _ = _out(repo, ["symbolic-ref", "-q", target])
    if code == 0:
        return "", "target_symbolic", f"{target} is a symbolic ref"
    code, sha, err = _out(repo, ["rev-parse", "--verify", "--quiet", target])
    if code != 0 or not FULL_SHA_RE.match(sha):
        return "", "target_missing", f"{target} does not exist: {err or 'not found'}"
    return sha, "", ""


def _worktree_occupants(repo: Path, target: str) -> list[str]:
    code, text, _ = _out(repo, ["worktree", "list", "--porcelain"])
    if code != 0:
        return []
    occupants: list[str] = []
    current = ""
    for line in text.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :]
        elif line == f"branch {target}" and current:
            occupants.append(current)
    return occupants


def _check_worktrees(repo: Path, target: str) -> tuple[str, str]:
    occupants = _worktree_occupants(repo, target)
    if occupants:
        return (
            "target_checked_out",
            f"{target} is checked out in: {', '.join(occupants)}; detach or remove "
            "that checkout, or promote from a worktree that does not hold it",
        )
    # Porcelain output is column-sensitive: the first two columns are the
    # status flags and may be blank, so never strip the leading whitespace.
    proc = _git(repo, ["status", "--porcelain"])
    if proc.returncode != 0:
        return "repo_invalid", f"git status failed: {(proc.stderr or '').strip()}"
    entries = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if entries:
        paths = ", ".join(entry[3:] for entry in entries[:5])
        return "worktree_dirty", f"promoting worktree is dirty: {paths}"
    return "", ""


def _branch_tips(repo: Path) -> set[str]:
    code, text, _ = _out(
        repo, ["for-each-ref", "--format=%(objectname)", TARGET_PREFIX]
    )
    if code != 0:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _update_ref(
    repo: Path, target: str, candidate: str, expected_old: str
) -> subprocess.CompletedProcess:
    return _git(
        repo,
        ["update-ref", "--no-deref", target, candidate, expected_old],
    )


def _common_dir(repo: Path) -> tuple[Path, str, str]:
    code, text, err = _out(repo, ["rev-parse", "--git-common-dir"])
    if code != 0 or not text:
        return repo, "repo_invalid", f"no git common directory: {err or 'missing'}"
    common = Path(text)
    if not common.is_absolute():
        common = repo / common
    return common.resolve(), "", ""


class _CoordinationLock:
    """One scoped lock under the common Git dir.

    ``O_CREAT|O_EXCL`` means a concurrent promotion loses. An existing lock
    always fails closed: it is never removed, aged out or taken over, not even
    when its contents look stale. Only the inode this invocation created is
    removed on release, so a lock that appeared in the meantime survives.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._inode: int | None = None

    def acquire(self) -> tuple[bool, str]:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False, (
                f"another promotion holds {self.path} ({self._describe()}); an "
                "existing lock is never stolen by age -- remove it manually only "
                "after confirming no promotion is running"
            )
        except OSError as exc:  # pragma: no cover - unwritable common dir
            return False, f"cannot create {self.path}: {exc}"
        self._fd = fd
        self._inode = os.fstat(fd).st_ino
        payload = f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        os.write(fd, payload.encode())
        return True, ""

    def _describe(self) -> str:
        try:
            return self.path.read_text(errors="replace").strip()[:200] or "empty"
        except OSError:
            return "unreadable"

    def release(self) -> None:
        fd, inode = self._fd, self._inode
        self._fd = self._inode = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - defensive
                pass
        if inode is None:
            return
        try:
            if os.stat(self.path).st_ino == inode:
                os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - defensive
            pass


# ---------------------------------------------------------------------------
# promotion
# ---------------------------------------------------------------------------


def promote_ref(
    repo_path: str | Path,
    artifacts_dir: str | Path,
    candidate: str,
    base: str,
    target: str,
    dry_run: bool = False,
) -> PromotionResult:
    """Fast-forward ``target`` to ``candidate`` only if fresh evidence certifies it."""
    result = PromotionResult(
        dry_run=dry_run, target=target, candidate=candidate, base=base
    )

    def reject(reason: str, detail: str) -> PromotionResult:
        result.promoted = False
        result.reason = reason
        result.detail = detail
        return result

    if not FULL_SHA_RE.match(candidate or "") or not FULL_SHA_RE.match(base or ""):
        return reject(
            "invalid_argument", "candidate and base must be full 40-hex commit SHAs"
        )

    repo, reason, detail = _resolve_repo(repo_path)
    if reason:
        return reject(reason, detail)

    target_before, reason, detail = _check_target(repo, target)
    if reason:
        return reject(reason, detail)
    result.before = target_before
    result.after = target_before

    reason, detail = _check_worktrees(repo, target)
    if reason:
        return reject(reason, detail)

    if target_before != base:
        return reject(
            "target_moved",
            f"{target} is at {target_before}; expected base {base}",
        )

    code, object_type, err = _out(repo, ["cat-file", "-t", candidate])
    if code != 0 or object_type != "commit":
        return reject(
            "not_a_commit", f"{candidate} is not a commit: {err or object_type}"
        )
    code, _, err = _out(repo, ["merge-base", "--is-ancestor", base, candidate])
    if code != 0:
        return reject(
            "not_fast_forward", f"{base} is not an ancestor of {candidate}: {err}"
        )
    if candidate not in _branch_tips(repo):
        return reject(
            "candidate_not_a_branch_tip",
            f"{candidate} is not the tip of any {TARGET_PREFIX}* ref",
        )

    common, reason, detail = _common_dir(repo)
    if reason:
        return reject(reason, detail)
    lock = _CoordinationLock(common / LOCK_FILENAME)
    acquired, detail = lock.acquire()
    if not acquired:
        return reject("lock_busy", detail)
    try:
        verification: VerificationResult = verify_evidence(
            artifacts_dir, repo, candidate, base
        )
        result.verification = verification.as_dict()
        if not verification.ok:
            return reject(
                f"evidence_rejected:{verification.reason}",
                verification.detail or "evidence rejected",
            )

        # Narrow the TOCTOU window: occupancy, cleanliness, the target ref and
        # the evidence are all re-observed immediately before the CAS.
        reason, detail = _check_worktrees(repo, target)
        if reason:
            return reject(reason, detail)
        current, reason, detail = _read_target(repo, target)
        if reason:
            return reject(reason, detail)
        if current != target_before:
            return reject(
                "target_moved",
                f"{target} moved from {target_before} to {current} before the update",
            )
        reverified: VerificationResult = verify_evidence(
            artifacts_dir, repo, candidate, base
        )
        result.verification = reverified.as_dict()
        if not reverified.ok:
            return reject(
                f"evidence_rejected:{reverified.reason}",
                reverified.detail or "evidence rejected",
            )

        if dry_run:
            result.reason = "ok"
            result.detail = "dry run: all checks passed; no ref was updated"
            result.after = current
            return result

        proc = _update_ref(repo, target, candidate, current)
        if proc.returncode != 0:
            return reject(
                "ref_update_failed",
                f"update-ref refused {target} -> {candidate} (expected old "
                f"{current}): {(proc.stderr or '').strip()}",
            )

        after, reason, detail = _read_target(repo, target)
        if reason:
            return reject(reason, detail)
        result.after = after
        if after != candidate:
            return reject(
                "post_update_mismatch",
                f"{target} reads {after} after the update, expected {candidate}",
            )
        result.promoted = True
        result.reason = "ok"
        return result
    finally:
        lock.release()


def _read_target(repo: Path, target: str) -> tuple[str, str, str]:
    code, sha, err = _out(repo, ["rev-parse", "--verify", "--quiet", target])
    if code != 0 or not FULL_SHA_RE.match(sha):
        return "", "target_missing", f"{target} no longer resolves: {err or 'missing'}"
    return sha, "", ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

TRUST_BOUNDARY = """\
Trust boundary (stated, not hidden):
  * A same-UID actor who can write the artifacts directory can rewrite evidence
    between the final hash read and the ref update; the compare-and-swap does
    not close that window.
  * Raw git run outside this command (update-ref, push, merge, hand-edited refs)
    is not intercepted. This is one guarded operation, not a repository lock.
  * An existing coordination lock always fails closed; it is never stolen.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-promote",
        description=(
            "Advance one local branch ref from a verified base to a verified "
            "candidate. Fast-forward only, single ref, no push/tag/deploy. "
            "Exit 0 promoted, 1 rejected, 2 usage error."
        ),
        epilog=TRUST_BOUNDARY,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True, help="git worktree toplevel")
    parser.add_argument(
        "--artifacts-dir",
        required=True,
        help="directory holding the schema-v2 manifest, sidecar and logs",
    )
    parser.add_argument(
        "--candidate", required=True, help="full 40-hex candidate commit SHA"
    )
    parser.add_argument("--base", required=True, help="full 40-hex expected base SHA")
    parser.add_argument(
        "--target",
        required=True,
        help="existing branch ref to advance, e.g. refs/heads/main",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="perform every check and the in-process verification, update nothing",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )
    return parser


def render_human(result: PromotionResult) -> str:
    if result.reason == "ok" and result.promoted:
        lines = [
            f"factory-promote: PROMOTED {result.target} {result.before} -> {result.after}"
        ]
    elif result.reason == "ok" and result.dry_run:
        lines = [
            f"factory-promote: DRY RUN ok for {result.target} "
            f"({result.before} -> {result.candidate}); no ref updated"
        ]
    else:
        lines = [
            f"factory-promote: REJECTED {result.reason} for {result.target}",
        ]
        if result.detail:
            lines.append(f"  detail: {result.detail}")
    lines.append(f"  candidate: {result.candidate}")
    lines.append(f"  base: {result.base}")
    verification = result.verification or {}
    if verification:
        lines.append(f"  verification: {verification.get('reason', 'n/a')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for flag, value in (("--candidate", args.candidate), ("--base", args.base)):
        if not FULL_SHA_RE.match(value):
            print(
                f"factory-promote: error: {flag} must be a full 40-hex commit SHA",
                file=sys.stderr,
            )
            return 2

    result = promote_ref(
        args.repo,
        args.artifacts_dir,
        args.candidate,
        args.base,
        args.target,
        dry_run=args.dry_run,
    )
    if result.reason == "invalid_argument":
        print(f"factory-promote: error: {result.detail}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(render_human(result))
    return 0 if result.reason == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
