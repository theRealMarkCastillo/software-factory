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
                        session,
                        start,
                        source,
                        f"model-{profile}",
                        2,
                        100,
                        20,
                        300,
                        10,
                        5,
                        9 if included else 0.5,
                        8 if included else 0.4,
                        "subscription" if included else "api",
                        "subscription_included" if included else "priced",
                    ),
                )
    return exports, home


def run_cli(exports, home, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--board",
            "example-board",
            "--prefix",
            "PRODUCT-A",
            "--hermes-home",
            str(home),
            "--kanban-json-dir",
            str(exports),
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_rollup_join_costs_and_intervals(tmp_path):
    exports, home = make_exports(tmp_path)
    result = run_cli(exports, home, "--include-others", "--json")
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


def test_subscription_billing_mode_never_counts_prices(tmp_path):
    exports, home = make_exports(tmp_path)
    with sqlite3.connect(home / "profiles/reviewer/state.db") as db:
        db.execute("UPDATE sessions SET cost_status = NULL")
    result = run_cli(exports, home, "--include-others", "--json")
    assert result.returncode == 0, result.stderr
    total = json.loads(result.stdout)["total"]
    assert total["estimated_cost_usd"] == 0.5
    assert total["actual_cost_usd"] == 0.4


@pytest.mark.parametrize("case", ["tie", "reuse", "missing-db"])
def test_uncertain_matches_are_reported_not_guessed(tmp_path, case):
    exports, home = make_exports(tmp_path)
    path = home / "profiles/builder/state.db"
    if case == "missing-db":
        path.unlink()
    else:
        with sqlite3.connect(path) as db:
            if case == "tie":
                db.execute(
                    "INSERT INTO sessions (id, started_at, source) "
                    "VALUES ('tie', 86, 'kanban')"
                )
            else:
                card = json.loads((exports / "card-c.json").read_text())
                card["runs"][0]["started_at"] = 100
                (exports / "card-c.json").write_text(json.dumps(card))
    result = run_cli(exports, home, "--include-others", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["total"]["matched_runs"] == 1
    assert len(report["unmatched_runs"]) == 2
    if case == "missing-db":
        assert not path.exists()


def test_multiple_prefixes_and_missing_values(tmp_path):
    exports, home = make_exports(tmp_path)
    with sqlite3.connect(home / "profiles/builder/state.db") as db:
        db.execute("UPDATE sessions SET actual_cost_usd = NULL")
    result = run_cli(
        exports, home, "--prefix", "SUPPORT", "--tolerance", "16", "--json"
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["groups"]["SUPPORT"]["matched_runs"] == 1
    assert "others" not in report["groups"]
    assert report["total"]["missing_values"]["actual_cost_usd"] == 2


def test_hermes_command_transport_uses_explicit_board(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("chain_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(command, 0, stdout='{"tasks": []}')

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.read_kanban("example-board", None) == {"tasks": []}
    module.read_kanban("example-board", None, "card-a")
    assert calls == [
        ["hermes", "kanban", "--board", "example-board", "list", "--json"],
        ["hermes", "kanban", "--board", "example-board", "show", "card-a", "--json"],
    ]


def test_query_does_not_read_unneeded_session_columns(tmp_path, monkeypatch):
    _, home = make_exports(tmp_path)
    db_path = home / "profiles/builder/state.db"
    with sqlite3.connect(db_path) as db:
        db.execute("ALTER TABLE sessions ADD COLUMN unrelated_payload TEXT")
    spec = importlib.util.spec_from_file_location("chain_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_open = module.open_sessions

    def restricted_open(path):
        conn = original_open(path)

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and column == "unrelated_payload":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(module, "open_sessions", restricted_open)
    runs = [{"card_id": "card-a", "id": 1, "profile": "builder", "started_at": 100}]
    matches, unmatched = module.match_sessions(runs, home, 15)
    assert len(matches) == 1
    assert unmatched == []


def test_cli_runs_without_site_packages(tmp_path):
    exports, home = make_exports(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(SCRIPT),
            "--board",
            "example-board",
            "--prefix",
            "PRODUCT-A",
            "--hermes-home",
            str(home),
            "--kanban-json-dir",
            str(exports),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["total"]["matched_runs"] == 2


@pytest.mark.parametrize("identity", ["title", "cwd"])
def test_concurrent_same_profile_runs_resolve_identity(tmp_path, identity):
    exports, home = make_exports(tmp_path)
    titles = ["PRODUCT-A alpha build", "PRODUCT-A beta review", "PRODUCT-A gamma test"]
    cards = []
    with sqlite3.connect(home / "profiles/builder/state.db") as db:
        db.execute("DELETE FROM sessions")
        db.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        db.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        for index, title in enumerate(titles):
            card_id = f"parallel-{index}"
            workspace = f"/synthetic/workspaces/{index}"
            cards.append({"id": card_id, "title": title})
            (exports / f"{card_id}.json").write_text(
                json.dumps(
                    {
                        "task": {
                            "id": card_id,
                            "title": title,
                            "workspace_path": workspace,
                        },
                        "runs": [
                            {
                                "id": index,
                                "profile": "builder",
                                "started_at": 100,
                                "ended_at": 160,
                            }
                        ],
                    }
                )
            )
            session_title = [titles[0], "PRODUCT-A beta…", titles[2] + " #2"][index]
            db.execute(
                "INSERT INTO sessions (id, started_at, source, title, cwd) "
                "VALUES (?, ?, 'kanban', ?, ?)",
                (
                    f"parallel-session-{index}",
                    101 + index,
                    session_title if identity == "title" else None,
                    workspace if identity == "cwd" else None,
                ),
            )
    (exports / "list.json").write_text(json.dumps({"tasks": cards}))
    result = run_cli(exports, home, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["unmatched_runs"] == []
    assert {(m["run_id"], m["session_id"]) for m in report["matches"]} == {
        (i, f"parallel-session-{i}") for i in range(3)
    }


def test_identity_ambiguity_stays_unmatched(tmp_path):
    exports, home = make_exports(tmp_path)
    with sqlite3.connect(home / "profiles/builder/state.db") as db:
        db.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        db.execute(
            "UPDATE sessions SET title = 'PRODUCT-A build' WHERE id = 'session-a'"
        )
        db.execute(
            "INSERT INTO sessions (id, started_at, source, title) "
            "VALUES ('ambiguous', 101, 'kanban', 'PRODUCT-A build')"
        )
    result = run_cli(exports, home, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["total"]["matched_runs"] == 1
    assert report["unmatched_runs"][0]["reason"] == "equidistant sessions"


def test_identity_cannot_select_outside_tolerance(tmp_path):
    exports, home = make_exports(tmp_path)
    with sqlite3.connect(home / "profiles/builder/state.db") as db:
        db.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        db.execute(
            "UPDATE sessions SET started_at = 116, title = 'PRODUCT-A build' "
            "WHERE id = 'session-a'"
        )
    result = run_cli(exports, home, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["unmatched_runs"][0]["reason"] == "no session within tolerance"


def test_default_does_not_read_nonmatching_export(tmp_path):
    exports, home = make_exports(tmp_path)
    (exports / "card-c.json").unlink()
    result = run_cli(exports, home, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report["groups"]) == {"PRODUCT-A"}
    assert report["total"]["runs"] == 2
    assert report["total"]["worker_minutes"] == 4
    assert report["unmatched_runs"] == []


@pytest.mark.parametrize("include_others", [False, True])
def test_only_selected_cards_are_fetched(tmp_path, monkeypatch, include_others):
    exports, _ = make_exports(tmp_path)
    spec = importlib.util.spec_from_file_location("chain_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fetched = []

    def recorder(board, directory, card_id=None):
        assert board == "example-board"
        if card_id is not None:
            fetched.append(card_id)
        return json.loads(
            (exports / (f"{card_id}.json" if card_id else "list.json")).read_text()
        )

    monkeypatch.setattr(module, "read_kanban", recorder)
    runs = module.load_runs(
        "example-board", None, ["PRODUCT-A"], include_others=include_others
    )
    assert fetched == (
        ["card-a", "card-b", "card-c"] if include_others else ["card-a", "card-b"]
    )
    assert {r["group"] for r in runs} == (
        {"PRODUCT-A", "others"} if include_others else {"PRODUCT-A"}
    )
