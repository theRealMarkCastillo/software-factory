"""Rule interactions at the repair diagnostic boundary (synthetic values only)."""

import json
from dataclasses import replace

import pytest
from factory_orch_fixtures import seed
from test_repair_context import candidate_sequence, capture, run

from factory_orchestration.graph import _failure_signature, thread_id
from factory_orchestration.redaction import redact_diagnostic
from factory_orchestration.state import GateOutcome

BODY = "synthetic-" + "private-body"


def pem(kind="RSA "):
    begin = "-----BEGIN " + kind + "PRIVATE " + "KEY-----"
    end = "-----END " + kind + "PRIVATE " + "KEY-----"
    return f"{begin}\n{BODY}\n{end}"


@pytest.mark.parametrize(
    "value", ["PRIVATE KEY", "-----BEGIN", "KEY-----", "RSA PRIVATE"]
)
def test_env_value_inside_pem_delimiters_cannot_expose_the_body(monkeypatch, value):
    monkeypatch.setenv("FIXTURE_AUTH", value)
    out = redact_diagnostic("before\n" + pem() + "\nafter")
    assert BODY not in out
    assert out.startswith("before\n") and out.endswith("\nafter")


def test_env_value_inside_orphan_pem_end_cannot_expose_the_body(monkeypatch):
    monkeypatch.setenv("FIXTURE_AUTH", "PRIVATE KEY")
    cut = BODY + "\n" + pem().splitlines()[-1] + "\nERROR: tail stays"
    out = redact_diagnostic(cut, cut_window=True)
    assert BODY not in out
    assert out.endswith("ERROR: tail stays")


def test_env_value_inside_a_token_shape_leaves_no_fragment(monkeypatch):
    token = "sk" + "-" + "abcdefgh" + "ijklmnopqrstuvwx"
    monkeypatch.setenv("FIXTURE_TOKEN", "abcdefgh")
    out = redact_diagnostic("ERROR: " + token + " end")
    assert "ijklmnop" not in out and "abcdefgh" not in out
    assert out.startswith("ERROR: ") and out.endswith(" end")


def test_redaction_is_stable_when_applied_twice(monkeypatch):
    monkeypatch.setenv("FIXTURE_AUTH", "PRIVATE KEY")
    once = redact_diagnostic("x " + pem() + " y")
    assert redact_diagnostic(once) == once


@pytest.mark.parametrize("channel", ["detail", "log", "review"])
def test_graph_sinks_never_hold_a_pem_body(tmp_path, monkeypatch, channel):
    monkeypatch.setenv("FIXTURE_AUTH", "PRIVATE KEY")
    s = seed(tmp_path)
    requests = capture(s)
    if channel == "review":
        original = s["reviewer"].submit

        def submit(**kw):
            s["reviewer"].outcome = "changes_required"
            handle, review = original(**kw)
            return handle, replace(review, comment="fix\n" + pem())

        s["reviewer"].submit = submit
    else:
        log = s["artifacts"] / "acceptance.log"
        log.write_text("ERROR: boom\n" + pem() + "\n")
        s["gate"].run_acceptance = lambda **kw: GateOutcome(
            "fail",
            reason="test_failed",
            detail=pem() if channel == "detail" else "failed",
            log_reference=str(log) if channel == "log" else "",
        )
    result = run(s)
    persisted = json.dumps(
        {
            "state": result.state,
            "operations": s["deps"].store.operations(thread_id(s["assignment"])),
        }
    )
    assert len(requests) >= 2
    assert BODY not in persisted
    assert all(BODY not in request.prompt for request in requests)


def test_hold_detail_never_holds_a_pem_body(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_AUTH", "PRIVATE KEY")
    s = seed(tmp_path)
    s["gate"].run_acceptance = lambda **kw: GateOutcome(
        "unavailable", reason="gate_unavailable", detail="down\n" + pem()
    )
    result = run(s)
    assert result.state["status"] == "held"
    assert BODY not in json.dumps(result.state)


@pytest.mark.parametrize(
    "pair",
    [
        ("invalid clock 99:99:99", "invalid clock 98:98:98"),
        ("invalid date 9999-99-99", "invalid date 9999-98-98"),
        ("stamp 2026-13-40T25:61:61Z", "stamp 2026-14-41T26:62:62Z"),
        ("opened 10:15:00", "opened 11:45:30"),
        ("due 2026-11-14", "due 2026-11-15"),
    ],
)
def test_non_iso_values_keep_fallback_identities_distinct(tmp_path, pair):
    assert _failure_signature({"detail": "ERROR: " + pair[0]}) != _failure_signature(
        {"detail": "ERROR: " + pair[1]}
    )
    s = seed(tmp_path)
    requests, _ = candidate_sequence(s)
    log = s["artifacts"] / "acceptance.log"
    calls = []

    def acceptance(**kw):
        text = pair[min(len(calls), 1)]
        calls.append(kw)
        log.write_text("ERROR: " + text + "\n")
        return GateOutcome("fail", reason="test_failed", detail=str(log))

    s["gate"].run_acceptance = acceptance
    result = run(s)
    # The third failure repeats the second, so the reason may be either hold;
    # what matters is that the distinct second failure was allowed a repair.
    assert result.state["repair_count"] == 2
    assert len(requests) == 3


def test_true_volatile_values_still_merge():
    assert _failure_signature(
        {"detail": "ERROR: at 2026-11-14T10:00:00Z in 3.4s 0xabc abcdef123456"}
    ) == _failure_signature(
        {"detail": "ERROR: at 2026-11-15 10:01:00.5+01:00 in 12 ms 0xdef 123456abcdef"}
    )
