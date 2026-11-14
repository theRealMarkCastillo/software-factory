"""Public launcher and packaged attestation provider, entirely offline."""

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from factory_orchestration.adapters import installed, reviewer_sandbox
from factory_orchestration.adapters.gates import InstalledLauncherGate
from factory_orchestration.errors import OrchestrationError
from tests.test_verify import _evidence, _repo

COMMANDS = {
    "run": "factory.runner",
    "verify": "factory.verify",
    "promote": "factory.promote",
    "audit": "factory.audit",
    "coverage": "factory.coverage",
    "datamodel": "factory.datamodel",
    "learning": "factory.learning",
    "orchestrate": "factory_orchestration.cli",
}


def test_console_scripts_preserved():
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    assert project["scripts"]["factory"] == "factory.cli:main"
    for command, module in COMMANDS.items():
        assert project["scripts"][f"factory-{command}"] == f"{module}:main"


@pytest.mark.parametrize("command,module", COMMANDS.items())
def test_dispatch_preserves_arguments_and_exit(monkeypatch, command, module):
    cli = importlib.import_module("factory.cli")
    calls = []

    def load(name):
        calls.append(name)
        return SimpleNamespace(main=lambda argv: (calls.append(argv), 17)[1])

    monkeypatch.setattr(cli.importlib, "import_module", load)
    assert cli.main([command, "--help", "--unknown", "two words"]) == 17
    assert calls == [module, ["--help", "--unknown", "two words"]]


@pytest.mark.parametrize("argv", [[], ["unknown"], ["--bad"]])
def test_invalid_command_usage(argv, capsys):
    cli = importlib.import_module("factory.cli")
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert captured.out == ""


def test_optional_orchestration_missing(monkeypatch, capsys):
    cli = importlib.import_module("factory.cli")

    def missing(name):
        raise ModuleNotFoundError("No module named 'langgraph'", name="langgraph")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["orchestrate", "--help"]) == 2
    error = capsys.readouterr().err
    assert "hermes-software-factory[orchestration]" in error
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error


def test_unrelated_import_error_not_hidden(monkeypatch):
    cli = importlib.import_module("factory.cli")

    def missing(name):
        raise ModuleNotFoundError("internal bug", name="unrelated_internal_module")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["orchestrate"])


@pytest.fixture
def public_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_LAUNCHER", raising=False)
    monkeypatch.setenv("FACTORY_OPS_ROOT", str(tmp_path / "absent-ops"))


def test_public_launcher_beside_interpreter(public_environment):
    assert installed.default_launcher() == Path(sys.executable).parent / "factory"


def test_private_launcher_precedes_public(public_environment, tmp_path, monkeypatch):
    root = tmp_path / "ops"
    launcher = root / "bin" / "factory"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("private installation")
    monkeypatch.setenv("FACTORY_OPS_ROOT", str(root))
    assert installed.default_launcher() == launcher


def test_explicit_launcher_precedes_private(public_environment, tmp_path, monkeypatch):
    launcher = tmp_path / "explicit"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    monkeypatch.setenv("FACTORY_LAUNCHER", str(launcher))
    assert installed.default_launcher() == launcher


@pytest.mark.parametrize(
    "kind", ["relative", "absent", "directory", "not-executable", "empty"]
)
def test_invalid_explicit_launcher_fails_closed(
    public_environment, tmp_path, monkeypatch, kind
):
    launcher = tmp_path / kind
    if kind == "directory":
        launcher.mkdir()
    elif kind == "not-executable":
        launcher.write_text("not executable")
        launcher.chmod(0o600)
    value = "relative/path" if kind == "relative" else str(launcher)
    monkeypatch.setenv("FACTORY_LAUNCHER", "" if kind == "empty" else value)
    with pytest.raises(ValueError, match="FACTORY_LAUNCHER"):
        installed.default_launcher()


def test_real_installed_verify(public_environment, tmp_path):
    launcher = installed.default_launcher()
    help_result = subprocess.run(
        [str(launcher), "verify", "--help"], capture_output=True, text=True, timeout=30
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--artifacts-dir" in help_result.stdout
    repo, base, candidate = _repo(tmp_path)
    artifacts = _evidence(tmp_path, repo, base, candidate)
    gate = InstalledLauncherGate(launcher, tmp_path / "receipts", timeout_s=30)
    outcome = gate.verify(
        artifacts=str(artifacts),
        repository=str(repo),
        candidate_sha=candidate,
        base_sha=base,
    )
    assert outcome.certified, outcome
    # Real verifier must still refuse tampering, not just route a help command.
    (artifacts / "pytest.txt").write_text("tampered")
    assert not gate.verify(
        artifacts=str(artifacts),
        repository=str(repo),
        candidate_sha=candidate,
        base_sha=base,
    ).certified


def test_vendored_provider_selected_without_checkout(monkeypatch):
    monkeypatch.delenv("FACTORY_HERMES_REPO", raising=False)
    server, text, tool = reviewer_sandbox._load_fake_provider()
    assert server.__module__ == "factory_orchestration.adapters.loopback_provider"
    assert text.__module__ == tool.__module__ == server.__module__
    assert not hasattr(reviewer_sandbox, "HERMES_REPO")


def test_explicit_missing_checkout_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_HERMES_REPO", str(tmp_path / "missing"))
    with pytest.raises(OrchestrationError, match="loopback fake provider not found"):
        reviewer_sandbox._load_fake_provider()


def test_explicit_checkout_provider(monkeypatch, tmp_path):
    fake = tmp_path / "tests" / "fakes" / "fake_llm_provider.py"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "class FakeLLMServer: pass\nclass Text: pass\nclass ToolCall: pass\n"
    )
    monkeypatch.setenv("FACTORY_HERMES_REPO", str(tmp_path))
    server, text, tool = reviewer_sandbox._load_fake_provider()
    assert [cls.__name__ for cls in (server, text, tool)] == [
        "FakeLLMServer",
        "Text",
        "ToolCall",
    ]
    assert server.__module__ != "factory_orchestration.adapters.loopback_provider"


def _post(server, body, path="/chat/completions"):
    request = Request(
        server.base_url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urlopen(request, timeout=5)


@pytest.mark.parametrize("stream", [False, True])
def test_loopback_script_and_recording(monkeypatch, stream):
    monkeypatch.delenv("FACTORY_HERMES_REPO", raising=False)
    Server, Text, ToolCall = reviewer_sandbox._load_fake_provider()
    script = [ToolCall("read_file", {"path": "/evidence/readme"}), Text("done")]
    with Server(script, default_text="finished") as server:
        assert server.base_url.startswith("http://127.0.0.1:")
        body = {"model": "fake-model", "messages": [], "stream": stream}
        with _post(server, body) as response:
            raw = response.read().decode()
            if stream:
                assert response.headers["Content-Type"].startswith("text/event-stream")
                assert raw.endswith("data: [DONE]\n\n")
                chunks = [
                    json.loads(line[6:])
                    for line in raw.splitlines()
                    if line.startswith("data: {")
                ]
                assert chunks[0]["object"] == "chat.completion.chunk"
                call = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
                assert call["index"] == 0
                assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
            else:
                result = json.loads(raw)
                assert result["object"] == "chat.completion"
                assert result["choices"][0]["finish_reason"] == "tool_calls"
                call = result["choices"][0]["message"]["tool_calls"][0]
            assert call["id"]
            assert call["function"]["name"] == "read_file"
            assert json.loads(call["function"]["arguments"]) == {
                "path": "/evidence/readme"
            }
        with _post(server, {**body, "stream": False}) as response:
            assert json.load(response)["choices"][0]["message"]["content"] == "done"
        with _post(server, {**body, "stream": False}) as response:
            assert json.load(response)["choices"][0]["message"]["content"] == "finished"
        assert len(server.requests) == 3
        assert server.requests[0]["kind"] == "main"
        assert server.requests[0]["body"] == body
        assert server.drop_unconsumed() == 0
    assert len(script) == 2  # the caller's script is not mutated


def test_unconsumed_script_and_unknown_path(monkeypatch):
    monkeypatch.delenv("FACTORY_HERMES_REPO", raising=False)
    Server, Text, _ = reviewer_sandbox._load_fake_provider()
    with Server([Text("a"), Text("b")]) as server:
        with pytest.raises(HTTPError) as error:
            _post(server, {}, "/unknown")
        assert error.value.code == 404
        assert server.drop_unconsumed() == 2
        assert server.drop_unconsumed() == 0
