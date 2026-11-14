"""Receipt lookup must not depend on timestamp/key ordering."""

import json
from types import SimpleNamespace

from test_repair_context import receipt


def test_receipt_selects_repair_index_when_updated_at_ties():
    records = [
        {
            "kind": "gate",
            "repair_index": index,
            "updated_at": "same",
            "receipt_json": json.dumps({"detail": f"repair-{index}"}),
        }
        for index in (1, 0)
    ]
    store = SimpleNamespace(operations=lambda thread: records)
    assignment = SimpleNamespace(board="fixture", card_id="fixture")
    # Use the real assignment shape rather than relying on its thread encoding.
    from unittest.mock import patch

    s = {"deps": SimpleNamespace(store=store), "assignment": assignment}
    with patch("test_repair_context.thread_id", return_value="fixture"):
        assert receipt(s, "gate")["detail"] == "repair-0"
