"""Real HTTP example behavior; not a Docker deployment claim."""

import os
import runpy
import subprocess
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "holdout-minimal"


@pytest.mark.parametrize("overfit", [False, True])
def test_example_visible_passes_but_holdout_catches_overfit(tmp_path, overfit):
    source = (EXAMPLE / "app.py").read_text()
    if overfit:
        source = source.replace("str(number * 2).encode()", 'b"2"')
    candidate = tmp_path / "app.py"
    candidate.write_text(source)
    handler = runpy.run_path(str(candidate))["Handler"]
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, "APP_URL": f"http://127.0.0.1:{server.server_port}"}
    try:
        visible = subprocess.run(
            [sys.executable, str(EXAMPLE / "visible.py")],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        holdout = subprocess.run(
            [sys.executable, "-B", str(EXAMPLE / "holdout-example" / "check.py")],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert visible.returncode == 0, visible.stderr
        assert holdout.returncode == (1 if overfit else 0), holdout.stderr
        assert (
            "2 failed, 1 passed" if overfit else "0 failed, 3 passed"
        ) in holdout.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
