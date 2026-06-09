"""``bh-daemon`` console entry point.

Parses CLI arguments, loads the workflow config and repo registry, then
runs the always-on daemon via ``asyncio.run``.

Flags:
    --once          Run a single tick then exit (useful for tests and CI).
    --workflow      Path to ``WORKFLOW.md`` (default: ``config/WORKFLOW.md``
                    relative to the repo root derived from this file's
                    location).
    --poll-interval Override the outer-loop poll interval in seconds.

Exit codes:
    0  — daemon ran (and exited via ``--once`` or signal).
    1  — configuration error (registry env vars unset, config file missing).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from baton_harness.chain.daemon import run_daemon
from baton_harness.chain.registry import load_registry
from baton_harness.vendor.symphony.config import load_workflow

_log = logging.getLogger(__name__)


def _default_workflow_path() -> Path:
    """Return the default ``config/WORKFLOW.md`` path.

    Resolves relative to the harness repo root, derived from this
    module's location (``src/baton_harness/chain/cli.py`` → four
    parents up = repo root).

    Returns:
        The absolute path to ``config/WORKFLOW.md``.
    """
    # src/baton_harness/chain/cli.py → src/baton_harness/chain →
    # src/baton_harness → src → <repo_root>
    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent.parent
    return repo_root / "config" / "WORKFLOW.md"


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``bh-daemon``.

    Args:
        argv: Command-line arguments.  Defaults to ``sys.argv[1:]``.

    Returns:
        An integer exit code: ``0`` for success, ``1`` for configuration
        error.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="bh-daemon",
        description=(
            "Always-on daemon: polls for agent-ready issues and runs them"
            " as dependency-ordered work units."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick then exit (useful for smoke tests and CI).",
    )
    parser.add_argument(
        "--workflow",
        metavar="PATH",
        default=None,
        help=(
            "Path to WORKFLOW.md config file.  Defaults to"
            " config/WORKFLOW.md in the harness repo root."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        metavar="SECONDS",
        type=float,
        default=None,
        help=(
            "Override the outer-loop poll interval in seconds."
            " Defaults to the value in WORKFLOW.md."
        ),
    )

    args = parser.parse_args(argv)

    # Resolve workflow path.
    workflow_path = (
        Path(args.workflow).resolve()
        if args.workflow
        else _default_workflow_path()
    )

    # Load workflow config.
    try:
        config = load_workflow(str(workflow_path))
    except Exception as exc:
        print(
            f"bh-daemon: error loading workflow config"
            f" {workflow_path!r}: {exc}",
            file=sys.stderr,
        )
        return 1

    # Load registry.
    try:
        registry = load_registry()
    except ValueError as exc:
        print(
            f"bh-daemon: registry configuration error: {exc}",
            file=sys.stderr,
        )
        print(
            "  Set BH_REPO_OWNER, BH_REPO_NAME, and BH_PROJECT_ROOT"
            " environment variables before running the daemon.",
            file=sys.stderr,
        )
        return 1

    # Run the daemon.
    try:
        asyncio.run(
            run_daemon(
                config,
                registry,
                once=args.once,
                poll_interval_s=args.poll_interval,
            )
        )
    except KeyboardInterrupt:
        _log.info("bh-daemon: interrupted by user")

    return 0
