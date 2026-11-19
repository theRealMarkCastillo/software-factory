"""Holdout boundaries; Docker is simulated, not deployment acceptance."""

import json
from pathlib import Path

import pytest

from factory import runner
from factory.policy import PolicyViolation


def suite(tmp_path):
    root = (tmp_path / "private").resolve()
    root.mkdir()
    (root / "test_secret.py").write_text("CANARY_HOLDOUT_SOURCE = True\n")
    (root / "holdout.json").write_text(
        json.dumps(
            {
                "version": 1,
                "service": "test",
                "command": ["python", "/factory-holdout/test_secret.py"],
                "files": ["test_secret.py"],
            }
        )
    )
    return root


@pytest.mark.parametrize(
    "case",
    [
        "inside",
        "relative",
        "symlink",
        "parent_symlink",
        "unlisted",
        "missing",
        "traversal",
        "special",
    ],
)
def test_holdout_paths_refused(tmp_path, case):
    from factory.holdout import load_holdout

    repo = tmp_path / "repo"
    repo.mkdir()
    root = suite(tmp_path)
    if case == "inside":
        repo = root.parent
    elif case == "relative":
        root = Path("relative")
    elif case in ("symlink", "parent_symlink"):
        link = tmp_path / "link"
        link.symlink_to(
            root if case == "symlink" else root.parent, target_is_directory=True
        )
        root = link if case == "symlink" else link / root.name
    elif case == "unlisted":
        (root / "extra.txt").write_text("unexpected")
    elif case == "missing":
        (root / "test_secret.py").unlink()
    elif case == "traversal":
        doc = json.loads((root / "holdout.json").read_text())
        doc["files"] = ["../escape"]
        (root / "holdout.json").write_text(json.dumps(doc))
    elif case == "special":
        import os

        os.mkfifo(root / "fifo")
    with pytest.raises((PolicyViolation, ValueError)):
        load_holdout(str(root), repo)


def test_holdout_path_dollars_are_literal(tmp_path):
    import json

    from factory.policy import holdout_override

    root = suite(tmp_path)
    renamed = root.with_name("private-$VARIABLE")
    root.rename(renamed)
    override = holdout_override(
        {"services": {"test": {"image": "python"}}}, str(renamed), tmp_path / "repo"
    )
    assert override["services"]["test"]["volumes"][0]["source"] == str(renamed).replace(
        "$", "$$"
    )
    assert json.loads((renamed / "holdout.json").read_text())["version"] == 1


def test_holdout_walk_errors_fail_closed(tmp_path, monkeypatch):
    import os

    from factory.holdout import load_holdout

    root = suite(tmp_path)

    def unreadable(path, **kwargs):
        if kwargs.get("onerror"):
            kwargs["onerror"](PermissionError("private contents"))
        return iter([(str(root), [], ["holdout.json", "test_secret.py"])])

    monkeypatch.setattr(os, "walk", unreadable)
    with pytest.raises(PolicyViolation, match="invalid holdout"):
        load_holdout(str(root), tmp_path / "repo")


@pytest.mark.parametrize("entry", [".", "./", "", "../escape", "/absolute"])
def test_manifest_refuses_non_file_paths(tmp_path, entry):
    from factory.holdout import load_holdout

    root = suite(tmp_path)
    doc = json.loads((root / "holdout.json").read_text())
    doc["files"] = [entry]
    (root / "holdout.json").write_text(json.dumps(doc))
    with pytest.raises(PolicyViolation):
        load_holdout(str(root), tmp_path / "repo")


def test_manifest_command_preserves_empty_argv_arguments(tmp_path):
    from factory.holdout import load_holdout

    root = suite(tmp_path)
    doc = json.loads((root / "holdout.json").read_text())
    doc["command"] = ["echo", ""]
    (root / "holdout.json").write_text(json.dumps(doc))
    assert load_holdout(str(root), tmp_path / "repo").command == ["echo", ""]


def test_holdout_summary_counts():
    from factory.holdout import summary_counts

    assert summary_counts("test | === 2 failed, 3 passed in 1s ===") == {
        "passed": 3,
        "failed": 2,
    }
    assert summary_counts(
        '<testsuite tests="5" failures="1" errors="1" skipped="1"/>'
    ) == {"passed": 2, "failed": 2}
    assert summary_counts("unparseable") == {"passed": None, "failed": None}


def test_holdout_digests_and_reserved_runs(tmp_path):
    from factory.holdout import load_holdout

    root = suite(tmp_path)
    first = load_holdout(str(root), tmp_path / "repo")
    assert len(first.digests["manifest_sha256"]) == 64
    assert set(first.digests["files"]) == {"test_secret.py"}
    (root / "runs" / "old").mkdir(parents=True)
    (root / "runs" / "old" / "stdout.log").write_text("private output")
    assert load_holdout(str(root), tmp_path / "repo").digests == first.digests
    (root / "test_secret.py").write_text("changed")
    assert load_holdout(str(root), tmp_path / "repo").digests != first.digests


@pytest.mark.parametrize(
    "options, refusal",
    [
        ({"deployment": True, "check": "check.sh"}, "deployment"),
        ({"run_gates": False}, "no-gates"),
        ({"mode": "diagnostic"}, "diagnostic"),
    ],
)
def test_holdout_refuses_non_acceptance_before_any_work(tmp_path, options, refusal):
    with pytest.raises(ValueError, match=refusal):
        runner.run_factory_card(
            "holdout",
            str(tmp_path / "absent-repo"),
            ["compose.yml"],
            holdout=str(tmp_path / "private"),
            **options,
        )
