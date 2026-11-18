"""Factory worker role contracts and the read-only profile conformance check."""

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from factory import roles

ROOT = Path(__file__).resolve().parents[1]
ROLES_DIR = ROOT / "integrations/hermes/roles"
SKILLS_DIR = ROOT / "integrations/hermes/skills"
WORKER_ROLES = {"factory-runner", "factory-qa", "factory-architect"}


# --- the shipped contract -------------------------------------------------


def test_manifest_defines_the_three_worker_roles():
    manifest = roles.load_manifest(ROLES_DIR)
    assert set(manifest) == WORKER_ROLES


@pytest.mark.parametrize("role", sorted(WORKER_ROLES))
def test_role_references_only_shipped_skills_and_templates(role):
    spec = roles.load_manifest(ROLES_DIR)[role]
    assert (ROLES_DIR / spec["soul"]).is_file()
    for skill in spec["skills"]:
        assert (SKILLS_DIR / skill / "SKILL.md").is_file(), skill
    assert "delegation" in spec["forbidden_toolsets"]
    assert {"file", "terminal", "skills", "kanban"} <= set(spec["required_toolsets"])
    assert not set(spec["required_toolsets"]) & set(spec["forbidden_toolsets"])


@pytest.mark.parametrize("role", sorted(WORKER_ROLES))
def test_soul_templates_are_host_neutral(role):
    text = (ROLES_DIR / roles.load_manifest(ROLES_DIR)[role]["soul"]).read_text()
    assert text.startswith("# ")
    # Host paths, home-relative paths, and verification dates belong in the
    # installation's supplement, not the shared contract.
    assert "/Users/" not in text and "/home/" not in text
    assert not re.search(r"(?<![\w`])~/", text)
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text)
    assert "this host" not in text.lower()


def test_manifest_rejects_unknown_skill_and_overlapping_toolsets(tmp_path):
    (tmp_path / "x.md").write_text("# X\n")
    base = {"soul": "x.md", "skills": [], "required_toolsets": ["file"]}
    for bad in (
        {**base, "forbidden_toolsets": ["file"]},
        {**base, "forbidden_toolsets": [], "skills": ["../escape"]},
        {**base, "forbidden_toolsets": [], "soul": "../x.md"},
    ):
        (tmp_path / "roles.json").write_text(
            json.dumps({"schema": 1, "roles": {"r": bad}})
        )
        with pytest.raises(roles.RoleError):
            roles.load_manifest(tmp_path)


# --- render ---------------------------------------------------------------


def test_render_without_supplement_is_the_template():
    template = (ROLES_DIR / "factory-qa.md").read_text()
    assert roles.render("factory-qa", roles_dir=ROLES_DIR) == template


def test_render_appends_supplement_after_one_blank_line(tmp_path):
    supplement = tmp_path / "s.md"
    supplement.write_text("\n\n## Installation\n\nBoard: example\n\n\n")
    rendered = roles.render("factory-qa", roles_dir=ROLES_DIR, supplement=supplement)
    template = (ROLES_DIR / "factory-qa.md").read_text()
    assert rendered == template.rstrip("\n") + "\n\n## Installation\n\nBoard: example\n"


def test_render_unknown_role_fails():
    with pytest.raises(roles.RoleError, match="unknown role"):
        roles.render("factory-nope", roles_dir=ROLES_DIR)


# --- check against a fake Hermes home --------------------------------------


def _profile(home, name, role, *, toolsets=None, soul=None, model=None, **extra):
    profile = home / "profiles" / name
    skills = profile / "skills"
    skills.mkdir(parents=True)
    spec = roles.load_manifest(ROLES_DIR)[role]
    for i, skill in enumerate(spec["skills"]):
        # Hermes nests skills under arbitrary categories; one is a symlink.
        target = skills / f"cat{i % 2}" / skill
        target.parent.mkdir(exist_ok=True)
        if i == 0:
            target.symlink_to(SKILLS_DIR / skill, target_is_directory=True)
        else:
            import shutil

            shutil.copytree(SKILLS_DIR / skill, target)
    config = {
        "model": model or {"provider": "p", "default": "m"},
        "platform_toolsets": {
            "cli": toolsets
            if toolsets is not None
            else [*spec["required_toolsets"], "web"]
        },
        **extra,
    }
    (profile / "config.yaml").write_text(yaml.safe_dump(config))
    (profile / "SOUL.md").write_text(
        soul if soul is not None else roles.render(role, roles_dir=ROLES_DIR)
    )
    return profile


def _binding(tmp_path, profiles):
    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"schema": 1, "profiles": profiles}))
    return path


def _check(tmp_path, profiles, **kwargs):
    return roles.check(
        _binding(tmp_path, profiles),
        hermes_home=tmp_path / "home",
        roles_dir=ROLES_DIR,
        **kwargs,
    )


def test_conforming_profile_passes(tmp_path):
    _profile(tmp_path / "home", "runner", "factory-runner")
    report = _check(tmp_path, {"runner": {"role": "factory-runner"}})
    assert report == {"runner": []}


def test_soul_drift_is_reported(tmp_path):
    _profile(tmp_path / "home", "qa", "factory-qa", soul="# Hand edited\n")
    findings = _check(tmp_path, {"qa": {"role": "factory-qa"}})["qa"]
    assert any("SOUL.md differs" in f for f in findings)


def test_supplement_is_part_of_the_expected_soul(tmp_path):
    (tmp_path / "qa.md").write_text("## Installation\n\nBoard: b\n")
    soul = roles.render(
        "factory-qa", roles_dir=ROLES_DIR, supplement=tmp_path / "qa.md"
    )
    _profile(tmp_path / "home", "qa", "factory-qa", soul=soul)
    binding = {"qa": {"role": "factory-qa", "soul_supplement": "qa.md"}}
    assert _check(tmp_path, binding) == {"qa": []}
    (tmp_path / "qa.md").write_text("## Installation\n\nBoard: other\n")
    assert _check(tmp_path, binding)["qa"]


def test_missing_and_forbidden_toolsets_are_reported(tmp_path):
    home = tmp_path / "home"
    _profile(home, "a", "factory-architect", toolsets=["file", "skills", "delegation"])
    _profile(home, "b", "factory-architect")
    config = home / "profiles/b/config.yaml"
    data = yaml.safe_load(config.read_text())
    data["platform_toolsets"]["telegram"] = ["delegation"]
    config.write_text(yaml.safe_dump(data))
    report = _check(
        tmp_path,
        {"a": {"role": "factory-architect"}, "b": {"role": "factory-architect"}},
    )
    assert any("missing toolsets" in f and "kanban" in f for f in report["a"])
    assert any("forbidden toolset" in f and "cli" in f for f in report["a"])
    assert any("forbidden toolset" in f and "telegram" in f for f in report["b"])


def test_unconfigured_toolsets_fail_closed(tmp_path):
    profile = _profile(tmp_path / "home", "r", "factory-runner")
    (profile / "config.yaml").write_text(yaml.safe_dump({"model": {}}))
    findings = _check(tmp_path, {"r": {"role": "factory-runner"}})["r"]
    assert any("platform_toolsets.cli" in f for f in findings)


def test_missing_and_duplicate_skills_are_reported(tmp_path):
    profile = _profile(tmp_path / "home", "r", "factory-runner")
    skill = roles.load_manifest(ROLES_DIR)["factory-runner"]["skills"][1]
    import shutil

    shutil.rmtree(next(profile.glob(f"skills/*/{skill}")))
    other = roles.load_manifest(ROLES_DIR)["factory-runner"]["skills"][2]
    shutil.copytree(SKILLS_DIR / other, profile / "skills/extra" / other)
    findings = _check(tmp_path, {"r": {"role": "factory-runner"}})["r"]
    assert any(f"missing skill {skill}" in f for f in findings)
    assert any(f"duplicate skill {other}" in f for f in findings)


def test_skill_content_is_compared_with_a_source_tree(tmp_path):
    profile = _profile(tmp_path / "home", "r", "factory-runner")
    skill = roles.load_manifest(ROLES_DIR)["factory-runner"]["skills"][1]
    binding = {"r": {"role": "factory-runner"}}
    assert _check(tmp_path, binding, skills_source=SKILLS_DIR) == {"r": []}
    (next(profile.glob(f"skills/*/{skill}")) / "SKILL.md").write_text("edited")
    findings = _check(tmp_path, binding, skills_source=SKILLS_DIR)["r"]
    assert any(f"skill {skill} differs" in f for f in findings)


def test_model_pin_is_checked_when_bound(tmp_path):
    _profile(tmp_path / "home", "r", "factory-runner")
    ok = {"r": {"role": "factory-runner", "model": {"provider": "p", "default": "m"}}}
    bad = {"r": {"role": "factory-runner", "model": {"provider": "p", "default": "x"}}}
    assert _check(tmp_path, ok) == {"r": []}
    assert any("model" in f for f in _check(tmp_path, bad)["r"])


def test_missing_profile_is_a_finding(tmp_path):
    (tmp_path / "home/profiles").mkdir(parents=True)
    findings = _check(tmp_path, {"ghost": {"role": "factory-qa"}})["ghost"]
    assert findings and "profile not found" in findings[0]


def test_binding_errors_are_configuration_errors(tmp_path):
    for profiles in (
        {"r": {"role": "factory-nope"}},
        {"../r": {"role": "factory-qa"}},
        {"r": {"role": "factory-qa", "soul_supplement": "/abs.md"}},
        {"r": {"role": "factory-qa", "surprise": 1}},
    ):
        with pytest.raises(roles.RoleError):
            _check(tmp_path, profiles)


def test_check_never_opens_or_prints_credentials(tmp_path, capsys):
    profile = _profile(
        tmp_path / "home", "r", "factory-runner", api_key="sk-do-not-print-me"
    )
    for name in ("auth.json", ".env"):
        (profile / name).write_text("secret")
        os.chmod(profile / name, 0)
    binding = _binding(tmp_path, {"r": {"role": "factory-qa"}})
    code = roles.main(
        [
            "check",
            "--binding",
            str(binding),
            "--hermes-home",
            str(tmp_path / "home"),
            "--roles-dir",
            str(ROLES_DIR),
        ]
    )
    out = capsys.readouterr()
    assert code == 1  # bound to the wrong role, so SOUL and skills drift
    assert "sk-do-not-print-me" not in out.out + out.err
    assert "secret" not in out.out + out.err


# --- CLI ------------------------------------------------------------------


def test_cli_exit_codes(tmp_path, capsys):
    _profile(tmp_path / "home", "r", "factory-runner")
    common = ["--hermes-home", str(tmp_path / "home"), "--roles-dir", str(ROLES_DIR)]
    good = _binding(tmp_path, {"r": {"role": "factory-runner"}})
    assert roles.main(["check", "--binding", str(good), *common]) == 0
    assert "OK r (factory-runner)" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": 1, "profiles": {"r": {"role": "nope"}}}))
    assert roles.main(["check", "--binding", str(bad), *common]) == 2


def test_cli_list_and_render(capsys):
    assert roles.main(["list", "--roles-dir", str(ROLES_DIR)]) == 0
    assert set(capsys.readouterr().out.split()) >= WORKER_ROLES
    assert roles.main(["render", "factory-qa", "--roles-dir", str(ROLES_DIR)]) == 0
    assert capsys.readouterr().out == (ROLES_DIR / "factory-qa.md").read_text()
