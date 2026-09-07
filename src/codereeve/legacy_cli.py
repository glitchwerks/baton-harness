"""Temporary Baton-era console entry points."""

from __future__ import annotations

import sys

from codereeve import cli

_WARNED: set[str] = set()


def warn_legacy(surface: str, replacement: str) -> None:
    """Print one process-local deprecation notice for a legacy surface."""
    if surface in _WARNED:
        return
    _WARNED.add(surface)
    print(
        f"{surface} is deprecated; use {replacement}; removed in 0.4.0",
        file=sys.stderr,
    )


def daemon_main() -> int:
    """Route the legacy daemon command through the canonical CLI."""
    warn_legacy("bh-daemon", "codereeve daemon")
    return cli.main(["daemon", *sys.argv[1:]])


def after_create_main() -> int:
    """Route the legacy after-create hook through the canonical CLI."""
    warn_legacy("bh-after-create", "codereeve hook after-create")
    return cli.main(["hook", "after-create", *sys.argv[1:]])


def before_run_main() -> int:
    """Route the legacy before-run hook through the canonical CLI."""
    warn_legacy("bh-before-run", "codereeve hook before-run")
    return cli.main(["hook", "before-run", *sys.argv[1:]])


def after_run_main() -> int:
    """Route the legacy after-run hook through the canonical CLI."""
    warn_legacy("bh-after-run", "codereeve hook after-run")
    return cli.main(["hook", "after-run", *sys.argv[1:]])


def force_pr_not_merge_main() -> int:
    """Route the legacy merge guard through the canonical CLI."""
    warn_legacy(
        "bh-force-pr-not-merge",
        "codereeve hook force-pr-not-merge",
    )
    return cli.main(["hook", "force-pr-not-merge", *sys.argv[1:]])


def verify_foundation_main() -> int:
    """Route the legacy verifier through the canonical CLI."""
    warn_legacy("bh-verify-foundation", "codereeve verify")
    return cli.main(["verify", *sys.argv[1:]])
