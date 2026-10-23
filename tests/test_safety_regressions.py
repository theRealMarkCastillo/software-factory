"""Exercise policy bypasses at the runner boundary without accessing Docker."""

from unittest.mock import Mock

import pytest
import yaml

from factory import runner
from factory.policy import PolicyViolation


@pytest.mark.parametrize(
    "extra",
    [
        {"include": ["../unchecked.yml"]},
        {"secrets": {"token": {"file": "/etc/passwd"}}},
        {"configs": {"token": {"file": "../outside"}}},
        {"secrets": {"token": {"external": True}}},
    ],
)
def test_runner_rejects_unsafe_top_level_sections(tmp_path, monkeypatch, extra):
    doc = {"services": {"test": {"image": "alpine"}}, **extra}
    _assert_rejected(tmp_path, monkeypatch, [doc])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volumes", ["/var/run/docker.sock:/socket"]),
        ("cap_add", ["SYS_ADMIN"]),
        ("ports", ["5432:5432"]),
        ("devices", ["/dev/sda:/dev/sda"]),
    ],
)
def test_runner_override_cannot_hide_unsafe_lists(tmp_path, monkeypatch, field, value):
    _assert_rejected(
        tmp_path,
        monkeypatch,
        [
            {"services": {"test": {"image": "alpine", field: value}}},
            {"services": {"test": {field: []}}},
        ],
    )


@pytest.mark.parametrize(
    "service",
    [
        {"volumes": ["${SOURCE}:/host"]},
        {"volumes": [{"type": "bind", "source": "$SOURCE", "target": "/host"}]},
        {"env_file": ["${ENV_PATH}"]},
        {"build": {"context": ".", "dockerfile": "${DOCKERFILE}"}},
        {"security_opt": ["${CONFINEMENT}"]},
    ],
)
def test_runner_rejects_interpolated_security_inputs(tmp_path, monkeypatch, service):
    _assert_rejected(
        tmp_path, monkeypatch, [{"services": {"test": {"image": "alpine", **service}}}]
    )


def _assert_rejected(tmp_path, monkeypatch, documents):
    paths = []
    for index, doc in enumerate(documents):
        path = tmp_path / f"compose-{index}.yml"
        path.write_text(yaml.safe_dump(doc))
        paths.append(str(path))
    snapshot = Mock(side_effect=AssertionError("validation must precede Docker"))
    monkeypatch.setattr(runner.ProtectedProjectSnapshot, "capture", snapshot)
    with pytest.raises(PolicyViolation):
        runner.run_factory_card(
            "regression",
            str(tmp_path),
            paths,
            run_gates=False,
            artifacts_root=str(tmp_path / "artifacts"),
        )
    snapshot.assert_not_called()


def test_safe_override_preserves_resource_sections(tmp_path):
    base = tmp_path / "compose.yml"
    override = tmp_path / "override.yml"
    base.write_text(
        yaml.safe_dump(
            {
                "services": {"test": {"image": "alpine", "command": ["false"]}},
                "configs": {"settings": {"file": "settings.txt"}},
                "secrets": {"token": {"file": "token.txt"}},
            }
        )
    )
    override.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "test": {"command": ["true"], "environment": {"VALUE": "${VALUE}"}}
                },
            }
        )
    )
    merged = runner._load_compose_files([base, override])
    assert merged["configs"]["settings"]["file"] == "settings.txt"
    assert merged["secrets"]["token"]["file"] == "token.txt"
    assert merged["services"]["test"]["command"] == ["true"]
