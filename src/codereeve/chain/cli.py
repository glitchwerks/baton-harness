"""``bh-daemon`` console entry point.

Parses CLI arguments, loads the workflow config and repo registry, then
runs the always-on daemon via ``asyncio.run``.

Flags:
    --once          Run a single tick then exit (useful for tests and CI).
    --workflow      Path to ``WORKFLOW.md`` (default: packaged workflow).
    --poll-interval Override the outer-loop poll interval in seconds.
    --report        Path to the session report JSON file.

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
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from codereeve import __version__
from codereeve._auth import (
    TokenValidationError,
    validate_daemon_token,
    validate_gh_token,
)
from codereeve.chain import bws_client, doctor, doctor_report
from codereeve.chain.app_auth import (
    AppAuthError,
    InstallationTokenSource,
    build_installation_token_provider,
    resolve_installation_token,
)
from codereeve.chain.app_private_key import (
    AppPrivateKeyConfigError,
    requires_bws,
    resolve_app_private_key_config,
)
from codereeve.chain.daemon import run_daemon
from codereeve.chain.identity import Identity, env_for
from codereeve.chain.registry import load_registry
from codereeve.config_env import (
    AliasConflictError,
    apply_resolved_environment,
    runtime_environment,
)
from codereeve.migration.lease import LeaseError, WriterLease
from codereeve.paths import (
    PathConflictError,
    runtime_state_directory,
    select_runtime_paths,
)
from codereeve.provenance import (
    Provenance,
    ProvenanceError,
    load_provenance,
)
from codereeve.resources import as_path
from codereeve.vendor.symphony.config import load_workflow

_log = logging.getLogger(__name__)
_BOOTSTRAPPED_GH_TOKEN = ""


def _doctor_context(config_path: Path | None) -> doctor.DoctorContext:
    """Capture startup authority and resolve the selected local config.

    Args:
        config_path: Explicit config file, or the default selection.

    Returns:
        Shared doctor context retaining environment values by value.
    """

    def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a bounded worker probe with captured UTF-8 output."""
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=env_for(
                Identity.WORKER,
                base_env=ctx.env,
                installation_token=ctx.installation_token,
            ),
        )

    ctx = doctor.create_context(
        env=dict(os.environ),
        config_path=config_path,
        which=shutil.which,
        runner=run_command,
        run=run_command,
        fetch_secret=bws_client.fetch_secret,
    )
    if ctx.path_conflict is not None:
        raise ctx.path_conflict
    if ctx.project_root:
        ctx.runtime_paths = select_runtime_paths(
            Path(ctx.project_root), ctx.env
        )
    return ctx


def _doctor_gate(
    ctx: doctor.DoctorContext,
    phases: tuple[doctor.Phase, ...],
    *,
    prog: str,
) -> bool:
    """Run a fail-closed gate and render failures without leaking secrets.

    Args:
        ctx: Resolved configuration and captured startup authority.
        phases: Ordered phases to execute.
        prog: Display name for user-facing messages.

    Returns:
        Whether the selected critical checks passed.
    """
    try:
        doctor.run_gate(ctx, phases)
        return True
    except doctor.DoctorGateError as exc:
        try:
            output = doctor_report.render_text(
                exc.results,
                secret_values=doctor_report.secret_values_from_context(ctx),
            )
        except Exception:
            output = f"{prog}: doctor report could not be safely rendered\n"
        print(output, end="", file=sys.stderr)
    except Exception:
        print(f"{prog}: doctor preflight failed", file=sys.stderr)
    return False


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
    """Load selected startup secrets and retain refreshable App authority.

    The bootstrap token is captured once and scrubbed on every exit,
    including configuration errors. Optional PAT and heartbeat reads
    preserve non-empty operator overrides. The PAT stays by value in
    _BOOTSTRAPPED_GH_TOKEN; the heartbeat URL retains its ambient destination.

    Args:
        app_id: GitHub App ID, defaulting to BWS_APP_ID.
        app_private_key_bws_id: Optional override for BWS_PEM_SECRET_ID.
        installation_id: App installation ID, defaulting to
            BWS_INSTALLATION_ID.

    Returns:
        A provider retaining the verified PEM for installation-token refresh.

    Raises:
        AppAuthError: If provider configuration, credentials, or key
            loading/signing are invalid.
        BwsClientError: If an optional PAT or heartbeat fetch fails.
    """
    from codereeve.chain import bws_client

    global _BOOTSTRAPPED_GH_TOKEN

    _BOOTSTRAPPED_GH_TOKEN = ""
    try:
        values = dict(runtime_environment().values)
        access_token = values.get("BWS_ACCESS_TOKEN", "")
        if app_private_key_bws_id:
            values["BWS_PEM_SECRET_ID"] = app_private_key_bws_id
        try:
            app_key_config = resolve_app_private_key_config(values)
        except AppPrivateKeyConfigError as exc:
            raise AppAuthError(
                f"{exc}; check the selected App private-key configuration"
            ) from exc

        if requires_bws(app_key_config, values) and not access_token:
            raise AppAuthError(
                "BWS_ACCESS_TOKEN is required by the selected secret "
                "configuration"
            )

        resolved_app_id = app_id or values.get(
            "CODEREEVE_GITHUB_APP_ID", values.get("BWS_APP_ID", "")
        )
        try:
            resolved_installation_id = installation_id or int(
                values.get(
                    "CODEREEVE_GITHUB_APP_INSTALLATION_ID",
                    values.get("BWS_INSTALLATION_ID", "0"),
                )
            )
        except ValueError:
            raise AppAuthError(
                "CODEREEVE_GITHUB_APP_INSTALLATION_ID must be an integer"
            ) from None

        gh_secret_id = os.environ.get("BWS_GH_TOKEN_SECRET_ID", "")
        gh_token = os.environ.get("GH_TOKEN", "")
        if gh_secret_id and not gh_token:
            gh_token = bws_client.fetch_secret(
                gh_secret_id, access_token=access_token
            )
        _BOOTSTRAPPED_GH_TOKEN = gh_token

        heartbeat_secret_id = os.environ.get(
            "BWS_HEARTBEAT_PING_URL_SECRET_ID", ""
        )
        if heartbeat_secret_id and not values.get(
            "CODEREEVE_HEARTBEAT_PING_URL"
        ):
            values["CODEREEVE_HEARTBEAT_PING_URL"] = bws_client.fetch_secret(
                heartbeat_secret_id, access_token=access_token
            )
            apply_resolved_environment(runtime_environment(values), os.environ)

        return build_installation_token_provider(
            app_id=resolved_app_id,
            app_key_config=app_key_config,
            installation_id=resolved_installation_id,
            bws_access_token=access_token,
            fetch_secret=bws_client.fetch_secret,
        )
    finally:
        os.environ.pop("BWS_ACCESS_TOKEN", None)


def _workflow_path(workflow: str | None) -> AbstractContextManager[Path]:
    """Resolve an explicit workflow or expose the packaged default.

    Args:
        workflow: Optional operator-supplied workflow path.

    Returns:
        Context manager yielding an absolute filesystem path.
    """
    if workflow:
        return nullcontext(Path(workflow).resolve())
    return as_path("WORKFLOW.md")


def _assert_force_pr_not_merge_tripwire() -> None:
    """Fail if the force-pr-not-merge hook no longer blocks a known payload."""
    cmd = [sys.executable, "-m", "codereeve.hooks.force_pr_not_merge"]
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


def main(
    argv: list[str] | None = None,
    *,
    prog: str = "codereeve daemon",
) -> int:
    """Run the daemon command-line interface.

    Args:
        argv: Command-line arguments. Defaults to ``sys.argv[1:]``.
        prog: Display name used in help, version, errors, and logs.

    Returns:
        An integer exit code: ``0`` for success, ``1`` for configuration
        error.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog=prog,
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
            " the WORKFLOW.md shipped in the harness package."
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
        "--report",
        metavar="PATH",
        default=None,
        help=(
            "Path to the session report JSON file. Defaults to"
            " .codereeve/session-report.json in the managed repo."
        ),
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run standalone preflight checks and exit.",
    )
    parser.add_argument(
        "--check-vault",
        action="store_true",
        help="Run the live BWS PEM fetch check and exit.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when doctor finds a critical failure.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--provenance",
        action="store_true",
        help="Print validated build provenance as JSON and exit.",
    )

    parser.add_argument(
        "--phase",
        action="append",
        choices=tuple(p.value for p in doctor.Phase),
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--config", type=Path, metavar="PATH")

    args = parser.parse_args(argv)

    if args.provenance:
        try:
            provenance = load_provenance()
        except ProvenanceError as exc:
            print(f"{prog}: provenance error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(provenance.as_dict(), sort_keys=True))
        return 0

    if args.doctor or args.check_vault:
        try:
            ctx = _doctor_context(args.config)
        except (AliasConflictError, PathConflictError) as exc:
            detail = (
                exc.safe_diagnostic
                if isinstance(exc, PathConflictError)
                else str(exc)
            )
            print(f"{prog}: configuration error: {detail}", file=sys.stderr)
            return 1
        phases = (
            (doctor.Phase.LIVE,)
            if args.check_vault
            else tuple(dict.fromkeys(doctor.Phase(p) for p in args.phase))
            if args.phase
            else tuple(doctor.Phase)
        )
        try:
            results = doctor.run_report(
                ctx,
                phases,
                checks=(doctor.VAULT_PEM_DRYRUN_CHECK,)
                if args.check_vault
                else None,
            )
            report_provenance: Provenance | None
            try:
                report_provenance = load_provenance()
            except ProvenanceError:
                report_provenance = None
            secrets = doctor_report.secret_values_from_context(ctx)
            output = (
                doctor_report.render_json(
                    results, phases, report_provenance, secret_values=secrets
                )
                if args.format == "json"
                else doctor_report.render_text(results, secret_values=secrets)
            )
        except Exception:
            print(
                f"{prog}: doctor report could not be safely rendered",
                file=sys.stderr,
            )
            return 1
        print(output, end="")
        if args.check_vault:
            return (
                0
                if len(results) == 1
                and results[0].status is doctor.CheckStatus.PASS
                else 1
            )
        return int(
            args.strict
            and doctor_report.summarize(results).critical_failures > 0
        )

    workflow_description = (
        str(Path(args.workflow).resolve())
        if args.workflow
        else "packaged WORKFLOW.md"
    )
    try:
        with _workflow_path(args.workflow) as workflow_path:
            config = load_workflow(str(workflow_path))
    except Exception as exc:
        print(
            f"{prog}: error loading workflow config"
            f" {workflow_description!r}: {exc}",
            file=sys.stderr,
        )
        return 1

    from codereeve.chain import sandbox_config as _sandbox_cfg

    # One snapshot owns config and pre-bootstrap BWS authority for both gates.
    try:
        gate_ctx = _doctor_context(args.config)
    except (AliasConflictError, PathConflictError) as exc:
        detail = (
            exc.safe_diagnostic
            if isinstance(exc, PathConflictError)
            else str(exc)
        )
        print(f"{prog}: configuration error: {detail}", file=sys.stderr)
        return 1
    if not _doctor_gate(
        gate_ctx,
        (doctor.Phase.INSTALLATION, doctor.Phase.CONFIGURATION),
        prog=prog,
    ):
        return 1
    apply_resolved_environment(runtime_environment(gate_ctx.env), os.environ)
    if gate_ctx.config is not None:
        _sandbox_cfg.apply_config(gate_ctx.config, os.environ)

    # Load registry.
    try:
        registry = load_registry()
    except ValueError as exc:
        print(
            f"{prog}: registry configuration error: {exc}",
            file=sys.stderr,
        )
        print(
            "  Set CODEREEVE_REPO_OWNER, CODEREEVE_REPO_NAME,"
            " and CODEREEVE_PROJECT_ROOT"
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
    # NOTE: workflow configuration was fully loaded above, so it is
    # unaffected by this directory change.
    project_root = registry[0].project_root
    report_path = (
        Path(args.report).resolve()
        if args.report
        else (
            runtime_state_directory(Path(project_root), gate_ctx.env)
            / "session-report.json"
        ).resolve()
    )

    # Validate CODEREEVE_PROJECT_ROOT before attempting to chdir.
    if not os.path.isdir(project_root):
        print(
            f"{prog}: error: CODEREEVE_PROJECT_ROOT does not exist"
            " or is not a directory",
            file=sys.stderr,
        )
        print(
            "  Set CODEREEVE_PROJECT_ROOT to the absolute path of the clone"
            " of the managed repository.",
            file=sys.stderr,
        )
        return 1

    _log.info("%s: chdir to configured managed repo root", prog)
    try:
        os.chdir(project_root)
    except (FileNotFoundError, NotADirectoryError, OSError):
        print(
            f"{prog}: error: unable to enter CODEREEVE_PROJECT_ROOT",
            file=sys.stderr,
        )
        return 1

    try:
        _assert_force_pr_not_merge_tripwire()
    except Exception as exc:
        print(
            f"{prog}: error: force-pr-not-merge startup self-test failed:"
            f" {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        lease = WriterLease.acquire(
            Path(project_root) / ".codereeve-migration.lock", purpose="daemon"
        )
    except LeaseError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1

    with lease:
        # Bootstrap GitHub App installation token (slice 3a).
        # Must run AFTER chdir so the managed repo is the process cwd.
        # bootstrap_secrets removes BWS_ACCESS_TOKEN from os.environ in
        # finally on every bootstrap exit, after any selected vault reads.
        # The installation token is NEVER written to os.environ; it is
        # passed by value to run_daemon (env-discipline invariant).
        try:
            global _BOOTSTRAPPED_GH_TOKEN
            _BOOTSTRAPPED_GH_TOKEN = ""
            installation_token = bootstrap_secrets()
        except (AppAuthError, Exception) as exc:
            print(
                f"{prog}: error: failed to bootstrap GitHub App token: {exc}",
                file=sys.stderr,
            )
            return 1

        # Fail fast if a vault-configured GH_TOKEN resolved empty (issue #212).
        worker_gh_pat = (
            os.environ.get("GH_TOKEN", "") or _BOOTSTRAPPED_GH_TOKEN
        )
        try:
            validate_gh_token(
                worker_gh_pat,
                secret_id_configured=bool(
                    os.environ.get("BWS_GH_TOKEN_SECRET_ID")
                ),
            )
        except TokenValidationError as exc:
            print(
                f"{prog}: error: GH_TOKEN failed boot-time validation: {exc}",
                file=sys.stderr,
            )
            return 1

        # Validate the minted token before entering the event loop.
        try:
            resolved_token = resolve_installation_token(installation_token)
            validate_daemon_token(resolved_token)
        except TokenValidationError as exc:
            print(
                f"{prog}: error: invalid installation token "
                f"from bootstrap: {exc}",
                file=sys.stderr,
            )
            return 1

        gate_ctx.installation_token = resolved_token
        gate_ctx.env["GH_TOKEN"] = worker_gh_pat
        if not _doctor_gate(gate_ctx, (doctor.Phase.LIVE,), prog=prog):
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
                    worker_gh_pat=worker_gh_pat,
                    report_path=report_path,
                    runtime_paths=gate_ctx.runtime_paths,
                    writer_lease=lease,
                )
            )
        except KeyboardInterrupt:
            _log.info("%s: interrupted by user", prog)

    return 0
