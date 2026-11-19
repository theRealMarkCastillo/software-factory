#!/usr/bin/env python3
"""Read-only, stdlib-only Kanban run/session rollups (not a factory engine API)."""

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

COUNTS = (
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
COSTS = ("estimated_cost_usd", "actual_cost_usd")


def open_sessions(path):
    """Open an existing database without creating it or permitting writes."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def timestamp(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("ISO timestamps must include a timezone") from None
        result = parsed.timestamp()
    if not math.isfinite(result):
        raise ValueError("timestamps must be finite")
    return result


def read_kanban(board, exports, card_id=None):
    if exports is not None:
        name = f"{card_id}.json" if card_id is not None else "list.json"
        return json.loads((exports / name).read_text(encoding="utf-8"))
    command = ["hermes", "kanban", "--board", board]
    command += ["show", card_id] if card_id is not None else ["list"]
    result = subprocess.run(
        [*command, "--json"], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def cards_from_listing(data):
    if isinstance(data, list):
        return data
    if "tasks" in data:
        return data["tasks"]
    if "columns" in data:
        columns = data["columns"]
        if isinstance(columns, dict):
            return [card for column in columns.values() for card in column]
    raise ValueError(
        "list JSON must be an array, {'tasks': [...]} or {'columns': {...}}"
    )


def load_runs(board, exports, prefixes, include_others=False):
    runs = []
    seen_cards = set()
    for card in cards_from_listing(read_kanban(board, exports)):
        group = next((p for p in prefixes if card["title"].startswith(p)), "others")
        if group == "others" and not include_others:
            continue
        card_id = card["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", card_id):
            raise ValueError("invalid card id")
        if card_id in seen_cards:
            continue
        seen_cards.add(card_id)
        detail = read_kanban(board, exports, card_id)
        task = detail["task"]
        title = task["title"]
        for run in detail.get("runs", []):
            runs.append(
                {
                    **run,
                    "card_id": card_id,
                    "title": title,
                    "group": group,
                    "workspace_path": task.get("workspace_path"),
                }
            )
    return runs


def match_sessions(runs, home, tolerance):
    sessions = {}
    errors = {}
    for profile in {run.get("profile") for run in runs}:
        if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
            errors[profile] = "invalid or missing profile"
            continue
        try:
            with closing(open_sessions(home / "profiles" / profile / "state.db")) as db:
                available = {
                    row[1] for row in db.execute("PRAGMA table_info(sessions)")
                }
                fields = (
                    "id",
                    "started_at",
                    "title",
                    "cwd",
                    "model",
                    "billing_mode",
                    "cost_status",
                    *COUNTS,
                    *COSTS,
                )
                # Only constant allowlisted identifiers enter this query. Never load
                # unrelated session payloads, prompts, or configuration.
                columns = ", ".join(field for field in fields if field in available)
                sessions[profile] = [
                    dict(row)
                    for row in db.execute(
                        f"SELECT {columns} FROM sessions WHERE source = 'kanban'"
                    )
                ]
        except sqlite3.Error:
            errors[profile] = "session database unavailable or incompatible"
    candidates = {}
    reasons = {}
    for index, run in enumerate(runs):
        profile = run.get("profile")
        start = timestamp(run.get("started_at"))
        if profile in errors or start is None:
            reasons[index] = errors.get(profile, "missing run start")
            continue
        nearby = []
        for session in sessions[profile]:
            session_start = timestamp(session.get("started_at"))
            if session_start is not None and session.get("id") is not None:
                distance = abs(session_start - start)
                if distance <= tolerance:
                    nearby.append((distance, session))
        nearby.sort(key=lambda item: item[0])
        identified = []
        highest_rank = 0
        for distance, session in nearby:
            title = re.sub(r" #\d+$", "", session.get("title") or "").removesuffix("…")
            title_match = bool(title) and (run.get("title") or "").startswith(title)
            cwd_match = bool(session.get("cwd")) and session["cwd"] == run.get(
                "workspace_path"
            )
            # Both > title-only > cwd-only; time cannot break an identity tie.
            rank = 2 * int(title_match) + int(cwd_match)
            if rank > highest_rank:
                highest_rank = rank
                identified = [(distance, session)]
            elif rank and rank == highest_rank:
                identified.append((distance, session))
        if len(identified) > 1:
            reasons[index] = "ambiguous identifying sessions"
        elif len(identified) == 1:
            candidates[index] = identified[0]
        elif not nearby:
            reasons[index] = "no session within tolerance"
        elif len(nearby) > 1 and nearby[0][0] == nearby[1][0]:
            reasons[index] = "equidistant sessions"
        else:
            candidates[index] = nearby[0]
    uses = Counter(
        (runs[i]["profile"], item[1]["id"]) for i, item in candidates.items()
    )
    matches = []
    for index, (distance, session) in candidates.items():
        run = runs[index]
        if uses[(run["profile"], session["id"])] > 1:
            reasons[index] = "session selected by multiple runs"
            continue
        run["session"] = session
        matches.append(
            {
                "card_id": run["card_id"],
                "run_id": run.get("id"),
                "profile": run["profile"],
                "session_id": session["id"],
                "distance_seconds": distance,
            }
        )
    unmatched = [
        {
            "card_id": runs[i]["card_id"],
            "run_id": runs[i].get("id"),
            "profile": runs[i].get("profile"),
            "reason": reason,
        }
        for i, reason in sorted(reasons.items())
    ]
    return matches, unmatched


def rollup(runs):
    intervals = []
    for run in runs:
        start = timestamp(run.get("started_at"))
        end = timestamp(run.get("ended_at"))
        if start is not None and end is not None and end >= start:
            intervals.append((start, end))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    sessions = [r["session"] for r in runs if "session" in r]
    result = {
        "runs": len(runs),
        "matched_runs": len(sessions),
        "unmatched_runs": len(runs) - len(sessions),
        "incomplete_intervals": len(runs) - len(intervals),
        "worker_minutes": sum(end - start for start, end in intervals) / 60,
        "active_minutes": sum(end - start for start, end in merged) / 60,
        "elapsed_minutes": (merged[-1][1] - merged[0][0]) / 60 if merged else 0,
        "blocked_runs": sum(
            r.get("status") == "blocked" or r.get("outcome") == "blocked" for r in runs
        ),
        "qa_fail_runs": sum((r.get("summary") or "").startswith("FAIL") for r in runs),
        "models": sorted({s["model"] for s in sessions if s.get("model")}),
        "billing_mode": dict(
            Counter(s.get("billing_mode") or "unknown" for s in sessions)
        ),
        "cost_status": dict(
            Counter(s.get("cost_status") or "unknown" for s in sessions)
        ),
        "missing_values": {},
    }
    for field in (*COUNTS, *COSTS):
        values = []
        missing = 0
        for session in sessions:
            included = any(
                str(session.get(key) or "").lower().replace("-", "_")
                in {"subscription", "subscription_included"}
                for key in ("billing_mode", "cost_status")
            )
            if field in COSTS and included:
                continue
            value = session.get(field)
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                values.append(value)
            else:
                missing += 1
        result[field] = sum(values)
        result["missing_values"][field] = missing
    denominator = result["input_tokens"] + result["cache_read_tokens"]
    result["cache_hit_share"] = (
        result["cache_read_tokens"] / denominator if denominator else None
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--prefix", action="append", required=True)
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser(),
    )
    parser.add_argument("--kanban-json-dir", type=Path)
    parser.add_argument("--tolerance", type=float, default=15)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--include-others",
        action="store_true",
        help="also fetch and report cards outside the selected prefixes",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.tolerance) or args.tolerance < 0:
        parser.error("--tolerance must be finite and nonnegative")
    prefixes = list(dict.fromkeys(args.prefix))
    if any(not p or p == "others" for p in prefixes):
        parser.error("prefixes must be nonempty and must not be 'others'")
    try:
        runs = load_runs(
            args.board, args.kanban_json_dir, prefixes, args.include_others
        )
        matches, unmatched = match_sessions(runs, args.hermes_home, args.tolerance)
        report = {
            "schema_version": 1,
            "board": args.board,
            "tolerance_seconds": args.tolerance,
            "groups": {
                p: rollup([r for r in runs if r["group"] == p])
                for p in [*prefixes, *(["others"] if args.include_others else [])]
            },
            "total": rollup(runs),
            "matches": matches,
            "unmatched_runs": unmatched,
        }
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"metrics error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(f"Board: {args.board} (tolerance {args.tolerance:g}s)")
        for group, values in [*report["groups"].items(), ("TOTAL", report["total"])]:
            print(f"\n{group}")
            for key, value in values.items():
                print(f"  {key}: {json.dumps(value, sort_keys=True)}")
        print(f"\nUnmatched runs: {len(unmatched)}")
        for run in unmatched:
            print(f"  {run['card_id']} / {run['run_id']}: {run['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
