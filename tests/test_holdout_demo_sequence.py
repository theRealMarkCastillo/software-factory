"""README Git sequence with real gates/HTTP; Docker boundary is simulated.

This proves the diff-based coverage and runner stage, not Compose isolation or
host safety. Live Compose acceptance must be checked independently.
"""

import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

from test_holdout_lifecycle import lifecycle  # noqa: F401 - shared Docker fixture

from factory import runner
from factory.runner import _run_preflight_gates

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "holdout-minimal"


def test_readme_original_base_overfit_reaches_holdout_failed(lifecycle, monkeypatch):  # noqa: F811
    repo, private, artifacts, _, _, _ = lifecycle
    # Restore real audit/coverage/datamodel gates; only Docker remains simulated.
    monkeypatch.setattr(runner, "_run_preflight_gates", _run_preflight_gates)
    names = ["app.py", "visible.py", "Dockerfile", "compose.yml", ".env.example"]
    # Copy every standalone candidate file, including the visible pytest suite.
    names += [p.name for p in EXAMPLE.glob("test_*.py")]
    for name in names:
        shutil.copyfile(EXAMPLE / name, repo / name)
    shutil.copyfile(EXAMPLE / "holdout-example/check.py", private / "check.py")
    (private / "test_secret.py").unlink()
    shutil.copyfile(EXAMPLE / "holdout-example/holdout.json", private / "holdout.json")

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("add", *names)
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "test: add doubling demo",
    )
    base = git("rev-parse", "HEAD")

    def compose(project, files, args, **kwargs):
        if args[0] != "up":
            return subprocess.CompletedProcess([], 0, "", "")
        handler = runpy.run_path(str(repo / "app.py"))["Handler"]
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        script = private / "check.py" if len(files) > 1 else repo / "visible.py"
        try:
            return subprocess.run(
                [sys.executable, "-B", str(script)],
                env={**os.environ, "APP_URL": f"http://127.0.0.1:{server.server_port}"},
                capture_output=True,
                text=True,
                timeout=15,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    monkeypatch.setattr(runner, "run_compose", compose)

    def run(card):
        return runner.run_factory_card(
            card,
            str(repo),
            ["compose.yml"],
            base_ref=base,
            artifacts_root=str(artifacts),
            holdout=str(private),
        )

    good = run("holdout-demo")
    assert good.stage == "complete", good
    app = repo / "app.py"
    app.write_text(app.read_text().replace("str(number * 2).encode()", 'b"2"'))
    git("add", "app.py")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "test: overfit n=1",
    )
    assert git("rev-parse", "HEAD") != base
    overfit = run("holdout-demo-overfit")  # BASE deliberately unchanged.
    assert overfit.stage == "holdout_failed", overfit
    coverage = overfit.gates["coverage"]
    assert coverage["passed"] and coverage["test_exit_code"] == 0
    assert coverage["base_sha"] == base
    manifest = json.loads(
        (Path(overfit.artifacts_dir) / "evidence-manifest.json").read_text()
    )
    assert manifest["holdout"]["passed"] == 1
    assert manifest["holdout"]["failed"] == 2
