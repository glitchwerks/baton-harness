"""Private installer entry point for reversible CodeReeve service cutover."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from codereeve.chain.app_private_key import AppPrivateKeyProvider
from codereeve.chain.sandbox_config import (
    SandboxConfigError,
    resolve_config_sources,
)
from codereeve.config_env import AliasConflictError, runtime_environment
from codereeve.paths import (
    PathConflictError,
    PathLayout,
    select_compatible_file,
)

from .coordinator import cutover, install_only, recover, render_only
from .model import CutoverError, CutoverResult, FreshSecrets, ServiceSpec


def _parser() -> argparse.ArgumentParser:
    """Build the closed installer grammar shared with the shell launcher."""
    parser = argparse.ArgumentParser(
        prog="install-daemon-service.sh", allow_abbrev=False
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--print-unit", action="store_true")
    mode.add_argument("--no-start", action="store_true")
    mode.add_argument("--recover")
    parser.add_argument("--harness-dir")
    parser.add_argument("--project-root")
    parser.add_argument("--environment")
    parser.add_argument("--user")
    return parser


def _account_home(user: str) -> Path:
    """Resolve the selected account home without executing shell code.

    Args:
        user: Literal service account name.

    Returns:
        Absolute account home used by the unit and strict verification.

    Raises:
        CutoverError: If the account has no safe absolute home on Linux.
    """
    if sys.platform == "win32":
        return Path(f"/home/{user}")
    import pwd

    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        raise CutoverError(
            "dedicated service account is unavailable"
        ) from None
    if not home.is_absolute():
        raise CutoverError("dedicated service account home is invalid")
    return home


def _is_interactive() -> bool:
    """Return whether both input and output support a private prompt."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm(environment: Mapping[str, str]) -> bool:
    """Obtain the single installation confirmation when a TTY is available."""
    values = runtime_environment(environment).values
    if values.get("CODEREEVE_SETUP_NO_PROMPT") == "1" or not _is_interactive():
        return True
    try:
        answer = input("Install the CodeReeve system service? [y/N] ")
    except EOFError:
        return False
    return answer.casefold() in {"y", "yes"}


def _fresh_secrets(
    environment: Mapping[str, str],
    *,
    required: bool,
    selected_path: Path | None,
    render_only: bool,
    path_exists: Callable[[Path], bool] = Path.is_file,
    interactive: Callable[[], bool] = _is_interactive,
) -> FreshSecrets | None:
    """Create bounded ephemeral BWS bytes only for a fresh installation.

    Args:
        environment: Conflict-resolved literal operator configuration.
        required: Whether any configured consumer requires BWS.
        selected_path: Existing or canonical system secrets selection.
        render_only: Whether the caller only needs pure unit text.
        path_exists: Injectable regular-file observation.
        interactive: Injectable private-prompt availability check.

    Returns:
        Validated ephemeral assignments, or ``None`` for render, file-only,
        or an existing secrets selection.

    Raises:
        CutoverError: If a required fresh token cannot be obtained safely.
    """
    if not required or render_only:
        return None
    if selected_path is None:
        raise CutoverError("required service secrets path is unavailable")
    if path_exists(selected_path):
        return None
    values = runtime_environment(environment).values
    token = values.get("BWS_ACCESS_TOKEN", "")
    if not token:
        if values.get("CODEREEVE_SETUP_NO_PROMPT") == "1":
            raise CutoverError(
                "BWS_ACCESS_TOKEN is required when prompts are disabled"
            )
        if not interactive():
            raise CutoverError(
                "BWS_ACCESS_TOKEN is required in a non-interactive session"
            )
        token = getpass.getpass("BWS_ACCESS_TOKEN: ")
    try:
        content = b"BWS_ACCESS_TOKEN=" + token.encode("ascii") + b"\n"
    except UnicodeError:
        raise CutoverError(
            "BWS_ACCESS_TOKEN has an unsupported literal format"
        ) from None
    try:
        return FreshSecrets(content)
    except CutoverError:
        raise CutoverError(
            "BWS_ACCESS_TOKEN has an unsupported literal format"
        ) from None


def _installation_inputs(
    args: argparse.Namespace, environment: Mapping[str, str]
) -> tuple[ServiceSpec, FreshSecrets | None]:
    """Resolve one complete service selection before coordinator effects.

    Args:
        args: Parsed installer arguments.
        environment: Literal operator environment.

    Returns:
        Immutable service specification and optional ephemeral secrets.

    Raises:
        CutoverError: If required local selections are unavailable.
    """
    resolved_operator = runtime_environment(environment)
    values = dict(resolved_operator.values)
    if environment.get("ANTHROPIC_API_KEY"):
        raise CutoverError("ANTHROPIC_API_KEY must be unset for the service")

    user = args.user or values.get("SUDO_USER") or values.get("USER")
    user = user or getpass.getuser()
    home = _account_home(user)
    harness_text = args.harness_dir or values.get("CODEREEVE_ROOT")
    harness = Path(harness_text) if harness_text else Path.cwd()
    project_override = args.project_root
    if project_override:
        values["CODEREEVE_PROJECT_ROOT"] = project_override
        values.pop("BH_PROJECT_ROOT", None)
    layout = PathLayout.for_environment(
        Path(project_override) if project_override else Path.cwd(),
        values,
        home=home,
    )
    configured = resolve_config_sources(None, values, layout)
    final_values = configured.environment.values
    project_text = final_values.get("CODEREEVE_PROJECT_ROOT")
    if not project_text:
        raise CutoverError("CODEREEVE_PROJECT_ROOT is required")
    project = Path(project_text)
    workflow = harness / "config" / "WORKFLOW.md"
    if not workflow.is_file():
        raise CutoverError("workflow configuration is unavailable")
    candidate = (
        Path(args.environment)
        if args.environment
        else (harness / ".venv-codereeve")
    )
    bws_required = (
        configured.config.github_app_key_provider is AppPrivateKeyProvider.BWS
        or bool(configured.config.bws_gh_token_secret_id)
        or bool(configured.config.bws_heartbeat_ping_url_secret_id)
    )
    selected_secrets: Path | None = None
    existing_secrets: Path | None = None
    if bws_required:
        explicit_secrets = final_values.get("CODEREEVE_DAEMON_SECRETS_PATH")
        if explicit_secrets:
            selected_secrets = Path(explicit_secrets)
            existing_secrets = selected_secrets
        else:
            service_layout = PathLayout.for_environment(
                project, final_values, home=home
            )
            existing_secrets = select_compatible_file(
                service_layout.canonical_secrets,
                service_layout.legacy_secrets,
                label="service secrets",
            ).path
            selected_secrets = service_layout.canonical_secrets
    spec = ServiceSpec(
        project_root=project,
        environment=candidate,
        run_user=user,
        workflow=workflow,
        secrets=selected_secrets,
        home=home,
    )
    fresh = _fresh_secrets(
        final_values,
        required=bws_required,
        selected_path=existing_secrets,
        render_only=args.print_unit,
    )
    return spec, fresh


def _write_result(result: CutoverResult) -> int:
    """Print value-safe durable status and map it to a process exit code."""
    print(f"status: {result.status}")
    if result.journal_path is not None:
        print(f"journal_path: {result.journal_path}")
    if result.recovery is not None:
        print(f"recovery: {result.recovery}")
    return 0 if result.status in {"committed", "installed"} else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run the private service installer command.

    Args:
        argv: Installer arguments, otherwise process arguments.
        environment: Literal operator environment, otherwise ``os.environ``.

    Returns:
        Zero for rendered, installed, or committed results; nonzero otherwise.
    """
    args = _parser().parse_args(argv)
    values = dict(os.environ if environment is None else environment)
    try:
        if args.recover is not None:
            return _write_result(recover(Path(args.recover)))
        spec, fresh = _installation_inputs(args, values)
        if args.print_unit:
            sys.stdout.write(render_only(spec))
            return 0
        if not _confirm(values):
            print("status: cancelled")
            return 0
        operation = install_only if args.no_start else cutover
        return _write_result(operation(spec, fresh_secrets=fresh))
    except CutoverError as exc:
        print(f"codereeve service installer: {exc}", file=sys.stderr)
    except (AliasConflictError, SandboxConfigError, PathConflictError):
        print(
            "codereeve service installer: configuration is unavailable",
            file=sys.stderr,
        )
    except (OSError, UnicodeError, ValueError):
        print(
            "codereeve service installer: local selection is unavailable",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
