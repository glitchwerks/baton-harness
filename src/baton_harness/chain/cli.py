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
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from baton_harness._auth import (
    TokenValidationError,
    validate_daemon_token,
    validate_gh_token,
)
from baton_harness.chain.app_auth import (
    AppAuthError,
    InstallationTokenSource,
    build_installation_token_provider,
    resolve_installation_token,
)
from baton_harness.chain.daemon import run_daemon
from baton_harness.chain.identity import Identity, env_for
from baton_harness.chain.registry import load_registry
from baton_harness.vendor.symphony.config import load_workflow

_log = logging.getLogger(__name__)
_BOOTSTRAPPED_GH_TOKEN = ""

_FORCE_PR_NOT_MERGE_SELF_TEST_PAYLOAD = json.dumps(
    {
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr merge 42"},
    }
)


def bootstrap_secrets(
    *,
    app_id: str = "",
    app_private_key_bws_id: str = "",
    installation_id: int = 0,
) -> InstallationTokenSource:
    """Fetch App private key from BWS and build a refreshable token source.

    Thin wrapper around ``app_auth.build_installation_token_provider`` that
    reads env vars, calls the real implementation, and returns a provider
    object for long-running daemon use. ``BWS_ACCESS_TOKEN`` is popped
    from ``os.environ`` by the inner call as its first operation.

    This function exists as a named symbol in ``cli`` so tests can patch
    ``baton_harness.chain.cli.bootstrap_secrets`` without touching the
    implementation module.

    **Ordering invariant:** all vault fetches in this function MUST happen
    BEFORE ``build_installation_token_provider`` is called.
    ``build_installation_token_provider`` pops ``BWS_ACCESS_TOKEN`` from
    ``os.environ`` as its very first operation, so any ``fetch_secret``
    call that occurs after it would receive an empty access token and fail
    with ``BwsClientError("access_token is empty")`` in production.

    **New env vars (issue #171):**

    - ``BWS_GH_TOKEN_SECRET_ID``: optional Bitwarden Secrets ID for a
      GitHub fine-grained PAT.  When set and ``GH_TOKEN`` is absent or
      empty in the environment, fetches the PAT for immediate startup
      validation without writing it into ``os.environ``.  If ``GH_TOKEN``
      is already set to a non-empty value, the vault is not called
      (operator override wins).
    - ``BWS_HEARTBEAT_PING_URL_SECRET_ID``: optional Bitwarden Secrets ID
      for a heartbeat webhook URL.  When set and ``BH_HEARTBEAT_PING_URL``
      is absent or empty, fetches the URL and writes it to
      ``os.environ["BH_HEARTBEAT_PING_URL"]``.  Same skip logic applies.

    Both new env vars are optional for backward compatibility: omitting
    them causes no fetch attempt and no error.  Vault errors
    (``BwsClientError``) propagate — fail-closed semantics.

    The PEM key is fetched once, internally, by
    ``build_installation_token_provider`` — no duplicate vault round-trip.

    Args:
        app_id: GitHub App numeric ID string.  Defaults to
            ``BWS_APP_ID`` env var.
        app_private_key_bws_id: Bitwarden Secrets ID for the RSA PEM
            key.  Defaults to ``BWS_PEM_SECRET_ID`` env var.
        installation_id: GitHub App installation ID.  Defaults to
            ``BWS_INSTALLATION_ID`` env var.

    Returns:
        A refreshable installation-token source for daemon-side gh calls.

    Raises:
        AppAuthError: Propagated from ``build_installation_token_provider``
            on Bitwarden or GitHub API failure during PEM/token bootstrap.
        BwsClientError: Propagated from ``bws_client.fetch_secret`` when
            a vault fetch for ``GH_TOKEN`` or ``BH_HEARTBEAT_PING_URL``
            fails (fail-closed — never swallowed).
    """
    from baton_harness.chain import bws_client

    _app_id = app_id or os.environ.get("BWS_APP_ID", "")
    _pem_id = app_private_key_bws_id or os.environ.get("BWS_PEM_SECRET_ID", "")
    _install_id = installation_id or int(
        os.environ.get("BWS_INSTALLATION_ID", "0")
    )

    # ------------------------------------------------------------------
    # All vault fetches — MUST happen BEFORE
    # build_installation_token_provider() is called (see ordering
    # invariant in the docstring above).
    #
    # We read BWS_ACCESS_TOKEN here but do NOT pop it yet; the pop
    # is delegated to build_installation_token_provider() so that
    # function's env-discipline invariant is preserved.  The access
    # token is only read (not consumed) at this stage.
    # ------------------------------------------------------------------

    global _BOOTSTRAPPED_GH_TOKEN

    _access_token = os.environ.get("BWS_ACCESS_TOKEN", "")
    _BOOTSTRAPPED_GH_TOKEN = ""

    # Step 1: optional GH_TOKEN vault fetch (skip if already set).
    _gh_token_secret_id = os.environ.get("BWS_GH_TOKEN_SECRET_ID", "")
    _gh_token = os.environ.get("GH_TOKEN", "")
    if _gh_token_secret_id and not os.environ.get("GH_TOKEN"):
        _gh_token = bws_client.fetch_secret(
            _gh_token_secret_id,
            access_token=_access_token,
        )
    _BOOTSTRAPPED_GH_TOKEN = _gh_token

    # Step 2: optional BH_HEARTBEAT_PING_URL vault fetch (skip if set).
    _heartbeat_secret_id = os.environ.get(
        "BWS_HEARTBEAT_PING_URL_SECRET_ID", ""
    )
    if _heartbeat_secret_id and not os.environ.get("BH_HEARTBEAT_PING_URL"):
        os.environ["BH_HEARTBEAT_PING_URL"] = bws_client.fetch_secret(
            _heartbeat_secret_id,
            access_token=_access_token,
        )

    # Step 3: build the token provider.  The PEM is fetched once,
    # internally, inside build_installation_token_provider — no
    # duplicate vault round-trip from this function.
    return build_installation_token_provider(
        app_id=_app_id,
        app_private_key_bws_id=_pem_id,
        installation_id=_install_id,
        fetch_secret=bws_client.fetch_secret,
    )


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


def _assert_force_pr_not_merge_tripwire() -> None:
    """Fail if the force-pr-not-merge hook no longer blocks a known payload."""
    cmd = [sys.executable, "-m", "baton_harness.hooks.force_pr_not_merge"]
    with tempfile.TemporaryDirectory(
        prefix="bh-force-pr-not-merge-self-test-"
    ) as tmpdir:
        result = subprocess.run(
            cmd,
            input=_FORCE_PR_NOT_MERGE_SELF_TEST_PAYLOAD,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmpdir,
            env=env_for(Identity.WORKER),
        )
    if result.returncode != 2 or not result.stderr.startswith(
        "BH_WORKER_TRIED_MERGE:"
    ):
        raise RuntimeError(
            "expected force-pr-not-merge hook to exit 2 with "
            f"BH_WORKER_TRIED_MERGE marker; got rc={result.returncode}, "
            f"stderr={result.stderr!r}"
        )


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
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run standalone preflight checks and exit.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when doctor finds a critical failure.",
    )

    args = parser.parse_args(argv)

    if args.doctor:
        from baton_harness.chain import bws_client, doctor

        def run_command(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            """Run a doctor probe and capture its UTF-8 output."""
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env_for(Identity.WORKER),
            )

        ctx = doctor.DoctorContext(
            project_root=os.environ.get("BH_PROJECT_ROOT", ""),
            home_dir=os.path.expanduser("~"),
            env=dict(os.environ),
            which=shutil.which,
            runner=run_command,
            run=run_command,
            fetch_secret=bws_client.fetch_secret,
        )
        results = doctor.run_report(ctx)
        for result in results:
            print(f"[{result.status.name}] {result.title}")
            if result.status in {
                doctor.CheckStatus.FAIL,
                doctor.CheckStatus.WARN,
            }:
                print(f"       detail: {result.detail}")
                print(f"       fix:    {result.fix}")

        if args.strict and any(
            result.severity is doctor.Severity.CRITICAL
            and result.status is doctor.CheckStatus.FAIL
            for result in results
        ):
            return 1
        return 0

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

    # Read and validate sandbox config (populates os.environ with
    # BH_REPO_OWNER, BH_REPO_NAME, BWS_APP_ID, BWS_INSTALLATION_ID, etc.)
    from baton_harness.chain import sandbox_config as _sandbox_cfg

    _config_path = os.path.join(
        os.environ.get("BH_PROJECT_ROOT", ""), ".bh", "config.env"
    )
    if os.path.isfile(_config_path):
        try:
            _sandbox_cfg.read_and_validate(_config_path)
        except _sandbox_cfg.SandboxConfigError as exc:
            print(
                f"bh-daemon: error: sandbox config invalid: {exc.message}",
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

    try:
        _assert_force_pr_not_merge_tripwire()
    except Exception as exc:
        print(
            "bh-daemon: error: force-pr-not-merge startup self-test failed:"
            f" {exc}",
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
        global _BOOTSTRAPPED_GH_TOKEN
        _BOOTSTRAPPED_GH_TOKEN = ""
        installation_token = bootstrap_secrets()
    except (AppAuthError, Exception) as exc:
        print(
            f"bh-daemon: error: failed to bootstrap GitHub App token: {exc}",
            file=sys.stderr,
        )
        return 1

    # Fail fast if a vault-configured GH_TOKEN resolved empty (issue #212).
    try:
        validate_gh_token(
            os.environ.get("GH_TOKEN", "") or _BOOTSTRAPPED_GH_TOKEN,
            secret_id_configured=bool(
                os.environ.get("BWS_GH_TOKEN_SECRET_ID")
            ),
        )
    except TokenValidationError as exc:
        print(
            f"bh-daemon: error: GH_TOKEN failed boot-time validation: {exc}",
            file=sys.stderr,
        )
        return 1

    # Validate the minted token before entering the event loop.
    try:
        validate_daemon_token(resolve_installation_token(installation_token))
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
