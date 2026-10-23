"""Copy the curated bundle to an explicit destination without overwriting skills."""

import argparse
import shutil
from pathlib import Path


def install(destination: Path, *, apply: bool = False) -> list[Path]:
    source = Path(__file__).resolve().parents[1] / "integrations/hermes/skills"
    skills = sorted(path.parent for path in source.glob("*/SKILL.md"))
    destination = destination.expanduser().resolve()
    targets = [destination / skill.name for skill in skills]
    conflicts = [path for path in targets if path.exists() or path.is_symlink()]
    if conflicts:
        raise FileExistsError(
            "Refusing to overwrite: " + ", ".join(map(str, conflicts))
        )
    if apply:
        destination.mkdir(parents=True, exist_ok=True)
        for skill, target in zip(skills, targets, strict=True):
            shutil.copytree(skill, target)
    return targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="Copy files (default: dry run)"
    )
    args = parser.parse_args()
    try:
        targets = install(args.destination, apply=args.apply)
    except (OSError, shutil.Error) as exc:
        parser.exit(1, f"{exc}\n")
    for target in targets:
        print(f"{'Installed' if args.apply else 'Would install'} {target}")


if __name__ == "__main__":
    main()
