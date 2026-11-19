"""Visible coverage test: deliberately checks only n=1, just like visible.py."""

import runpy
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen


def test_double_one():
    handler = runpy.run_path(str(Path(__file__).with_name("app.py")))["Handler"]
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/?n=1", timeout=5
        ) as response:
            assert response.read() == b"2"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
