"""Trusted pytest reporting plugin for factory replay.

The replay engine loads this module by absolute path from its *own* source tree
and never from either candidate checkout, then passes the recorder object to
``pytest.main(plugins=[...])``. Because the recorder is registered explicitly and
plugin autoloading is disabled for the child, a candidate revision cannot
contribute a plugin, conftest or ini option that silently changes the outcome.

The recorder writes what pytest itself observed for every node -- setup, call and
teardown outcome plus the real exception class -- so a replay verdict never
depends on parsing stdout. It is diagnostic instrumentation, not a hostile-code
sandbox: candidate code shares the interpreter.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

SCHEMA_NAME = "factory-replay-phase-report"
SCHEMA_VERSION = 1

PHASES = ("setup", "call", "teardown")
_REPORT_MAX_NODES = 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _short(value: object, limit: int = 300) -> str:
    text = str(value) if value is not None else ""
    return text if len(text) <= limit else text[:limit] + "..."


def _phase_entry(report, call) -> dict:
    """One authoritative phase record for a node from pytest's own report."""
    entry: dict = {
        "outcome": report.outcome,
        "duration": round(float(report.duration), 6),
    }
    excinfo = getattr(call, "excinfo", None)
    if excinfo is not None:
        exc_type = excinfo.type
        try:
            is_assertion = bool(issubclass(exc_type, AssertionError))
        except TypeError:  # pragma: no cover - excinfo always holds a class
            is_assertion = False
        entry["exception_type"] = getattr(exc_type, "__name__", str(exc_type))
        entry["exception_module"] = getattr(exc_type, "__module__", "")
        entry["exception_is_assertion"] = is_assertion
    return entry


class PhaseRecorder:
    """Explicit pytest plugin: records phases and writes one JSON report."""

    def __init__(self, output_path, nonce: str, checkout: str):
        self.output_path = Path(output_path)
        self.nonce = nonce
        self.checkout = str(Path(checkout).resolve())
        self.nodes: dict[str, dict] = {}
        self.collected: list[str] = []
        self.collection_errors: list[dict] = []
        self.exitstatus: int | None = None
        self._written = False

    def pytest_collection_modifyitems(self, session, config, items):
        self.collected = [item.nodeid for item in items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(
                {
                    "nodeid": _short(report.nodeid, 300),
                    "outcome": report.outcome,
                    "message": _short(getattr(report, "longrepr", "")),
                }
            )

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if report.when in PHASES and len(self.nodes) < _REPORT_MAX_NODES:
            self.nodes.setdefault(item.nodeid, {})[report.when] = _phase_entry(
                report, call
            )
        return report

    def pytest_sessionfinish(self, session, exitstatus):
        self.exitstatus = int(exitstatus)
        self._write()

    def _write(self) -> None:
        if self._written:
            return
        self._written = True
        plugin_path = Path(__file__).resolve()
        payload = {
            "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
            "nonce": self.nonce,
            "plugin": {
                "path": str(plugin_path),
                "sha256": "sha256:" + sha256_file(plugin_path),
            },
            "checkout": self.checkout,
            "cwd": os.getcwd(),
            "python": {
                "executable": sys.executable,
                "version": sys.version.split()[0],
            },
            "pytest": {"version": pytest.__version__},
            "exitstatus": self.exitstatus,
            "collected": list(self.collected),
            "collection_errors": list(self.collection_errors),
            "nodes": self.nodes,
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output_path.with_name(self.output_path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.output_path)
