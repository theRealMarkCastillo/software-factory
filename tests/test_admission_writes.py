"""Admission CLI writes cooperate across processes without losing safety state."""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from factory_orchestration.cli import main

ROOT = Path(__file__).resolve().parents[1]

# Pause immediately after reading the policy, while the writer must hold its lock.
SLOW_WRITER = """
import sys, time
from pathlib import Path
from factory_orchestration.cli import main
state, ready, release = map(Path, sys.argv[1:4])
original = Path.read_text
def slow_read(path, *args, **kwargs):
    text = original(path, *args, **kwargs)
    if path == state / 'admission.json':
        ready.touch()
        deadline = time.monotonic() + 10
        while not release.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('test release timed out')
            time.sleep(0.01)
    return text
Path.read_text = slow_read
raise SystemExit(main(['--state-dir', str(state), 'admission', '--enroll', 'slow']))
"""


def _env(tmp_path):
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes"),
        "PYTHONPATH": str(ROOT),
    }


def _wait_for(path, process):
    import time

    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, process.communicate()
        assert time.monotonic() < deadline, "writer never reached its policy read"
        time.sleep(0.01)


@pytest.mark.parametrize("change", [["--enroll", "other"], ["--disable"]])
def test_slow_writer_preserves_concurrent_change(tmp_path, change):
    state = tmp_path / "state"
    state.mkdir()
    path = state / "admission.json"
    path.write_text(json.dumps({"enabled": True, "enrolled_cards": ["original"]}))
    ready, release = tmp_path / "ready", tmp_path / "release"
    helper = tmp_path / "slow_writer.py"
    helper.write_text(SLOW_WRITER)
    slow = subprocess.Popen(
        [sys.executable, str(helper), str(state), str(ready), str(release)],
        env=_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    other = None
    try:
        _wait_for(ready, slow)
        other = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "factory_orchestration.cli",
                "--state-dir",
                str(state),
                "admission",
                *change,
            ],
            env=_env(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # An unlocked writer finishes here; a cooperating writer waits for release.
        try:
            other.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        release.touch()
        slow_output = slow.communicate(timeout=10)
        other_output = other.communicate(timeout=10)
        assert slow.returncode == 0, slow_output
        assert other.returncode == 0, other_output
        payload = json.loads(path.read_text())
        assert payload["enabled"] is (change != ["--disable"])
        expected = {"original", "slow"}
        if change[0] == "--enroll":
            expected.add("other")
        assert set(payload["enrolled_cards"]) == expected
    finally:
        release.touch()
        for process in (slow, other):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


def _call(state, *args):
    return main(["--state-dir", str(state), "admission", *args])


def test_unknown_keys_and_unrequested_fields_are_preserved(tmp_path):
    path = tmp_path / "admission.json"
    payload = {"enabled": True, "enrolled_cards": ["keep"], "future": {"v": [1, 2]}}
    path.write_text(json.dumps(payload))
    assert _call(tmp_path, "--enroll", "new") == 0
    assert json.loads(path.read_text()) == {
        **payload,
        "enrolled_cards": ["keep", "new"],
    }
    assert _call(tmp_path, "--disable") == 0
    assert json.loads(path.read_text())["future"] == payload["future"]


def test_unenroll_removes_only_named_cards(tmp_path):
    path = tmp_path / "admission.json"
    path.write_text(json.dumps({"enabled": True, "enrolled_cards": ["a", "b", "c"]}))
    assert _call(tmp_path, "--unenroll", "a", "--unenroll", "c", "--enroll", "d") == 0
    assert json.loads(path.read_text()) == {
        "enabled": True,
        "enrolled_cards": ["b", "d"],
    }


@pytest.mark.parametrize("args", [[], ["--unenroll", "absent"], ["--enroll", "keep"]])
def test_no_change_does_not_replace_policy(tmp_path, args):
    path = tmp_path / "admission.json"
    text = '{"enabled": true, "enrolled_cards": ["keep"]}\n'
    path.write_text(text)
    before = path.stat()
    assert _call(tmp_path, *args) == 0
    assert path.read_text() == text
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_conflicting_enrollment_refuses_without_write(tmp_path):
    path = tmp_path / "admission.json"
    path.write_text('{"enabled": true, "enrolled_cards": ["keep"]}')
    before = path.read_bytes()
    assert _call(tmp_path, "--enroll", "keep", "--unenroll", "keep") != 0
    assert path.read_bytes() == before


def test_lock_timeout_refuses_without_write(tmp_path, capsys):
    path = tmp_path / "admission.json"
    path.write_text('{"enabled": false, "enrolled_cards": []}')
    before = path.read_bytes()
    with (tmp_path / "admission.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert _call(tmp_path, "--enable", "--lock-timeout", "0.05") != 0
    assert "timed out" in capsys.readouterr().err
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "text", ["{", "[]", '{"enrolled_cards": "bad"}', '{"enrolled_cards": [1]}']
)
def test_malformed_policy_is_not_overwritten(tmp_path, text):
    path = tmp_path / "admission.json"
    path.write_text(text)
    assert _call(tmp_path, "--enable") != 0
    assert path.read_text() == text


def test_missing_policy_defaults_to_disabled(tmp_path, capsys):
    assert _call(tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "enabled": False,
        "enrolled_cards": [],
    }
    assert _call(tmp_path, "--enroll", "new") == 0
    assert json.loads((tmp_path / "admission.json").read_text()) == {
        "enabled": False,
        "enrolled_cards": ["new"],
    }
