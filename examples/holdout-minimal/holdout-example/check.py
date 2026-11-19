"""Example only: real holdouts live outside the builder's repository."""

import os
from urllib.request import urlopen

passed = failed = 0
base = os.environ.get("APP_URL", "http://app:8000")
for number in (1, 2, -3):
    try:
        with urlopen(f"{base}/?n={number}", timeout=5) as response:
            assert int(response.read()) == number * 2
        passed += 1
    except Exception as exc:  # noqa: BLE001 - diagnostic output remains private
        print(f"n={number}: {exc}")
        failed += 1
print(f"{failed} failed, {passed} passed")
raise SystemExit(1 if failed else 0)
