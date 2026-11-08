#!/usr/bin/env python3
"""Host-side R2-A reviewer sandbox attestation (explicit, outside pytest).

The offline test suite blocks Docker on purpose: a nominally mocked QC run must
never create a container. This script is the sanctioned way to *execute* the
attestation against the real Hermes Docker backend and write the receipts.

Usage::

    python scripts/attest_reviewer_sandbox.py \
        --root .factory/reviewer-sandbox \
        --candidate /path/to/candidate-worktree \
        --evidence /path/to/selected-evidence \
        --out .factory/reviewer-sandbox/attestation.json

It never touches an existing Hermes profile: the reviewer home is created under
``--root`` with a factory marker, and Docker access goes through
``factory.policy``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory_orchestration.adapters.reviewer_sandbox import (  # noqa: E402
    SandboxedReadonlyAttestor,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--image", default="")
    parser.add_argument("--hermes", default="hermes")
    args = parser.parse_args(argv)

    attestor = SandboxedReadonlyAttestor(
        root=args.root, image=args.image, executable=args.hermes
    )
    receipt = attestor.attest(
        reviewer="factory-reviewer",
        candidate=str(args.candidate),
        evidence=str(args.evidence),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt.as_dict(), indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "enforced": receipt.enforced,
                "reason": receipt.reason,
                "detail": receipt.detail,
                "out": str(args.out),
            },
            indent=2,
        )
    )
    return 0 if receipt.enforced else 2


if __name__ == "__main__":
    raise SystemExit(main())
