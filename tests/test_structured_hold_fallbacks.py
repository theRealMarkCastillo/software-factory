"""Fallbacks used by hold nodes and read-only legacy inspection."""

import pytest
from factory_orch_fixtures import seed

from factory_orchestration.graph import HOLD_ROUTES, build_graph, thread_id
from factory_orchestration.runner import _checkpoint_saver, inspect_run
from factory_orchestration.state import initial_state


@pytest.mark.parametrize(
    "node,reason",
    [
        ("hold_budget", "budget_exhausted"),
        ("hold_infrastructure", "worker_unavailable"),
        ("hold_reconciliation", "effect_unknown"),
        ("hold_operator", "protocol_error"),
    ],
)
def test_legacy_reason_fallback_uses_hold_stage_not_end_route(tmp_path, node, reason):
    seeded = seed(tmp_path)
    deps = seeded["deps"]
    thread = thread_id(seeded["assignment"])
    deps.store.record_admission(thread, seeded["assignment"], seeded["attempt"])
    state = initial_state(seeded["assignment"], seeded["attempt"], owner_token="test")
    state.update(status="held", stage=node, route="end", repair_context={"source": ""})
    with _checkpoint_saver(deps.store) as saver:
        graph = build_graph(deps, saver)
        graph.update_state({"configurable": {"thread_id": thread}}, state, as_node=node)
    view = inspect_run(deps, thread)
    assert view["hold_record"]["reason"] == reason
    assert view["hold_record"]["stage"] == node
    assert view["hold_record"]["spec_excerpt"] == ""
    assert node in HOLD_ROUTES
