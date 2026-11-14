"""Offline regressions through the shipped script entry points."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_orchestration.runner import _validate_spec_visibility

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BoundaryReached(BaseException):
    """Stop before any external effect, after exercising the real interface."""


def test_pilot_entrypoint_passes_real_spec_admission(tmp_path, monkeypatch):
    module = load_script("langgraph_rollout_pilot")
    launcher = tmp_path / "bin" / "factory"
    root = tmp_path / "pilot"
    real_sh = module.Pilot.sh

    def offline_sh(self, argv, **kwargs):
        if argv[0] != str(launcher):
            return real_sh(self, argv, **kwargs)
        if "admission" in argv:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        assert "run" in argv
        payload = json.loads(self.assignment.read_text())
        _validate_spec_visibility(SimpleNamespace(**payload))
        spec = Path(payload["spec_reference"])
        relative = spec.relative_to(self.workspace)
        assert relative.as_posix() == "spec.md"
        assert self.git(
            "show", f"{payload['immutable_base_sha']}:spec.md", cwd=self.repo
        )
        assert spec.read_text() == module.SPEC_TEXT
        assert self.git("status", "--porcelain", cwd=self.workspace) == ""
        raise BoundaryReached

    monkeypatch.setattr(module.Pilot, "sh", offline_sh)
    with pytest.raises(BoundaryReached):
        module.main(
            [
                "--execute",
                "--run-root",
                str(root),
                "--launcher",
                str(launcher),
                "--card",
                "t_deadbeef",
                "--attempt-run-id",
                "7",
                "--attempt-profile",
                "factory-runner",
                "--claim-lock",
                "host:1",
            ]
        )


def test_live_review_entrypoint_binds_real_submit_before_attestation(
    tmp_path, monkeypatch
):
    module = load_script("live_review_fixture")
    monkeypatch.setattr(
        module,
        "read_profile_model_config",
        lambda _: {
            "default": "offline-model",
            "base_url": "http://127.0.0.1:1/v1",
            "provider": "openai",
        },
    )
    monkeypatch.setattr(
        module,
        "read_profile_provider_env",
        lambda _: {
            "OPENAI_API_KEY": "offline-placeholder",
        },
    )
    monkeypatch.setattr(module, "cleanup_containers", lambda: None)
    real_submit = module.HermesReviewer.submit
    observed = {}

    def submit(self, **kwargs):
        observed.update(kwargs)
        return real_submit(self, **kwargs)

    def attest(self, *, reviewer, candidate, evidence):
        assert observed["spec_path"] == "/evidence/evidence/spec.md"
        assert (Path(evidence) / "spec.md").is_file()
        assert observed["changed_files"] == ("module.py",)
        assert observed["changed_files_truncated"] == 0
        assert (Path(candidate) / "module.py").is_file()
        raise BoundaryReached

    monkeypatch.setattr(module.HermesReviewer, "submit", submit)
    monkeypatch.setattr(module.SandboxedReadonlyAttestor, "attest", attest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_review_fixture.py",
            "--run",
            "--scratch",
            str(tmp_path / "scratch"),
            "--out",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(BoundaryReached):
        module.main()
