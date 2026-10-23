"""Preflight probe for one replay checkout.

Executed as ``<engine python> _replay_preflight.py <checkout>`` with a minimal
environment and the checkout first on ``sys.path``. It installs the checkout
import guard, imports ``factory`` (and ``factory.evidence`` when the snapshot has
it) and prints which files those imports actually came from. The parent refuses
the replay unless every file belongs to the requested checkout, so a historical
module can never silently come from the installed engine.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_by_path(path: Path, name: str):
    spec = spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {path}")
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: _replay_preflight.py <checkout>", file=sys.stderr)
        return 90
    checkout = Path(arguments[0]).resolve()
    isolation = _load_by_path(
        Path(__file__).resolve().parent / "_replay_isolation.py",
        "factory_replay_isolation",
    )
    isolation.install(checkout)

    import factory  # resolves through the guard

    info = {
        "factory_file": factory.__file__,
        "factory_path": list(getattr(factory, "__path__", [])),
        "executable": sys.executable,
        "python": sys.version.split()[0],
        "sitecustomize": [
            getattr(module, "__file__", None)
            for name, module in sys.modules.items()
            if name in ("sitecustomize", "usercustomize")
        ],
    }
    try:
        spec = importlib.util.find_spec("factory.evidence")
        info["evidence_file"] = spec.origin if spec is not None else None
    except ModuleNotFoundError:
        info["evidence_file"] = None
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
