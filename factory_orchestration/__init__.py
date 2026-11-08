"""Opt-in LangGraph orchestration for bounded factory cards.

This package is deliberately separate from the standalone verification engine
(``factory``). Importing ``factory`` never imports this module and never requires
LangGraph; the orchestration extra is optional and pinned in ``pyproject.toml``.

Ownership (see ``docs/langgraph-hybrid/ownership.md``):

* Hermes Kanban owns cards, claims, attempts, workers and lifecycle transitions.
* ``factory_orchestration`` owns intra-card stages, checkpoints and the
  interpretation of structured worker/gate results.
* ``factory`` (the engine) owns policy admission, gate execution, evidence
  validity and promotion semantics.

A terminal graph result is a *proposal* to the lifecycle owner. It is not a card
transition, and it never promotes or publishes anything.
"""

from .state import GRAPH_VERSION, STATE_SCHEMA_VERSION

__all__ = ["GRAPH_VERSION", "STATE_SCHEMA_VERSION"]
