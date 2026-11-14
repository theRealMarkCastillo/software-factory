"""Synthetic-only regressions for the repair diagnostic boundary."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from factory_orch_fixtures import seed
from test_repair_context import (
    HEADER,
    PROMPT_STATE,
    candidate_sequence,
    capture,
    context,
    receipt,
    run,
)

from factory_orchestration.graph import (
    _failure_signature,
    _implementation_prompt,
    thread_id,
)
from factory_orchestration.state import GateOutcome


@pytest.mark.parametrize("channel", ["log", "detail", "review", "installed", "verify"])
def test_env_echo_is_redacted_before_prompt_state_receipt_and_hold(
    tmp_path, monkeypatch, channel
):
    secret = "synthetic-" + "credential-value"
    monkeypatch.setenv("FIXTURE_AUTH", secret)
    s = seed(tmp_path)
    requests = capture(s)
    if channel == "review":
        original = s["reviewer"].submit

        def submit(**kw):
            s["reviewer"].outcome = "changes_required"
            handle, review = original(**kw)
            return handle, replace(review, comment="fix " + secret)

        s["reviewer"].submit = submit
    elif channel == "installed":
        from factory_orchestration.adapters.gates import InstalledLauncherGate

        stub = tmp_path / "acceptance.py"
        stub.write_text(
            'import os, sys\nsys.stderr.write("ERROR: " + os.environ["FIXTURE_AUTH"])\n'
            "raise SystemExit(1)\n"
        )
        s["assignment"] = replace(
            s["assignment"], acceptance_argv=(sys.executable, str(stub))
        )
        s["deps"].gate = InstalledLauncherGate(Path("/unused"), tmp_path / "receipts")
    elif channel == "verify":
        from factory_orchestration.state import VerifyOutcome

        s["gate"].verify = lambda **kw: VerifyOutcome(False, "test_failed", secret)
    else:
        log = s["artifacts"] / "acceptance.log"
        log.write_text("ERROR: " + secret)
        s["gate"].run_acceptance = lambda **kw: GateOutcome(
            "fail",
            reason="test_failed",
            detail=secret if channel == "detail" else "failed",
            log_reference=str(log) if channel == "log" else "",
        )
    result = run(s)
    data = context(requests[1])
    field = (
        "review_comment"
        if channel == "review"
        else ("gate_log_excerpt" if channel in {"log", "installed"} else "detail")
    )
    assert "[REDACTED:FIXTURE_AUTH]" in data[field]
    persisted = json.dumps(
        {
            "state": result.state,
            "operations": s["deps"].store.operations(thread_id(s["assignment"])),
        }
    )
    assert secret not in persisted
    assert all(secret not in request.prompt for request in requests)
    if channel in {"detail", "review"}:
        stored = receipt(s, "review" if channel == "review" else "gate")
        assert "[REDACTED:FIXTURE_AUTH]" in json.dumps(stored)


# Assemble token shapes at runtime; no credential-shaped literals are tracked.
SHAPES = [
    ("openai", lambda: "sk" + "-" + "a" * 24),
    *[("github", lambda kind=kind: "gh" + kind + "_" + "a" * 32) for kind in "pousr"],
    ("github", lambda: "github" + "_pat_" + "a" * 32),
    ("aws", lambda: "AK" + "IA" + "A" * 16),
    ("google", lambda: "AI" + "za" + "a" * 35),
    *[
        ("slack", lambda kind=kind: "xo" + "x" + kind + "-" + "a" * 12)
        for kind in "abprs"
    ],
    ("bearer", lambda: "BeArEr " + "a" * 24),
    ("jwt", lambda: "ey" + "J" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10),
    (
        "pem",
        lambda: (
            "-----BEGIN "
            + "RSA PRIVATE KEY-----\nfixture\n-----END "
            + "RSA PRIVATE KEY-----"
        ),
    ),
    ("pem", lambda: "-----BEGIN " + "PRIVATE KEY-----\ncut fixture"),
]


@pytest.mark.parametrize("label,make", SHAPES)
def test_fixed_secret_shape(label, make):
    from factory_orchestration.redaction import redact_diagnostic

    secret = make()
    redacted = redact_diagnostic("before " + secret + " after")
    assert secret not in redacted
    assert "[REDACTED:" + label + "]" in redacted
    if label == "bearer":
        assert "BeArEr [REDACTED:bearer]" in redacted


@pytest.mark.parametrize(
    "name",
    [
        "fixture_key",
        "FIXTURE_TOKEN",
        "FIXTURE_SECRET",
        "FIXTURE_PASSWORD",
        "FIXTURE_PASSWD",
        "FIXTURE_CREDENTIAL",
        "FIXTURE_AUTH",
    ],
)
def test_env_name_contract(monkeypatch, name):
    from factory_orchestration.redaction import redact_diagnostic

    secret = "fixture-" + "value-123"
    monkeypatch.setenv(name, secret)
    assert redact_diagnostic(secret) == "[REDACTED:" + name + "]"


def test_provider_name_sets_are_included(monkeypatch):
    from factory_orchestration.redaction import redact_diagnostic

    from factory_orchestration.adapters import hermes

    monkeypatch.setattr(hermes, "PROVIDER_ENV", frozenset({"FIXTURE_PROVIDER"}))
    monkeypatch.setitem(hermes.REVIEW_PROVIDER_KEYS, "fixture", ("FIXTURE_REVIEW",))
    for name in ("FIXTURE_PROVIDER", "FIXTURE_REVIEW"):
        monkeypatch.setenv(name, "provider-" + name)
        assert redact_diagnostic("provider-" + name) == "[REDACTED:" + name + "]"


def test_env_overlap_longest_first_and_scope(monkeypatch):
    from factory_orchestration.redaction import redact_diagnostic

    short = "fixture-" + "1234"
    monkeypatch.setenv("FIXTURE_KEY", short)
    monkeypatch.setenv("FIXTURE_TOKEN", short + "-suffix")
    monkeypatch.setenv("FIXTURE_PASSWORD", "tiny")
    monkeypatch.setenv("FIXTURE_PUBLIC", "unmatched-name-value")
    assert redact_diagnostic(short + "-suffix " + short) == (
        "[REDACTED:FIXTURE_TOKEN] [REDACTED:FIXTURE_KEY]"
    )
    assert redact_diagnostic("tiny unmatched-name-value") == "tiny unmatched-name-value"


@pytest.mark.parametrize("field", ["detail", "gate_log_excerpt", "review_comment"])
def test_redact_before_truncation_and_prompt_rendering(monkeypatch, field):
    secret = "fixture-" + "s" * 6000
    monkeypatch.setenv("FIXTURE_SECRET", secret)
    prompt = _implementation_prompt(
        {**PROMPT_STATE, "repair_context": {field: "before " + secret + " after"}}, 1
    )
    data = json.loads(prompt.split(HEADER)[1])
    assert data[field] == "before [REDACTED:FIXTURE_SECRET] after"


def test_log_redaction_precedes_tail_cut(tmp_path, monkeypatch):
    from factory_orchestration.graph import _gate_log_tail

    secret = "fixture-" + "s" * 6000
    monkeypatch.setenv("FIXTURE_SECRET", secret)
    log = tmp_path / "acceptance.log"
    log.write_text("before " + secret + " after")
    assert (
        _gate_log_tail(str(tmp_path), str(log))
        == "before [REDACTED:FIXTURE_SECRET] after"
    )


def test_signature_uses_redacted_text(monkeypatch):
    monkeypatch.setenv("FIXTURE_TOKEN", "synthetic-" + "first-value")
    first = _failure_signature({"detail": "ERROR: synthetic-" + "first-value"})
    monkeypatch.setenv("FIXTURE_TOKEN", "synthetic-" + "second-value")
    second = _failure_signature({"detail": "ERROR: synthetic-" + "second-value"})
    assert first == second


@pytest.mark.parametrize("prefix", ["FAILED ", "ERROR "])
@pytest.mark.parametrize(
    "parameters", [("create one", "create two"), ("deadbeef", "cafebabe")]
)
def test_distinct_complete_pytest_ids_spend_repairs(tmp_path, prefix, parameters):
    s = seed(tmp_path)
    requests, _ = candidate_sequence(s)
    log = s["artifacts"] / "acceptance.log"
    calls = []

    def acceptance(**kw):
        param = parameters[min(len(calls), 1)]
        calls.append(kw)
        log.write_text(prefix + f"tests/test_api.py::test_case[{param}] - broken\n")
        return GateOutcome("fail", reason="test_failed", detail=str(log))

    s["gate"].run_acceptance = acceptance
    result = run(s)
    assert result.state["repair_count"] == 2
    assert len(requests) == 3


@pytest.mark.parametrize(
    "node",
    [
        "test_case[create one]",
        "test_case[deadbeef123456]",
        "test_case[2026-11-14T10:00:00Z]",
        "test_case[0.12s]",
    ],
)
def test_pytest_node_identity_verbatim(node):
    base = {"source": "acceptance", "reason_code": "evidence_rejected"}
    import hashlib

    identity = "tests/test_api.py::" + node
    expected = hashlib.sha256(
        ("acceptance | evidence_rejected | " + identity).encode()
    ).hexdigest()
    assert (
        _failure_signature({**base, "gate_log_excerpt": "noise\nFAILED " + identity})
        == expected
    )


def test_fallback_normalizes_only_volatile_diagnostics():
    assert _failure_signature(
        {"detail": "ERROR: at 2026-11-14T10:00:00Z in 3.4s 0xabc abcdef123456"}
    ) == _failure_signature(
        {"detail": "ERROR: at 2026-11-15T10:01:00Z in 0.12s 0xdef 123456abcdef"}
    )
    assert _failure_signature({"detail": "ERROR: deadbeef"}) != _failure_signature(
        {"detail": "ERROR: cafebabe"}
    )
