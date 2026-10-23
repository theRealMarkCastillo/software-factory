"""Checkout import guard for replay execution.

The engine is installed as an editable package in the runner's virtualenv, and
an editable finder will happily satisfy ``factory.<submodule>`` for a name the
checkout does not contain. That would silently mix the *current* engine source
into a replay of a historical revision -- exactly the isolation this module
exists to prevent.

Installed at the front of ``sys.meta_path``, the guard resolves any top-level
package that exists in the checkout *only* from the checkout and refuses a
missing submodule instead of falling through to another finder. Names that do
not exist in the checkout are left untouched.
"""

from __future__ import annotations

import sys
from importlib.machinery import PathFinder
from pathlib import Path


class CheckoutImportGuard:
    """Resolve checkout-owned packages from the checkout and nowhere else."""

    def __init__(self, checkout: Path, protected: tuple[str, ...] = ("factory",)):
        self.checkout = Path(checkout).resolve()
        self.protected = frozenset(protected)

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.partition(".")[0]
        package = self.checkout / top
        if not (package / "__init__.py").is_file():
            if top in self.protected:
                raise ModuleNotFoundError(
                    f"{fullname!r} is requested from a checkout that does not "
                    f"contain the {top!r} package; refusing to fall back to the "
                    f"installed engine"
                )
            return None
        if "." not in fullname:
            search = [str(self.checkout)]
        else:
            parent_name = fullname.rpartition(".")[0]
            parent = sys.modules.get(parent_name)
            search = list(getattr(parent, "__path__", []) or [])
            if not search:
                return None
        spec = PathFinder.find_spec(fullname, search)
        if spec is None or spec.origin is None:
            raise ModuleNotFoundError(
                f"{fullname!r} requested by the replay is not present in the "
                f"checkout {self.checkout}; refusing to fall back to the engine"
            )
        origin = Path(spec.origin).resolve()
        if origin != self.checkout and not origin.is_relative_to(self.checkout):
            raise ModuleNotFoundError(
                f"{fullname!r} resolved outside the requested checkout: {origin}"
            )
        return spec


def install(checkout: Path | str) -> CheckoutImportGuard:
    guard = CheckoutImportGuard(Path(checkout))
    sys.meta_path.insert(0, guard)
    return guard
