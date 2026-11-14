"""Rule interactions at the repair diagnostic boundary (synthetic values only)."""

import json
import re
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


def test_overlapping_env_values_are_all_removed(monkeypatch):
    monkeypatch.setenv("FIXTURE_KEY", "abcdefgh")
    monkeypatch.setenv("FIXTURE_TOKEN", "defghijk")
    out = redact_diagnostic("left abcdefghijk right")
    assert out.startswith("left [REDACTED:") and out.endswith("] right")
    assert out.count("[REDACTED:") == 1


def test_self_overlapping_env_value_is_fully_removed(monkeypatch):
    monkeypatch.setenv("FIXTURE_KEY", "abababab")
    assert redact_diagnostic("x ababababab y").count("ab") == 0


def test_overlapping_env_values_never_reach_a_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_KEY", "abcdefgh")
    monkeypatch.setenv("FIXTURE_TOKEN", "defghijk")
    s = seed(tmp_path)
    s["gate"].run_acceptance = lambda **kw: GateOutcome(
        "unavailable", reason="gate_unavailable", detail="left abcdefghijk right"
    )
    result = run(s)
    assert result.state["status"] == "held"
    assert "ijk" not in result.state["last_reason_detail"]


@pytest.mark.parametrize("field", ["detail", "review"])
def test_overlapping_env_values_never_reach_prompt_or_store(
    tmp_path, monkeypatch, field
):
    monkeypatch.setenv("FIXTURE_KEY", "abcdefgh")
    monkeypatch.setenv("FIXTURE_TOKEN", "defghijk")
    s = seed(tmp_path)
    requests = capture(s)
    text = "left abcdefghijk right"
    if field == "review":
        original = s["reviewer"].submit

        def submit(**kw):
            s["reviewer"].outcome = "changes_required"
            handle, review = original(**kw)
            return handle, replace(review, comment=text)

        s["reviewer"].submit = submit
    else:
        s["gate"].run_acceptance = lambda **kw: GateOutcome(
            "fail", reason="test_failed", detail=text
        )
    result = run(s)
    persisted = json.dumps(
        {
            "state": result.state,
            "operations": s["deps"].store.operations(thread_id(s["assignment"])),
        }
    )
    assert len(requests) >= 2
    assert "ijk right" not in persisted.replace("] right", "")
    assert all("ghijk" not in request.prompt for request in requests)


def _occurrences(text, value):
    start = text.find(value)
    while start != -1:
        yield start, start + len(value)
        start = text.find(value, start + 1)


def _property_holds(seed_value, cut_window, setenv):
    """Seeded span-union property. Returns only a static failure label or ""

    so that a failure can never print text, environment values or matches.
    The oracle is a per-character mask built by trying every rule at EVERY
    start offset of the original text; it shares no scanning code with the
    implementation.
    """
    import os
    import random

    from factory_orchestration import redaction

    rng = random.Random(seed_value)
    alphabet = "ab-K "
    kinds = ["", "RSA ", "EC "]
    pieces = [
        *["-----BEGIN " + kind + "PRIVATE " + "KEY-----\n" for kind in kinds],
        *["-----END " + kind + "PRIVATE " + "KEY-----\n" for kind in kinds],
        "PRIVATE " + "KEY-----",
        "s" + "k-" + "a" * 22,
        "Bearer " + "b" * 22,
        "AK" + "IA" + "B" * 16,
        "\n",
    ]
    for index in range(rng.randint(1, 4)):
        source = rng.choice(pieces + ["".join(rng.choices(alphabet, k=12))])
        start = rng.randrange(0, max(1, len(source) - 8))
        value = source[start : start + rng.randint(8, 12)]
        if len(value) >= 8:
            setenv(f"FIXTURE_{index}_TOKEN", value)
    text = "".join(
        rng.choice(pieces + ["".join(rng.choices(alphabet, k=rng.randint(1, 14)))])
        for _ in range(rng.randint(3, 16))
    )
    mask = [False] * len(text)
    for name, value in os.environ.items():
        if len(value) >= 8 and redaction._ENV_NAME.search(name):
            for begin, end in _occurrences(text, value):
                mask[begin:end] = [True] * (end - begin)
    pem_mask = [False] * len(text)
    for label, pattern in redaction._SHAPES:
        for offset in range(len(text)):
            match = pattern.match(text, offset)
            if match:
                begin = match.end(1) if label == "bearer" else match.start()
                mask[begin : match.end()] = [True] * (match.end() - begin)
                if label == "pem":
                    pem_mask[begin : match.end()] = [True] * (match.end() - begin)
    if cut_window:
        orphans = [
            match.end()
            for match in re.finditer(
                r"^-----END [A-Z ]*PRIVATE KEY-----\r?$", text, re.M
            )
            if not pem_mask[match.start()]
        ]
        if orphans:
            mask[: max(orphans)] = [True] * max(orphans)

    spans = redaction._redaction_spans(text, cut_window=cut_window)
    if spans != sorted(spans) or any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        return "spans are not sorted and disjoint"
    covered = [False] * len(text)
    for begin, end, _ in spans:
        covered[begin:end] = [True] * (end - begin)
    if covered != mask:
        return "span union differs from the independent character mask"
    expected, cursor = [], 0
    for begin, end, label in spans:
        expected += [text[cursor:begin], f"[REDACTED:{label}]"]
        cursor = end
    expected.append(text[cursor:])
    if redaction.redact_diagnostic(text, cut_window=cut_window) != "".join(expected):
        return "rendered output is not the original with exactly the spans replaced"
    return ""


@pytest.mark.parametrize("seed_value", range(60))
@pytest.mark.parametrize("cut_window", [False, True])
def test_every_original_match_is_covered_and_the_rest_is_unchanged(
    monkeypatch, seed_value, cut_window
):
    problem = _property_holds(seed_value, cut_window, monkeypatch.setenv)
    if problem:
        # Static label only: never the text, a value or a match object.
        pytest.fail(problem, pytrace=False)


def _nested(kind_outer="RSA ", kind_inner="", close_inner=False):
    lines = [
        "-----BEGIN " + kind_outer + "PRIVATE " + "KEY-----",
        "-----BEGIN " + kind_inner + "PRIVATE " + "KEY-----",
        BODY,
        "-----END " + kind_outer + "PRIVATE " + "KEY-----",
        "nested-" + "tail-canary",
    ]
    if close_inner:
        lines.append("-----END " + kind_inner + "PRIVATE " + "KEY-----")
        lines.append("after stays")
    return "\n".join(lines)


@pytest.mark.parametrize("cut_window", [False, True])
@pytest.mark.parametrize("close_inner", [False, True])
def test_nested_and_crossing_pem_blocks_are_fully_removed(cut_window, close_inner):
    out = redact_diagnostic(_nested(close_inner=close_inner), cut_window=cut_window)
    assert "tail-canary" not in out and BODY not in out
    assert out.endswith("after stays") == close_inner


def test_nested_pem_never_reaches_a_hold(tmp_path):
    s = seed(tmp_path)
    s["gate"].run_acceptance = lambda **kw: GateOutcome(
        "unavailable", reason="gate_unavailable", detail=_nested()
    )
    result = run(s)
    assert result.state["status"] == "held"
    assert "tail-canary" not in json.dumps(result.state)


@pytest.mark.parametrize("channel", ["detail", "log", "review"])
def test_nested_pem_never_reaches_prompt_or_store(tmp_path, channel):
    s = seed(tmp_path)
    requests = capture(s)
    if channel == "review":
        original = s["reviewer"].submit

        def submit(**kw):
            s["reviewer"].outcome = "changes_required"
            handle, review = original(**kw)
            return handle, replace(review, comment=_nested())

        s["reviewer"].submit = submit
    else:
        log = s["artifacts"] / "acceptance.log"
        log.write_text("ERROR: boom\n" + _nested() + "\n")
        s["gate"].run_acceptance = lambda **kw: GateOutcome(
            "fail",
            reason="test_failed",
            detail=_nested() if channel == "detail" else "failed",
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
    assert "tail-canary" not in persisted
    assert all("tail-canary" not in request.prompt for request in requests)


def test_a_failing_property_never_prints_an_environment_value(tmp_path):
    """Break rendering on purpose in a child pytest; the value must not appear."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    sentinel = "b" * 22
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n"
        "from factory_orchestration import redaction\n"
        "import test_repair_context_interactions as subject\n"
        "def test_probe(monkeypatch):\n"
        "    monkeypatch.setattr(redaction, 'redact_diagnostic', lambda t, **k: t)\n"
        "    failures = [\n"
        "        subject._property_holds(seed, False, monkeypatch.setenv)\n"
        "        for seed in range(60)\n"
        "    ]\n"
        "    assert any(failures), 'mutation was not detected'\n"
        "    pytest.fail(next(f for f in failures if f), pytrace=False)\n"
    )
    tests_dir = Path(__file__).resolve().parent
    env = dict(os.environ, FIXTURE_PROBE_TOKEN=sentinel)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tests_dir), str(tests_dir.parent), env.get("PYTHONPATH", "")]
    )
    xml = tmp_path / "junit.xml"
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            f"--junitxml={xml}",
            str(probe),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    assert child.returncode == 1
    assert "rendered output is not the original" in child.stdout
    for output in (child.stdout, child.stderr, xml.read_text()):
        assert sentinel not in output
