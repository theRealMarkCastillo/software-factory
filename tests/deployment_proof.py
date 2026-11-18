"""Operator-invoked live proof; never collected by pytest.

Invoke only on an explicitly authorized Docker host, after committing a clean
candidate. Docker inspection stays behind factory.policy; lifecycle goes through
the installed candidate factory-run entry point.
"""

import argparse
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from factory.policy import ProtectedProjectSnapshot, find_containers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-root", required=True)
    args = parser.parse_args()
    root = Path(args.artifacts_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    records = []
    for label, script, expected in (
        ("success", "check.sh", 0),
        ("negative", "check-fail.sh", 1),
    ):
        before = ProtectedProjectSnapshot.capture()
        command = [
            "uv",
            "run",
            "--locked",
            "factory-run",
            "--deployment",
            "--card",
            f"deployment-proof-{label}",
            "--repo",
            str(repo),
            "--base",
            "HEAD",
            "--compose",
            "examples/deployment-minimal/compose.yml",
            "--check",
            f"examples/deployment-minimal/{script}",
            "--env-example",
            "examples/deployment-minimal/env.example",
            "--timeout",
            "300",
            "--artifacts-root",
            str(root),
        ]
        proc = subprocess.run(
            command, cwd=repo, capture_output=True, text=True, timeout=900, check=False
        )
        (root / f"{label}.stdout.json").write_text(proc.stdout)
        (root / f"{label}.stderr.log").write_text(proc.stderr)
        result = json.loads(proc.stdout)
        after = ProtectedProjectSnapshot.capture()
        remaining = find_containers(
            {"com.docker.compose.project": result["project_name"]}
        )
        collateral = before.collateral_damage_report(after)
        record = {
            "label": label,
            "revision": revision,
            "command": command,
            "returncode": proc.returncode,
            "result": result,
            "before": asdict(before),
            "after": asdict(after),
            "remaining_project_containers": remaining,
            "collateral": collateral,
        }
        records.append(record)
        (root / "live-proof.json").write_text(json.dumps(records, indent=2) + "\n")
        assert not remaining, record
        assert not any(collateral.values()), record
        assert proc.returncode == expected, record
        assert result["passed"] is (expected == 0), record
        assert result["stage"] == (
            "complete" if expected == 0 else "deployment_check_failed"
        ), record
    print(
        json.dumps(
            {
                "revision": revision,
                "proof": str(root / "live-proof.json"),
                "runs": len(records),
                "passed": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
