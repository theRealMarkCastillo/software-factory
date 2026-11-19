"""Synthetic-only end-to-end coverage for the optional Hermes metrics script."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/hermes_chain_metrics.py"


def make_exports(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    cards = [
        ("card-a", "PRODUCT-A build", "builder", 100, 220, "done", "PASS"),
        ("card-b", "PRODUCT-A review", "reviewer", 160, 280, "blocked", "FAIL issue"),
        ("card-c", "SUPPORT check", "builder", 400, 460, "done", "PASS"),
    ]
    (exports / "list.json").write_text(
        json.dumps({"tasks": [{"id": c[0], "title": c[1]} for c in cards]})
    )
    for index, (card, title, profile, start, end, status, summary) in enumerate(cards):
        (exports / f"{card}.json").write_text(
            json.dumps(
                {
                    "task": {"id": card, "title": title},
                    "runs": [
                        {
                            "id": index + 1,
                            "profile": profile,
                            "started_at": start,
                            "ended_at": end,
                            "status": status,
                            "outcome": status,
                            "summary": summary,
                        }
                    ],
                }
            )
        )
    home = tmp_path / "home"
    for profile, rows in {
        "builder": [("session-a", 114, "kanban"), ("outside", 416, "kanban")],
        "reviewer": [("session-b", 160, "kanban"), ("ignore", 100, "cli")],
    }.items():
        db = home / "profiles" / profile / "state.db"
        db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE sessions (id TEXT, started_at REAL, source TEXT, "
                "model TEXT, api_call_count INTEGER, input_tokens INTEGER, "
                "output_tokens INTEGER, cache_read_tokens INTEGER, "
                "cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
                "estimated_cost_usd REAL, actual_cost_usd REAL, "
                "billing_mode TEXT, cost_status TEXT)"
            )
            for session, start, source in rows:
                included = profile == "reviewer"
                conn.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        session, start, source, f"model-{profile}", 2, 100, 20,
                        300, 10, 5, 9 if included else 0.5,
                        8 if included else 0.4,
                        "subscription" if included else "api",
                        "subscription_included" if included else "priced",
                    ),
                )
    return exports, home


def run_cli(exports, home, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--board", "example-board", "--prefix",
         "PRODUCT-A", "--hermes-home", str(home), "--kanban-json-dir",
         str(exports), *extra],
        text=True, capture_output=True, check=False,
    )


def test_cli_rollup_join_costs_and_intervals(tmp_path):
    exports, home = make_exports(tmp_path)
    result = run_cli(exports, home, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["board"] == "example-board"
    product = report["groups"]["PRODUCT-A"]
    assert product["runs"] == 2
    assert product["worker_minutes"] == 4
    assert product["active_minutes"] == 3
    assert product["elapsed_minutes"] == 3
    assert product["blocked_runs"] == 1
    assert product["qa_fail_runs"] == 1
    assert product["models"] == ["model-builder", "model-reviewer"]
    assert product["api_call_count"] == 4
    assert product["input_tokens"] == 200
    assert product["output_tokens"] == 40
    assert product["cache_read_tokens"] == 600
    assert product["cache_write_tokens"] == 20
    assert product["reasoning_tokens"] == 10
    assert product["cache_hit_share"] == 0.75
    assert product["estimated_cost_usd"] == 0.5
    assert product["actual_cost_usd"] == 0.4
    assert product["billing_mode"] == {"api": 1, "subscription": 1}
    assert product["cost_status"] == {"priced": 1, "subscription_included": 1}
    assert report["groups"]["others"]["matched_runs"] == 0
    assert report["total"]["runs"] == 3
    assert report["total"]["worker_minutes"] == 5
    assert report["total"]["active_minutes"] == 4
    assert report["total"]["elapsed_minutes"] == 6
    assert report["unmatched_runs"][0]["card_id"] == "card-c"
    assert len(report["unmatched_runs"]) == 1
    assert {m["session_id"] for m in report["matches"]} == {"session-a", "session-b"}


def test_database_is_opened_read_only(tmp_path):
    _, home = make_exports(tmp_path)
    spec = importlib.util.spec_from_file_location("chain_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.open_sessions(home / "profiles/builder/state.db") as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden (id TEXT)")


def test_text_output_and_tolerance_override(tmp_path):
    exports, home = make_exports(tmp_path)
    result = run_cli(exports, home, "--tolerance", "16")
    assert result.returncode == 0, result.stderr
    assert "PRODUCT-A" in result.stdout
    assert "subscription_included" in result.stdout
    assert "Unmatched runs: 0" in result.stdout
    assert "actual_cost_usd" in result.stdout
