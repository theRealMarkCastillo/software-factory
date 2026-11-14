"""Standalone multiplexer; preserve each engine tool's argv and exit status."""

from __future__ import annotations

import importlib
import sys

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
USAGE = "usage: factory {" + "|".join(COMMANDS) + "} [args...]"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"--help", "-h"}:
        print(USAGE)
        return 0
    if not args or args[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    command = args[0]
    try:
        if command == "orchestrate":
            # Only this subcommand needs the optional workflow dependencies.
            importlib.import_module("langgraph.checkpoint.sqlite")
        module = importlib.import_module(COMMANDS[command])
        return module.main(args[1:])
    except ModuleNotFoundError as exc:
        if command != "orchestrate" or (exc.name or "").split(".")[0] not in {
            "factory_orchestration",
            "langgraph",
            "langchain_core",
            "langgraph_checkpoint_sqlite",
            "aiosqlite",
        }:
            raise
        print(
            'factory orchestrate requires: pip install "hermes-software-factory[orchestration]"',
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
