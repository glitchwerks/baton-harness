"""Unified CodeReeve command-line interface."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from codereeve import (
    __version__,
    after_create,
    after_run,
    before_run,
    verify_foundation,
)
from codereeve.chain import cli as daemon_cli
from codereeve.hooks import force_pr_not_merge

Handler = Callable[[list[str]], int]


def _daemon(argv: list[str]) -> int:
    """Run the daemon with the canonical display name."""
    return daemon_cli.main(argv, prog="codereeve daemon")


def _doctor(argv: list[str]) -> int:
    """Run the daemon's doctor mode with the canonical display name."""
    return daemon_cli.main(["--doctor", *argv], prog="codereeve doctor")


def _provenance(argv: list[str]) -> int:
    """Print package provenance when no extra arguments are present."""
    if argv:
        return _usage_error("provenance does not accept arguments")
    return daemon_cli.main(["--provenance"], prog="codereeve provenance")


def _guard(argv: list[str]) -> int:
    """Run the force-pr-not-merge hook without command arguments."""
    if argv:
        return _usage_error(
            "hook force-pr-not-merge does not accept arguments"
        )
    return force_pr_not_merge.main()


def _usage_error(message: str) -> int:
    """Print a usage error and return argparse's conventional status."""
    print(f"codereeve: {message}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


HELP = """usage: codereeve [--version] COMMAND [ARGS]

commands:
  daemon
  doctor
  provenance
  hook after-create
  hook before-run
  hook after-run
  hook force-pr-not-merge
  verify"""


HANDLERS: dict[str, Handler] = {
    "daemon": _daemon,
    "doctor": _doctor,
    "provenance": _provenance,
    "verify": lambda argv: verify_foundation.main(argv),
    "hook_after_create": lambda argv: after_create.main(argv),
    "hook_before_run": lambda argv: before_run.main(argv),
    "hook_after_run": lambda argv: after_run.main(argv),
    "hook_force_pr_not_merge": _guard,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Route one canonical CodeReeve command.

    Args:
        argv: Command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        The selected command's status, or 2 for a usage error.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _usage_error("a command is required")
    if args == ["--help"] or args == ["-h"]:
        print(HELP)
        return 0
    if args == ["--version"]:
        print(f"codereeve {__version__}")
        return 0

    command = args.pop(0)
    if command == "hook":
        if not args:
            return _usage_error("a hook command is required")
        hook = args.pop(0).replace("-", "_")
        key = f"hook_{hook}"
        if key not in HANDLERS:
            return _usage_error(f"unknown hook command: {hook}")
        return HANDLERS[key](args)
    if command not in {"daemon", "doctor", "provenance", "verify"}:
        return _usage_error(f"unknown command: {command}")
    return HANDLERS[command](args)
