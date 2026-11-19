"""Visible smoke test intentionally exercises only n=1."""

import os
from urllib.request import urlopen

with urlopen(
    os.environ.get("APP_URL", "http://app:8000") + "/?n=1", timeout=5
) as response:
    assert response.read() == b"2"
print("1 passed")
