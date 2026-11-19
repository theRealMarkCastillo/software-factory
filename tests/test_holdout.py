"""Holdout boundaries; Docker is simulated, not deployment acceptance."""

import json
from pathlib import Path

import pytest

from factory import runner


@pytest.mark.parametrize(
    "options, refusal",
    [
        ({"deployment": True, "check": "check.sh"}, "deployment"),
        ({"run_gates": False}, "no-gates"),
        ({"mode": "diagnostic"}, "diagnostic"),
    ],
)
def test_holdout_refuses_non_acceptance_before_any_work(tmp_path, options, refusal):
    with pytest.raises(ValueError, match=refusal):
        runner.run_factory_card(
            "holdout", str(tmp_path / "absent-repo"), ["compose.yml"],
            holdout=str(tmp_path / "private"), **options,
        )
