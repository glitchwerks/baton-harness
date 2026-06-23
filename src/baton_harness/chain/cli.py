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
import os
import sys
from pathlib import Path

from baton_harness._auth import TokenValidationError, validate_daemon_token
from baton_harness.chain.app_auth import (
    AppAuthError,
)
from baton_harness.chain.app_auth import (
    bootstrap_secrets as _bootstrap_secrets_impl,
)
from baton_harness.chain.daemon import run_daemon
from baton_harness.chain.registry import load_registry
from baton_harness.vendor.symphony.config import load_workflow

_log = logging.getLogger(__name__)


def bootstrap_secrets(
    *,
    app_id: str = "",
    app_private_key_bws_id: str = "",
    installation_id: int = 0,
) -> str:
    """Fetch App private key from BWS and mint an installation token.

    Thin wrapper around ``app_auth.bootstrap_secrets`` that reads env
    vars, calls the real implementation, and returns only the token
    string (discarding ``expires_at``).  ``BWS_ACCESS_TOKEN`` is popped
    from ``os.environ`` by the inner call as its first operation.

    This function exists as a named symbol in ``cli`` so tests can patch
    ``baton_harness.chain.cli.bootstrap_secrets`` without touching the
    implementation module.

    Args:
        app_id: GitHub App numeric ID string.  Defaults to
            ``BWS_APP_ID`` env var.
        app_private_key_bws_id: Bitwarden Secrets ID for the RSA PEM
            key.  Defaults to ``BWS_PEM_SECRET_ID`` env var.
        installation_id: GitHub App installation ID.  Defaults to
            ``BWS_INSTALLATION_ID`` env var.

    Returns:
        The minted installation access token (``ghs_`` prefix).

    Raises:
        AppAuthError: Propagated from ``app_auth.bootstrap_secrets`` on
            Bitwarden or GitHub API failure.
    """
    from baton_harness.chain import bws_client
    from baton_harness.chain.app_auth import mint_installation_token

    _app_id = app_id or os.environ.get("BWS_APP_ID", "")
    _pem_id = app_private_key_bws_id or os.environ.get("BWS_PEM_SECRET_ID", "")
    _install_id = installation_id or int(
        os.environ.get("BWS_INSTALLATION_ID", "0")
    )

    token, _expires_at = _bootstrap_secrets_impl(
        _app_id,
        _pem_id,
        _install_id,
        fetch_secret=bws_client.fetch_secret,
        mint_token=mint_installation_token,
    )
    return token


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

    # Resolve workflow path to ABSOLUTE before any chdir so the path
    # survives a working-directory change later in this function.
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

    # Change the process working directory to the managed repo root BEFORE
    # entering the event loop.  The vendored GitHubTracker calls ``gh``
    # without ``--repo``, so those calls resolve against the process cwd.
    # Without this chdir, the tracker's ``fetch_issue_state`` and
    # ``check_pr_exists`` would hit the harness repo (or wherever the
    # daemon was launched from) instead of the managed repo.
    # NOTE: workflow_path was resolved to absolute above, so it is
    # unaffected by this directory change.
    project_root = registry[0].project_root

    # Validate BH_PROJECT_ROOT before attempting to chdir.
    if not os.path.isdir(project_root):
        print(
            f"bh-daemon: error: BH_PROJECT_ROOT does not exist or is not a"
            f" directory: {project_root}",
            file=sys.stderr,
        )
        print(
            "  Set BH_PROJECT_ROOT to the absolute path of the local clone"
            " of the managed repository.",
            file=sys.stderr,
        )
        return 1

    _log.info("bh-daemon: chdir to managed repo root: %s", project_root)
    try:
        os.chdir(project_root)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        print(
            f"bh-daemon: error: BH_PROJECT_ROOT does not exist or is not a"
            f" directory: {project_root}: {exc}",
            file=sys.stderr,
        )
        return 1

    # Bootstrap GitHub App installation token (slice 3a).
    # Must run AFTER chdir so the managed repo is the process cwd.
    # BWS_ACCESS_TOKEN is popped from os.environ by bootstrap_secrets
    # as its first operation — never re-added after this point.
    # The installation token is NEVER written to os.environ; it is
    # passed by value to run_daemon (env-discipline invariant).
    try:
        installation_token = bootstrap_secrets()
    except (AppAuthError, Exception) as exc:
        print(
            f"bh-daemon: error: failed to bootstrap GitHub App token: {exc}",
            file=sys.stderr,
        )
        return 1

    # Validate the minted token before entering the event loop.
    try:
        validate_daemon_token(installation_token)
    except TokenValidationError as exc:
        print(
            f"bh-daemon: error: invalid installation token from bootstrap:"
            f" {exc}",
            file=sys.stderr,
        )
        return 1

    # Run the daemon.  run_daemon calls reconcile_startup internally as
    # part of its startup sweep (Gap 1A invariant: cli.py must NOT call
    # reconcile_startup directly through any import path).
    try:
        asyncio.run(
            run_daemon(
                config,
                registry,
                once=args.once,
                poll_interval_s=args.poll_interval,
                installation_token=installation_token,
            )
        )
    except KeyboardInterrupt:
        _log.info("bh-daemon: interrupted by user")

    return 0
