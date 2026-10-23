"""Child bootstrap for one replay side.

Run as ``<engine python> _replay_child.py <spec.json>`` from the suite
directory. The bootstrap:

* removes its own source directory from ``sys.path`` so the replay engine tree
  can never shadow the checkout under test;
* puts the requested checkout first on ``sys.path``;
* loads the trusted reporting plugin by absolute path (never by package name);
* runs pytest with an explicit config file, no conftest and no plugin autoload
  (the parent already scrubbed ``PYTEST_ADDOPTS``/``PYTEST_PLUGINS`` and set
  ``PYTEST_DISABLE_PLUGIN_AUTOLOAD``).

The spec and report paths live outside both checkouts. Nothing here is a
sandbox: it is pytest *configuration* isolation for reviewed code only.
"""

from __future__ import annotations

import json
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

EXIT_BAD_SPEC = 90


def _load_by_path(path: Path, name: str):
    spec = spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - impossible for a file
        raise RuntimeError(f"cannot load plugin from {path}")
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: _replay_child.py <spec.json>", file=sys.stderr)
        return EXIT_BAD_SPEC
    try:
        spec = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
        checkout = Path(spec["checkout"]).resolve()
        plugin_path = Path(spec["plugin_path"]).resolve()
        report_path = Path(spec["report_path"]).resolve()
        nonce = str(spec["nonce"])
        pytest_args = [str(argument) for argument in spec["pytest_args"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"replay bootstrap: bad spec: {exc}", file=sys.stderr)
        return EXIT_BAD_SPEC

    own_directory = Path(__file__).resolve().parent
    sys.path[:] = [
        entry
        for entry in sys.path
        if Path(entry or os.getcwd()).resolve() != own_directory
    ]
    sys.path.insert(0, str(checkout))

    if plugin_path.is_relative_to(checkout):  # pragma: no cover - parent refuses first
        print("replay bootstrap: plugin lives inside the checkout", file=sys.stderr)
        return EXIT_BAD_SPEC

    # Install the checkout guard before importing anything the suite may shadow:
    # an editable engine install must never satisfy a missing checkout module.
    isolation = _load_by_path(
        Path(__file__).resolve().parent / "_replay_isolation.py",
        "factory_replay_isolation",
    )
    isolation.install(checkout)

    import pytest  # imported after sys.path is pinned to the checkout

    plugin = _load_by_path(plugin_path, "factory_replay_trusted_plugin")
    recorder = plugin.PhaseRecorder(report_path, nonce, str(checkout))
    return int(pytest.main(pytest_args, plugins=[recorder]))


if __name__ == "__main__":
    raise SystemExit(main())
