"""Read-only evidence verification CLI: ``factory-verify``.

Verifies one artifacts directory against an explicit candidate and base commit
and exits ``0`` (certified), ``1`` (rejected), or ``2`` (usage error). All
policy lives in :mod:`factory.evidence`; this module only parses, calls, and
renders. It has no escape hatches (no ``--allow-legacy``, ``--skip-gate``,
``--force``), because any such flag would recreate the bypass this gate closes.

The command is offline and read-only: it runs only local ``git`` reads
(``rev-parse``, ``cat-file``, ``ls-tree``, ``merge-base``, ``diff``) with
command-scoped hardening, writes no receipt, and mutates no file and no ref.
Promotion verifies in-process, so there is nothing to replay or reuse.
"""

from __future__ import annotations

import argparse
import json
import sys

from factory.evidence import FULL_SHA_RE, VerificationResult, verify_evidence
from factory.frozen import offending_paths, validate_paths, violation_detail

TRUST_BOUNDARY = """\
Trust boundary (stated, not hidden):
  * Integrity is not same-UID attestation: an actor who can write the artifacts
    directory can produce a self-consistent manifest, sidecar and logs.
  * Clean start/end snapshots do not bind ignored files, container images,
    dependencies, or the inherited environment.
  * The engine fingerprint binds evidence to the engine build that produced it,
    not to an installed release.
  * Raw Git run outside the sanctioned commands is not intercepted.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-verify",
        description=(
            "Verify a schema-v2 evidence directory against a candidate and base "
            "commit. Read-only and offline; exit 0 certified, 1 rejected, "
            "2 usage error."
        ),
        epilog=TRUST_BOUNDARY,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--artifacts-dir",
        required=True,
        help="directory containing evidence-manifest.json and its sidecar",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="git repository (object database) that must contain --base and --candidate",
    )
    parser.add_argument(
        "--candidate", required=True, help="full 40-hex candidate commit SHA"
    )
    parser.add_argument("--base", required=True, help="full 40-hex base commit SHA")
    parser.add_argument(
        "--frozen",
        action="append",
        default=[],
        metavar="PATH",
        help="literal frozen file or directory (trailing /); repeatable",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )
    return parser


def render_human(result: VerificationResult) -> str:
    if result.ok:
        lines = [
            f"factory-verify: CERTIFIED {result.candidate} (base {result.base})",
        ]
    else:
        lines = [
            f"factory-verify: REJECTED {result.reason} "
            f"for candidate {result.candidate} (base {result.base})"
        ]
        if result.detail:
            lines.append(f"  detail: {result.detail}")
    failed = [check for check in result.checks if not check["ok"]]
    lines.append(f"  checks: {len(result.checks)} run, {len(failed)} failed")
    for check in result.checks:
        if check["ok"]:
            lines.append(f"    {check['name']}: ok")
        else:
            reason = check.get("reason", "")
            lines.append(f"    {check['name']}: FAIL ({reason})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for flag, value in (("--candidate", args.candidate), ("--base", args.base)):
        if not FULL_SHA_RE.match(value):
            print(
                f"factory-verify: error: {flag} must be a full 40-hex commit SHA",
                file=sys.stderr,
            )
            return 2

    paths = tuple(args.frozen)
    try:
        validate_paths(paths)
    except ValueError as exc:
        print(f"factory-verify: error: {exc}", file=sys.stderr)
        return 2
    result = verify_evidence(args.artifacts_dir, args.repo, args.candidate, args.base)
    if paths:
        try:
            violations = offending_paths(args.repo, args.base, args.candidate, paths)
        except (ValueError, OSError) as exc:
            result = VerificationResult(
                False, "invalid_argument", str(exc), args.candidate, args.base
            )
        else:
            if violations:
                result.ok = False
                result.reason = "frozen_path_modified"
                result.detail = violation_detail(violations)
                result.checks.append(
                    {
                        "name": "frozen_paths",
                        "ok": False,
                        "reason": "frozen_path_modified",
                    }
                )
    if result.reason == "invalid_argument":
        print(f"factory-verify: error: {result.detail}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(render_human(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
