"""Port leases must survive the entire deployment, not only allocation."""

import os
import time

import pytest

from factory import policy


def test_live_port_lock_cannot_be_stolen_after_wait_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_STATE_DIR", str(tmp_path))
    with policy._acquire_port_lock():
        lock = tmp_path / "ports" / ".lock"
        os.utime(lock, (time.time() - 500, time.time() - 500))
        now = time.time()
        ticks = iter([now, now + 61])
        monkeypatch.setattr(time, "time", lambda: next(ticks, now + 61))
        with pytest.raises(policy.PolicyViolation, match="port lock held"):
            with policy._acquire_port_lock():
                pytest.fail("stole a live deployment's lease")
        assert lock.exists()
    assert not lock.exists()


def test_allocator_unique_avoids_reserved_and_bound(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(policy, "reserved_host_ports", lambda: {49000})

    def probe(argv, **kwargs):
        import subprocess

        if argv[0] == "lsof":
            occupied = argv[-1] == ":49001"
            return subprocess.CompletedProcess(
                argv, 0 if occupied else 1, "listener" if occupied else "", ""
            )
        return subprocess.CompletedProcess(argv, 0, "127.0.0.1:49002->80/tcp", "")

    monkeypatch.setattr(policy.subprocess, "run", probe)
    assert policy.allocate_host_ports(3) == [49003, 49004, 49005]
    assert not (tmp_path / "ports" / ".lock").exists()
