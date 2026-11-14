"""Offline wheel-install probe; run with the dev Python and a clean Python path.

Example: python tests/standalone_wheel_probe.py /path/to/clean-env/bin/python
The target environment must contain the wheel + PyYAML, without orchestration.
Fixtures are synthetic verifier evidence, not live Compose acceptance.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def child(payload):
    import importlib.util

    from factory import cli
    from factory_orchestration.adapters.gates import InstalledLauncherGate
    from factory_orchestration.adapters.installed import default_launcher
    from factory_orchestration.adapters.reviewer_sandbox import _load_fake_provider

    assert Path(cli.__file__).is_relative_to(sys.prefix), cli.__file__
    assert importlib.util.find_spec("langgraph") is None
    launcher = default_launcher()
    assert launcher == Path(sys.executable).parent / "factory"
    for command in (
        "run",
        "verify",
        "promote",
        "audit",
        "coverage",
        "datamodel",
        "learning",
    ):
        result = subprocess.run(
            [str(launcher), command, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (command, result.stderr)
    result = subprocess.run(
        [str(launcher), "orchestrate", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "[orchestration]" in result.stderr and "Traceback" not in result.stderr
    outcome = InstalledLauncherGate(launcher, Path(payload["receipts"])).verify(
        **payload["verify"]
    )
    assert outcome.certified, outcome
    Server, Text, ToolCall = _load_fake_provider()
    assert all(
        Path(sys.modules[cls.__module__].__file__).is_relative_to(sys.prefix)
        for cls in (Server, Text, ToolCall)
    )
    with Server([Text("wheel provider")]) as server:
        from urllib.request import Request, urlopen

        request = Request(
            server.base_url + "/chat/completions",
            data=json.dumps({"tools": [{"function": {"name": "read_file"}}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            assert (
                json.load(response)["choices"][0]["message"]["content"]
                == "wheel provider"
            )
        assert server.drop_unconsumed() == 0
    print(
        "PASS: wheel-only core help (7), missing-extra hint, default launcher, "
        "gate fixture verification, packaged loopback provider"
    )


def main():
    if sys.argv[1] == "--child":
        child(json.loads(sys.argv[2]))
        return
    # Only the fixture builder uses the development test dependencies.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.test_verify import _evidence, _repo

    with tempfile.TemporaryDirectory(
        dir=Path(sys.prefix), prefix="wheel-probe-"
    ) as tmp:
        root = Path(tmp)
        repo, base, candidate = _repo(root)
        artifacts = _evidence(root, repo, base, candidate)
        payload = {
            "receipts": str(root / "receipts"),
            "verify": {
                "repository": str(repo),
                "artifacts": str(artifacts),
                "candidate_sha": candidate,
                "base_sha": base,
            },
        }
        env = dict(os.environ, FACTORY_OPS_ROOT=str(root / "no-ops"))
        env.pop("FACTORY_LAUNCHER", None)
        env.pop("FACTORY_HERMES_REPO", None)
        subprocess.run(
            [
                str(Path(sys.argv[1]).absolute()),
                "-I",
                str(Path(__file__).resolve()),
                "--child",
                json.dumps(payload),
            ],
            env=env,
            cwd=root,
            check=True,
            timeout=120,
        )


if __name__ == "__main__":
    main()
