"""Owner amendments: Compose identities and bounded large-log windows."""

import hashlib
import os

import pytest

from factory_orchestration.graph import _failure_signature, _gate_log_tail


@pytest.mark.parametrize(
    "prefix",
    [
        "api-test-1  | ",
        "api-test-1  | 2026-11-14T10:00:01Z ",
        "2026-11-14T10:00:01.123+00:00 ",
    ],
)
@pytest.mark.parametrize("status", ["FAILED ", "ERROR "])
def test_compose_and_timestamp_prefixes_preserve_complete_node_id(prefix, status):
    node = "tests/test_x.py::test_y[a b]"
    assert (
        _failure_signature({"gate_log_excerpt": prefix + status + node + " - boom"})
        == hashlib.sha256((" |  | " + node).encode()).hexdigest()
    )


def test_log_read_seeks_and_reads_no_more_than_last_64_kib(tmp_path, monkeypatch):
    log = tmp_path / "acceptance.log"
    log.write_bytes(b"x" * (2 * 1024 * 1024) + b"\nERROR: useful tail\n")
    original = os.read
    reads = []

    def read(fd, amount):
        reads.append((os.lseek(fd, 0, os.SEEK_CUR), amount))
        return original(fd, amount)

    monkeypatch.setattr(os, "read", read)
    tail = _gate_log_tail(str(tmp_path), str(log))
    assert "ERROR: useful tail" in tail
    assert sum(amount for offset, amount in reads) <= 65536
    assert all(offset >= log.stat().st_size - 65536 for offset, amount in reads)


def test_partial_window_line_is_dropped(tmp_path):
    log = tmp_path / "acceptance.log"
    log.write_text("untrusted-partial" * 6000 + "\nERROR: final line\n")
    tail = _gate_log_tail(str(tmp_path), str(log))
    assert tail == "[truncated]ERROR: final line\n"


@pytest.mark.parametrize("body_lines", [10, 6000])
def test_orphan_pem_end_redacts_window_prefix(tmp_path, body_lines):
    log = tmp_path / "acceptance.log"
    log.write_text(
        ("-----BEGIN " + "RSA PRIVATE KEY-----\n" if body_lines > 10 else "")
        + "fixture-body\n" * body_lines
        + "-----END "
        + "RSA PRIVATE KEY-----\nERROR: final line\n"
    )
    tail = _gate_log_tail(str(tmp_path), str(log))
    assert "fixture-body" not in tail
    assert "[REDACTED:pem]" in tail
    assert tail.endswith("ERROR: final line\n")


def test_tail_truncation_prefers_complete_lines(tmp_path):
    log = tmp_path / "acceptance.log"
    log.write_text("prefix\n" + "x" * 5000 + "\nERROR: final line\n")
    assert _gate_log_tail(str(tmp_path), str(log)) == "[truncated]ERROR: final line\n"
