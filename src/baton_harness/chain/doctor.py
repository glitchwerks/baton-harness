"""Preflight readiness checks for the Baton harness daemon."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path

from baton_harness import resources
from baton_harness.chain import app_auth, ruleset_status, sandbox_config
from baton_harness.chain.app_private_key import (
    AppPrivateKeyConfig,
    AppPrivateKeyConfigError,
    load_app_private_key,
    requires_bws,
)
from baton_harness.provenance import load_provenance
from baton_harness.vendor.symphony.config import load_workflow

RunFn = Callable[..., subprocess.CompletedProcess[str]]
FetchSecretFn = Callable[..., str]
RunnerFn = Callable[[list[str]], subprocess.CompletedProcess[str]]
WhichFn = Callable[[str], str | None]
CheckFn = Callable[["DoctorContext"], "CheckResult"]


class Severity(str, Enum):
    """Severity assigned to a preflight check."""

    CRITICAL = "critical"
    WARNING = "warning"


class CheckStatus(str, Enum):
    """Outcome of a preflight check."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class Phase(str, Enum):
    """Stable public phase for a preflight check."""

    INSTALLATION = "installation"
    CONFIGURATION = "configuration"
    LIVE = "live"


@dataclass
class CheckResult:
    """Result returned by a preflight check.

    Attributes:
        check_id: Stable identifier for the check.
        phase: Public preflight phase that owns the check.
        title: Human-readable check title.
        severity: Operational severity of a failure.
        status: Outcome of the check.
        detail: Secret-safe explanation of the outcome.
        remediation: Secret-safe remediation guidance.
    """

    check_id: str
    phase: Phase
    title: str
    severity: Severity
    status: CheckStatus
    detail: str
    remediation: str


@dataclass
class DoctorContext:
    """Injected dependencies and values used by preflight checks.

    Attributes:
        project_root: Target repository root directory.
        home_dir: Effective user home directory.
        env: Environment values available to checks.
        which: Executable lookup seam.
        runner: Single-argument subprocess seam for local probes.
        run: General subprocess seam reserved for later phases.
        fetch_secret: Secret-fetch seam reserved for later phases.
        installation_token: GitHub App token passed by value.
        config_path: Selected sandbox config path, when available.
        config: Purely resolved sandbox config, when valid.
        config_error: Safe local resolution error, when invalid.
    """

    project_root: str
    home_dir: str
    env: dict[str, str]
    which: WhichFn
    runner: RunnerFn
    run: RunFn
    fetch_secret: FetchSecretFn
    installation_token: str = ""
    config_path: Path | None = None
    config: sandbox_config.SandboxConfig | None = None
    config_error: str = ""


class DoctorGateError(RuntimeError):
    """Raised after all selected critical preflight failures are collected."""

    def __init__(self, results: Sequence[CheckResult]) -> None:
        """Store the complete selected result set for caller rendering.

        Args:
            results: Complete results collected by the gate.
        """
        super().__init__("critical preflight checks failed")
        self.results = tuple(results)


@dataclass
class Check:
    """Callable preflight check with static catalog metadata.

    Attributes:
        check_id: Stable identifier for the check.
        title: Human-readable check title.
        severity: Operational severity of a failure.
        phase: Startup phase in which the check applies.
        daemon_native: Whether native daemon code already runs it.
        fix: Secret-safe remediation guidance.
        fn: Callable implementing the check.
    """

    check_id: str
    title: str
    severity: Severity
    phase: Phase
    daemon_native: bool
    fix: str
    fn: CheckFn

    def __call__(self, ctx: DoctorContext) -> CheckResult:
        """Run the check implementation.

        Args:
            ctx: Injected doctor context.

        Returns:
            The check result.
        """
        return self.fn(ctx)


def _phase_for(check_id: str) -> Phase:
    """Return the phase assigned by the authoritative catalog.

    Args:
        check_id: Stable preflight check identifier.

    Returns:
        The phase that owns the check.

    Raises:
        ValueError: If the check ID is not part of the public catalog.
    """
    for check in CATALOG:
        if check.check_id == check_id:
            return check.phase
    raise ValueError(f"unknown preflight check id: {check_id}")


def _config_error_detail(exc: BaseException) -> str:
    """Return a non-empty diagnostic for an expected config failure.

    Args:
        exc: Expected resolver, filesystem, or decoding failure.

    Returns:
        The exception message, or its type when the message is empty.
    """
    return str(exc) or type(exc).__name__


def create_context(
    *,
    env: Mapping[str, str],
    config_path: Path | None = None,
    home_dir: str | None = None,
    installation_token: str = "",
    which: WhichFn,
    runner: RunnerFn,
    run: RunFn,
    fetch_secret: FetchSecretFn,
) -> DoctorContext:
    """Create one non-mutating context for every preflight phase.

    Config selection and resolution use the pure sandbox-config APIs. A
    local config error is retained in the context so installation checks
    can still run independently and configuration checks can report it.

    Args:
        env: Environment snapshot available to checks.
        config_path: Optional explicit sandbox config path.
        home_dir: Optional effective home directory.
        installation_token: GitHub App token passed only by value.
        which: Executable lookup seam.
        runner: Single-argument subprocess seam.
        run: General subprocess seam.
        fetch_secret: Secret-fetch seam.

    Returns:
        A context with a copied environment and pure config resolution.
    """
    environment = dict(env)
    selected_path: Path | None = None
    resolved_config: sandbox_config.SandboxConfig | None = None
    config_error = ""
    try:
        selected_path = sandbox_config.select_config_path(
            os.fspath(config_path) if config_path is not None else None,
            environment,
        )
        resolved_config = sandbox_config.resolve_config(
            selected_path, environment
        )
    except (sandbox_config.SandboxConfigError, OSError, UnicodeError) as exc:
        config_error = _config_error_detail(exc)
        if config_path is not None:
            selected_path = config_path

    project_root = environment.get("BH_PROJECT_ROOT", "")
    if not project_root and selected_path is not None:
        if selected_path.parent.name == ".bh":
            project_root = str(selected_path.parent.parent)

    return DoctorContext(
        project_root=project_root,
        home_dir=home_dir if home_dir is not None else os.path.expanduser("~"),
        env=environment,
        which=which,
        runner=runner,
        run=run,
        fetch_secret=fetch_secret,
        installation_token=installation_token,
        config_path=selected_path,
        config=resolved_config,
        config_error=config_error,
    )


def _result(
    check_id: str,
    title: str,
    severity: Severity,
    status: CheckStatus,
    detail: str,
    remediation: str,
) -> CheckResult:
    """Build a check result from secret-safe values.

    Args:
        check_id: Stable identifier for the check.
        title: Human-readable check title.
        severity: Operational severity of a failure.
        status: Outcome of the check.
        detail: Secret-safe explanation of the outcome.
        remediation: Secret-safe remediation guidance.

    Returns:
        A populated check result.
    """
    return CheckResult(
        check_id=check_id,
        phase=_phase_for(check_id),
        title=title,
        severity=severity,
        status=status,
        detail=detail,
        remediation=remediation,
    )


def _config_path(ctx: DoctorContext) -> Path:
    """Return the selected config path for local configuration checks.

    Args:
        ctx: Context carrying an explicit path or legacy project root.

    Returns:
        The config path selected for this context.
    """
    if ctx.config_path is not None:
        return ctx.config_path
    return Path(ctx.project_root) / ".bh" / "config.env"


def _resolved_config(ctx: DoctorContext) -> sandbox_config.SandboxConfig:
    """Return one strict config snapshot and cache its result on the context.

    Args:
        ctx: Context carrying a resolved config or its selected path.

    Returns:
        The validated sandbox configuration.

    Raises:
        SandboxConfigError: If config selection, reading, or validation fails.
    """
    if ctx.config is not None:
        return ctx.config
    if ctx.config_error:
        raise sandbox_config.SandboxConfigError(ctx.config_error)

    try:
        ctx.config = sandbox_config.resolve_config(_config_path(ctx), ctx.env)
    except (sandbox_config.SandboxConfigError, OSError, UnicodeError) as exc:
        ctx.config_error = _config_error_detail(exc)
        raise sandbox_config.SandboxConfigError(ctx.config_error) from exc
    return ctx.config


def _resolved_private_key_context(
    ctx: DoctorContext,
) -> tuple[AppPrivateKeyConfig, dict[str, str]]:
    """Resolve provider and optional consumers from one config snapshot.

    Args:
        ctx: Context supplying the config root and environment overrides.

    Returns:
        Validated provider configuration and complete resolved values.

    Raises:
        AppPrivateKeyConfigError: If config is unreadable or invalid.
    """
    try:
        resolved = _resolved_config(ctx)
    except sandbox_config.SandboxConfigError as exc:
        raise AppPrivateKeyConfigError(str(exc)) from exc

    values: dict[str, str] = {}
    sandbox_config.apply_config(resolved, values)
    config = AppPrivateKeyConfig(
        provider=resolved.github_app_key_provider,
        bws_secret_id=resolved.bws_pem_secret_id,
        file_path=resolved.github_app_private_key_file,
    )
    return config, values


def _check_package_provenance(ctx: DoctorContext) -> CheckResult:
    """Validate the immutable provenance packaged with the distribution.

    Args:
        ctx: Injected doctor context, unused by this offline check.

    Returns:
        A passing result when packaged provenance is valid.
    """
    del ctx
    load_provenance()
    return _result(
        "PKG_PROVENANCE",
        "Runtime provenance is valid",
        Severity.CRITICAL,
        CheckStatus.PASS,
        "Installed metadata matches the packaged provenance record.",
        "Reinstall a verified baton-harness artifact.",
    )


def _check_package_imports(ctx: DoctorContext) -> CheckResult:
    """Import each supported runtime package boundary.

    Args:
        ctx: Injected doctor context, unused by this offline check.

    Returns:
        A passing result when every required package imports.
    """
    del ctx
    module_names = (
        "baton_harness",
        "baton_harness.chain.cli",
        "baton_harness.vendor.symphony.config",
    )
    for module_name in module_names:
        importlib.import_module(module_name)
    return _result(
        "PKG_IMPORTS",
        "Runtime package imports succeed",
        Severity.CRITICAL,
        CheckStatus.PASS,
        "Required runtime modules import successfully.",
        "Reinstall the baton-harness package and its runtime dependencies.",
    )


_REQUIRED_ENTRY_POINTS = frozenset(
    {
        "bh-after-create",
        "bh-before-run",
        "bh-after-run",
        "bh-daemon",
        "bh-force-pr-not-merge",
        "bh-verify-foundation",
    }
)


def _check_package_entry_points(ctx: DoctorContext) -> CheckResult:
    """Require every supported console script in installed metadata.

    Args:
        ctx: Injected doctor context, unused by this offline check.

    Returns:
        A result listing any missing console scripts.
    """
    del ctx
    installed = {
        entry_point.name
        for entry_point in metadata.distribution("baton-harness").entry_points
        if entry_point.group == "console_scripts"
    }
    missing = sorted(_REQUIRED_ENTRY_POINTS - installed)
    if missing:
        return _result(
            "PKG_ENTRY_POINTS",
            "Installed entry points are complete",
            Severity.CRITICAL,
            CheckStatus.FAIL,
            "Missing installed console scripts: " + ", ".join(missing),
            "Reinstall a complete baton-harness artifact.",
        )
    return _result(
        "PKG_ENTRY_POINTS",
        "Installed entry points are complete",
        Severity.CRITICAL,
        CheckStatus.PASS,
        "All required console scripts are installed.",
        "Reinstall a complete baton-harness artifact.",
    )


def _check_package_resources(ctx: DoctorContext) -> CheckResult:
    """Read every resource in the packaged-resource manifest.

    Args:
        ctx: Injected doctor context, unused by this offline check.

    Returns:
        A passing result when every packaged resource is readable.
    """
    del ctx
    for name in resources.RESOURCE_NAMES:
        resources.read_bytes(name)
    return _result(
        "PKG_RESOURCES",
        "Packaged resources are complete",
        Severity.CRITICAL,
        CheckStatus.PASS,
        "Every required packaged resource is readable.",
        "Reinstall a complete baton-harness artifact.",
    )


def _check_package_workflow(ctx: DoctorContext) -> CheckResult:
    """Parse the packaged default workflow through the runtime loader.

    Args:
        ctx: Injected doctor context, unused by this offline check.

    Returns:
        A passing result when the packaged workflow parses.
    """
    del ctx
    from baton_harness.chain.cli import _workflow_path

    with _workflow_path(None) as workflow_path:
        load_workflow(str(workflow_path))
    return _result(
        "PKG_WORKFLOW",
        "Packaged workflow is valid",
        Severity.CRITICAL,
        CheckStatus.PASS,
        "The packaged default workflow parses successfully.",
        "Reinstall a verified baton-harness artifact.",
    )


def _cli_result(
    ctx: DoctorContext,
    *,
    check_id: str,
    title: str,
    binary: str,
    severity: Severity,
    missing_status: CheckStatus,
    fix: str,
) -> CheckResult:
    """Check whether one executable is available on ``PATH``.

    Args:
        ctx: Injected doctor context.
        check_id: Stable identifier for the check.
        title: Human-readable check title.
        binary: Executable name to locate.
        severity: Operational severity of a failure.
        missing_status: Status to return when the executable is absent.
        fix: Remediation guidance.

    Returns:
        PASS when found, otherwise ``missing_status``.
    """
    if ctx.which(binary):
        return _result(
            check_id,
            title,
            severity,
            CheckStatus.PASS,
            f"{binary} is available on PATH.",
            fix,
        )
    return _result(
        check_id,
        title,
        severity,
        missing_status,
        f"{binary} is not available on PATH.",
        fix,
    )


def _check_cli_gh(ctx: DoctorContext) -> CheckResult:
    """Check that the GitHub CLI is available.

    Args:
        ctx: Injected doctor context.

    Returns:
        GitHub CLI availability result.
    """
    return _cli_result(
        ctx,
        check_id="CLI_GH",
        title="GitHub CLI available",
        binary="gh",
        severity=Severity.CRITICAL,
        missing_status=CheckStatus.FAIL,
        fix="Install gh and ensure it is on PATH.",
    )


def _check_cli_bws(ctx: DoctorContext) -> CheckResult:
    """Check that the Bitwarden Secrets CLI is available.

    Args:
        ctx: Injected doctor context.

    Returns:
        Bitwarden Secrets CLI availability result.
    """
    title = "Bitwarden Secrets CLI available"
    fix = "Install bws and ensure it is on PATH."
    try:
        config, values = _resolved_private_key_context(ctx)
    except AppPrivateKeyConfigError as exc:
        return _result(
            "CLI_BWS",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            str(exc),
            "Correct the App private-key provider configuration.",
        )
    if not requires_bws(config, values):
        return _result(
            "CLI_BWS",
            title,
            Severity.CRITICAL,
            CheckStatus.PASS,
            "BWS is not required by the resolved secret configuration.",
            fix,
        )
    return _cli_result(
        ctx,
        check_id="CLI_BWS",
        title=title,
        binary="bws",
        severity=Severity.CRITICAL,
        missing_status=CheckStatus.FAIL,
        fix=fix,
    )


def _check_cli_claude(ctx: DoctorContext) -> CheckResult:
    """Check that the Claude CLI is available.

    Args:
        ctx: Injected doctor context.

    Returns:
        Claude CLI availability result.
    """
    return _cli_result(
        ctx,
        check_id="CLI_CLAUDE",
        title="Claude CLI available",
        binary="claude",
        severity=Severity.CRITICAL,
        missing_status=CheckStatus.FAIL,
        fix="Install claude and ensure it is on PATH.",
    )


def _check_cli_uv(ctx: DoctorContext) -> CheckResult:
    """Check that the uv package manager is available.

    Args:
        ctx: Injected doctor context.

    Returns:
        uv availability result.
    """
    return _cli_result(
        ctx,
        check_id="CLI_UV",
        title="uv package manager available",
        binary="uv",
        severity=Severity.WARNING,
        missing_status=CheckStatus.WARN,
        fix="Install uv and ensure it is on PATH.",
    )


def _check_project_root(ctx: DoctorContext) -> CheckResult:
    """Check that the configured project root is a directory.

    Args:
        ctx: Injected doctor context.

    Returns:
        Project-root validity result.
    """
    title = "Project root is valid"
    fix = "Set BH_PROJECT_ROOT to an existing directory."
    if ctx.project_root and Path(ctx.project_root).is_dir():
        return _result(
            "ENV_PROJECT_ROOT",
            title,
            Severity.CRITICAL,
            CheckStatus.PASS,
            "BH_PROJECT_ROOT identifies an existing directory.",
            fix,
        )
    return _result(
        "ENV_PROJECT_ROOT",
        title,
        Severity.CRITICAL,
        CheckStatus.FAIL,
        "BH_PROJECT_ROOT is empty or is not an existing directory.",
        fix,
    )


def _check_host_env(ctx: DoctorContext) -> CheckResult:
    """Check whether the optional host environment file exists.

    Args:
        ctx: Injected doctor context.

    Returns:
        Host environment file presence result.
    """
    title = "Host environment file present"
    fix = "Create ~/.config/baton-harness/host.env if it is needed."
    path = Path(ctx.home_dir) / ".config" / "baton-harness" / "host.env"
    if path.exists():
        status = CheckStatus.PASS
        detail = "The host environment file is present."
    else:
        status = CheckStatus.WARN
        detail = "The host environment file is absent."
    return _result(
        "ENV_HOST_ENV", title, Severity.WARNING, status, detail, fix
    )


def _check_config_env(ctx: DoctorContext) -> CheckResult:
    """Check whether ``.bh/config.env`` exists.

    Args:
        ctx: Injected doctor context.

    Returns:
        Config file presence result.
    """
    title = "Sandbox config file present"
    fix = "Create .bh/config.env in BH_PROJECT_ROOT."
    path = _config_path(ctx)
    if path.is_file():
        status = CheckStatus.PASS
        detail = ".bh/config.env is present."
    else:
        status = CheckStatus.FAIL
        detail = ".bh/config.env is missing."
    return _result(
        "CFG_CONFIG_ENV", title, Severity.CRITICAL, status, detail, fix
    )


def _check_required_keys(ctx: DoctorContext) -> CheckResult:
    """Validate required config keys without subprocess or network use.

    Args:
        ctx: Injected doctor context.

    Returns:
        Required-key validation result.
    """
    title = "Required sandbox config keys valid"
    fix = "Set all required .bh/config.env keys to valid values."
    try:
        _resolved_config(ctx)
    except sandbox_config.SandboxConfigError as exc:
        return _result(
            "CFG_REQUIRED_KEYS",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            str(exc),
            fix,
        )
    return _result(
        "CFG_REQUIRED_KEYS",
        title,
        Severity.CRITICAL,
        CheckStatus.PASS,
        "All required config keys are present and shape-valid.",
        fix,
    )


def _check_optional_secret_ids(ctx: DoctorContext) -> CheckResult:
    """Validate optional secret IDs without subprocess or network use.

    Args:
        ctx: Injected doctor context.

    Returns:
        Optional secret-ID validation result.
    """
    title = "Optional secret IDs valid"
    fix = "Use UUID values for optional BWS secret ID settings."
    path = _config_path(ctx)
    if not path.exists():
        return _result(
            "CFG_OPTIONAL_SECRET_IDS",
            title,
            Severity.WARNING,
            CheckStatus.SKIP,
            ".bh/config.env is missing; optional IDs are not applicable.",
            fix,
        )
    try:
        _resolved_config(ctx)
    except sandbox_config.SandboxConfigError as exc:
        return _result(
            "CFG_OPTIONAL_SECRET_IDS",
            title,
            Severity.WARNING,
            CheckStatus.WARN,
            str(exc),
            fix,
        )
    return _result(
        "CFG_OPTIONAL_SECRET_IDS",
        title,
        Severity.WARNING,
        CheckStatus.PASS,
        "All configured optional secret IDs are shape-valid.",
        fix,
    )


def _check_bws_access_token(ctx: DoctorContext) -> CheckResult:
    """Check for a non-empty BWS access token without exposing it.

    Args:
        ctx: Injected doctor context.

    Returns:
        BWS access-token presence result.
    """
    title = "BWS access token present"
    fix = "Set BWS_ACCESS_TOKEN to a non-empty access token."
    try:
        config, values = _resolved_private_key_context(ctx)
    except AppPrivateKeyConfigError as exc:
        return _result(
            "ENV_BWS_ACCESS_TOKEN",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            str(exc),
            "Correct the App private-key provider configuration.",
        )
    if not requires_bws(config, values):
        return _result(
            "ENV_BWS_ACCESS_TOKEN",
            title,
            Severity.CRITICAL,
            CheckStatus.PASS,
            "BWS is not required by the resolved secret configuration.",
            fix,
        )
    token = ctx.env.get("BWS_ACCESS_TOKEN", "")
    if token:
        status = CheckStatus.PASS
        detail = "BWS_ACCESS_TOKEN is set."
    else:
        status = CheckStatus.FAIL
        detail = "BWS_ACCESS_TOKEN is unset or empty."
    return _result(
        "ENV_BWS_ACCESS_TOKEN",
        title,
        Severity.CRITICAL,
        status,
        detail,
        fix,
    )


def _check_gitignore_symphony(ctx: DoctorContext) -> CheckResult:
    """Check for the exact ``.symphony/`` gitignore entry.

    Args:
        ctx: Injected doctor context.

    Returns:
        Gitignore-entry presence result.
    """
    title = "Symphony state is gitignored"
    fix = "Add an exact .symphony/ line to the repository .gitignore."
    path = Path(ctx.project_root) / ".gitignore"
    if not path.exists():
        status = CheckStatus.FAIL
        detail = ".gitignore is missing."
    elif any(
        line.strip() == ".symphony/"
        for line in path.read_text(encoding="utf-8").splitlines()
    ):
        status = CheckStatus.PASS
        detail = ".gitignore contains the required .symphony/ entry."
    else:
        status = CheckStatus.FAIL
        detail = ".gitignore lacks an exact .symphony/ entry."
    return _result(
        "GITIGNORE_SYMPHONY",
        title,
        Severity.CRITICAL,
        status,
        detail,
        fix,
    )


def _check_anthropic_unset(ctx: DoctorContext) -> CheckResult:
    """Check that ``ANTHROPIC_API_KEY`` is not configured.

    Args:
        ctx: Injected doctor context.

    Returns:
        API-key absence result without exposing the key.
    """
    title = "Anthropic API key is unset"
    fix = "Unset ANTHROPIC_API_KEY and use mounted OAuth credentials."
    if ctx.env.get("ANTHROPIC_API_KEY"):
        status = CheckStatus.FAIL
        detail = "ANTHROPIC_API_KEY is set; OAuth deployment requires unset."
    else:
        status = CheckStatus.PASS
        detail = "ANTHROPIC_API_KEY is unset."
    return _result(
        "CRED_ANTHROPIC_UNSET",
        title,
        Severity.CRITICAL,
        status,
        detail,
        fix,
    )


def _check_force_pr_tripwire(ctx: DoctorContext) -> CheckResult:
    """Run the force-PR-not-merge startup self-test.

    Args:
        ctx: Injected doctor context, unused by this local self-test.

    Returns:
        Tripwire self-test result.
    """
    del ctx
    title = "Force-PR-not-merge tripwire passes"
    fix = "Restore the force-pr-not-merge hook and its startup self-test."
    try:
        from baton_harness.chain import cli

        cli._assert_force_pr_not_merge_tripwire()
    except Exception as exc:  # noqa: BLE001
        return _result(
            "FORCE_PR_TRIPWIRE",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            f"Tripwire self-test raised {type(exc).__name__}.",
            fix,
        )
    return _result(
        "FORCE_PR_TRIPWIRE",
        title,
        Severity.CRITICAL,
        CheckStatus.PASS,
        "The force-PR-not-merge tripwire self-test passed.",
        fix,
    )


def _has_helper(result: subprocess.CompletedProcess[str]) -> bool:
    """Return whether a git-config probe found a non-blank helper.

    Args:
        result: Completed git-config probe.

    Returns:
        True when the command succeeded and emitted a non-blank line.
    """
    return result.returncode == 0 and any(
        line.strip() for line in result.stdout.splitlines()
    )


def _check_git_credential_helper(ctx: DoctorContext) -> CheckResult:
    """Check for a scoped or global git credential helper.

    Args:
        ctx: Injected doctor context.

    Returns:
        Git credential-helper presence result.
    """
    title = "Git credential helper configured"
    fix = "Run `gh auth setup-git` to configure a credential helper."
    keys = (
        "credential.https://github.com.helper",
        "credential.helper",
    )
    try:
        for key in keys:
            result = ctx.runner(["git", "config", "--get-all", key])
            if _has_helper(result):
                return _result(
                    "GIT_CRED_HELPER",
                    title,
                    Severity.CRITICAL,
                    CheckStatus.PASS,
                    "A git credential helper is configured.",
                    fix,
                )
    except Exception:  # noqa: BLE001
        pass
    return _result(
        "GIT_CRED_HELPER",
        title,
        Severity.CRITICAL,
        CheckStatus.FAIL,
        "No scoped or global git credential helper is configured.",
        fix,
    )


def _check_ruleset(
    ctx: DoctorContext, check_id: str, title: str
) -> CheckResult:
    """Check that both required repository rulesets match their definitions.

    Args:
        ctx: Injected doctor context.
        check_id: Catalog identifier for the ruleset result.
        title: Human-readable check title.

    Returns:
        PASS when both rulesets match, otherwise FAIL.
    """
    fix = "Run bin/provision-ruleset.sh to provision the required rulesets."
    try:
        config = _resolved_config(ctx)
    except sandbox_config.SandboxConfigError as exc:
        return _result(
            check_id,
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            str(exc),
            fix,
        )

    def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
        return ctx.runner(["gh", *args])

    status = ruleset_status.ruleset_is_provisioned(
        config.repo_owner,
        config.repo_name,
        app_id=config.github_app_id,
        runner=_run_gh,
    )
    if status is ruleset_status.RulesetStatus.MATCH:
        return _result(
            check_id,
            title,
            Severity.CRITICAL,
            CheckStatus.PASS,
            "Both required repository rulesets match their definitions.",
            fix,
        )
    return _result(
        check_id,
        title,
        Severity.CRITICAL,
        CheckStatus.FAIL,
        f"Required repository rulesets could not be verified ({status.name}).",
        fix,
    )


def _check_ruleset_main(ctx: DoctorContext) -> CheckResult:
    """Check the combined ruleset verdict under the main-ruleset ID."""
    return _check_ruleset(
        ctx, "RULESET_MAIN", "Main branch ruleset provisioned"
    )


def _check_ruleset_feature(ctx: DoctorContext) -> CheckResult:
    """Check the combined ruleset verdict under the feature-ruleset ID."""
    return _check_ruleset(
        ctx,
        "RULESET_FEATURE",
        "Feature branch ruleset provisioned",
    )


def _check_labels_present(ctx: DoctorContext) -> CheckResult:
    """Check that all labels required by the harness are present."""
    title = "Required repository labels present"
    fix = "Create every required harness label in the target repository."
    required = {
        "agent-ready",
        "agent-done",
        "agent-failed",
        "blocked",
        "agent-in-progress",
        "agent-merged",
    }
    try:
        config = _resolved_config(ctx)
    except sandbox_config.SandboxConfigError as exc:
        return _result(
            "LABELS_PRESENT",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            str(exc),
            fix,
        )
    result = ctx.runner(
        [
            "gh",
            "label",
            "list",
            "-R",
            f"{config.repo_owner}/{config.repo_name}",
            "--json",
            "name",
            "--jq",
            ".[].name",
        ]
    )
    if result.returncode != 0:
        return _result(
            "LABELS_PRESENT",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            "The repository label list could not be retrieved.",
            fix,
        )

    present = {
        line.strip() for line in result.stdout.splitlines() if line.strip()
    }
    missing = sorted(required - present)
    if missing:
        return _result(
            "LABELS_PRESENT",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            f"Missing required labels: {', '.join(missing)}.",
            fix,
        )
    return _result(
        "LABELS_PRESENT",
        title,
        Severity.CRITICAL,
        CheckStatus.PASS,
        "All required harness labels are present.",
        fix,
    )


def _check_gh_repo_admin(ctx: DoctorContext) -> CheckResult:
    """Report whether the repository has an admin collaborator."""
    title = "Repository admin collaborator present"
    fix = "Ensure the repository has at least one admin collaborator."
    try:
        config = _resolved_config(ctx)
    except sandbox_config.SandboxConfigError as exc:
        return _result(
            "GH_REPO_ADMIN",
            title,
            Severity.WARNING,
            CheckStatus.WARN,
            str(exc),
            fix,
        )
    try:
        result = ctx.runner(
            [
                "gh",
                "api",
                "repos/"
                f"{config.repo_owner}/{config.repo_name}"
                "/collaborators?permission=admin",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError("gh api returned a non-zero exit status")
        collaborators = json.loads(result.stdout)
        if not isinstance(collaborators, list):
            raise TypeError("gh api response was not an array")
        has_admin = any(
            isinstance(item, dict)
            and (
                item.get("role_name") == "admin"
                or (
                    isinstance(item.get("permissions"), dict)
                    and bool(item["permissions"].get("admin"))
                )
            )
            for item in collaborators
        )
    except Exception:  # noqa: BLE001
        return _result(
            "GH_REPO_ADMIN",
            title,
            Severity.WARNING,
            CheckStatus.WARN,
            "Repository admin collaborators could not be verified.",
            fix,
        )

    if has_admin:
        status = CheckStatus.PASS
        detail = "At least one repository admin collaborator is present."
    else:
        status = CheckStatus.WARN
        detail = "No repository admin collaborator was found."
    return _result(
        "GH_REPO_ADMIN", title, Severity.WARNING, status, detail, fix
    )


def _check_gh_auth(ctx: DoctorContext) -> CheckResult:
    """Check standalone GitHub CLI authentication status."""
    title = "GitHub CLI authentication valid"
    fix = "Run `gh auth login` to authenticate the GitHub CLI."
    result = ctx.runner(["gh", "auth", "status"])
    if result.returncode == 0:
        status = CheckStatus.PASS
        detail = "GitHub CLI authentication is valid."
    else:
        status = CheckStatus.FAIL
        detail = "GitHub CLI authentication is invalid."
    return _result("GH_AUTH", title, Severity.CRITICAL, status, detail, fix)


def _check_oauth_volume(ctx: DoctorContext) -> CheckResult:
    """Check that the Claude OAuth credential file can be opened."""
    title = "Claude OAuth credential file readable"
    fix = "Mount a readable Claude OAuth credential file before startup."
    path = Path(ctx.home_dir) / ".claude" / ".credentials.json"
    try:
        with open(path):  # noqa: PTH123
            pass
        status = CheckStatus.PASS
        detail = "The Claude OAuth credential file is present and readable."
    except OSError:
        status = CheckStatus.WARN
        detail = "The Claude OAuth credential file is absent or unreadable."
    return _result(
        "CRED_OAUTH_VOLUME",
        title,
        Severity.WARNING,
        status,
        detail,
        fix,
    )


def _check_vault_dryrun(ctx: DoctorContext) -> CheckResult:
    """Load the selected App key and prove it can sign without GitHub I/O.

    Args:
        ctx: Injected doctor context.

    Returns:
        PASS when the selected key signs an App JWT, otherwise safe FAIL.
    """
    title = "App private key is usable"
    fix = "Verify the selected App private-key source and its credentials."
    try:
        config, values = _resolved_private_key_context(ctx)
    except AppPrivateKeyConfigError as exc:
        return _result(
            "VAULT_PEM_DRYRUN",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            str(exc),
            fix,
        )
    app_id = values["BH_GITHUB_APP_ID"]
    try:
        private_key = load_app_private_key(
            config,
            bws_access_token=ctx.env.get("BWS_ACCESS_TOKEN", ""),
            fetch_secret=ctx.fetch_secret,
        )
        # Signing proves usability; discard the JWT without network access.
        app_auth.build_app_jwt(app_id, private_key, now=int(time.time()))
        status = CheckStatus.PASS
        detail = "App private key loaded successfully."
    except Exception:
        status = CheckStatus.FAIL
        detail = (
            f"{config.provider.value} provider App private key is unusable."
        )
    return _result(
        "VAULT_PEM_DRYRUN",
        title,
        Severity.CRITICAL,
        status,
        detail,
        fix,
    )


VAULT_PEM_DRYRUN_CHECK = Check(
    "VAULT_PEM_DRYRUN",
    "App private key is usable",
    Severity.CRITICAL,
    Phase.LIVE,
    False,
    "Verify the selected App private-key source and its credentials.",
    _check_vault_dryrun,
)


CATALOG: list[Check] = [
    Check(
        "PKG_PROVENANCE",
        "Runtime provenance is valid",
        Severity.CRITICAL,
        Phase.INSTALLATION,
        False,
        "Reinstall a verified baton-harness artifact.",
        _check_package_provenance,
    ),
    Check(
        "PKG_IMPORTS",
        "Runtime package imports succeed",
        Severity.CRITICAL,
        Phase.INSTALLATION,
        False,
        "Reinstall the baton-harness package and its runtime dependencies.",
        _check_package_imports,
    ),
    Check(
        "PKG_ENTRY_POINTS",
        "Installed entry points are complete",
        Severity.CRITICAL,
        Phase.INSTALLATION,
        False,
        "Reinstall a complete baton-harness artifact.",
        _check_package_entry_points,
    ),
    Check(
        "PKG_RESOURCES",
        "Packaged resources are complete",
        Severity.CRITICAL,
        Phase.INSTALLATION,
        False,
        "Reinstall a complete baton-harness artifact.",
        _check_package_resources,
    ),
    Check(
        "PKG_WORKFLOW",
        "Packaged workflow is valid",
        Severity.CRITICAL,
        Phase.INSTALLATION,
        False,
        "Reinstall a verified baton-harness artifact.",
        _check_package_workflow,
    ),
    Check(
        "FORCE_PR_TRIPWIRE",
        "Force-PR-not-merge tripwire passes",
        Severity.CRITICAL,
        Phase.INSTALLATION,
        False,
        "Restore the force-pr-not-merge hook and its startup self-test.",
        _check_force_pr_tripwire,
    ),
    Check(
        "CLI_GH",
        "GitHub CLI available",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Install gh and ensure it is on PATH.",
        _check_cli_gh,
    ),
    Check(
        "CLI_BWS",
        "Bitwarden Secrets CLI available",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Install bws and ensure it is on PATH.",
        _check_cli_bws,
    ),
    Check(
        "CLI_CLAUDE",
        "Claude CLI available",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Install claude and ensure it is on PATH.",
        _check_cli_claude,
    ),
    Check(
        "CLI_UV",
        "uv package manager available",
        Severity.WARNING,
        Phase.CONFIGURATION,
        False,
        "Install uv and ensure it is on PATH.",
        _check_cli_uv,
    ),
    Check(
        "ENV_PROJECT_ROOT",
        "Project root is valid",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Set BH_PROJECT_ROOT to an existing directory.",
        _check_project_root,
    ),
    Check(
        "ENV_HOST_ENV",
        "Host environment file present",
        Severity.WARNING,
        Phase.CONFIGURATION,
        False,
        "Create ~/.config/baton-harness/host.env if it is needed.",
        _check_host_env,
    ),
    Check(
        "CFG_CONFIG_ENV",
        "Sandbox config file present",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Create .bh/config.env in BH_PROJECT_ROOT.",
        _check_config_env,
    ),
    Check(
        "CFG_REQUIRED_KEYS",
        "Required sandbox config keys valid",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Set all required .bh/config.env keys to valid values.",
        _check_required_keys,
    ),
    Check(
        "CFG_OPTIONAL_SECRET_IDS",
        "Optional secret IDs valid",
        Severity.WARNING,
        Phase.CONFIGURATION,
        False,
        "Use UUID values for optional BWS secret ID settings.",
        _check_optional_secret_ids,
    ),
    Check(
        "ENV_BWS_ACCESS_TOKEN",
        "BWS access token present",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Set BWS_ACCESS_TOKEN to a non-empty access token.",
        _check_bws_access_token,
    ),
    Check(
        "GITIGNORE_SYMPHONY",
        "Symphony state is gitignored",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        False,
        "Add an exact .symphony/ line to the repository .gitignore.",
        _check_gitignore_symphony,
    ),
    Check(
        "CRED_ANTHROPIC_UNSET",
        "Anthropic API key is unset",
        Severity.CRITICAL,
        Phase.CONFIGURATION,
        True,
        "Unset ANTHROPIC_API_KEY and use mounted OAuth credentials.",
        _check_anthropic_unset,
    ),
    Check(
        "GIT_CRED_HELPER",
        "Git credential helper configured",
        Severity.CRITICAL,
        Phase.LIVE,
        True,
        "Run `gh auth setup-git` to configure a credential helper.",
        _check_git_credential_helper,
    ),
    Check(
        "RULESET_MAIN",
        "Main branch ruleset provisioned",
        Severity.CRITICAL,
        Phase.LIVE,
        False,
        "Run bin/provision-ruleset.sh to provision the required rulesets.",
        _check_ruleset_main,
    ),
    Check(
        "RULESET_FEATURE",
        "Feature branch ruleset provisioned",
        Severity.CRITICAL,
        Phase.LIVE,
        False,
        "Run bin/provision-ruleset.sh to provision the required rulesets.",
        _check_ruleset_feature,
    ),
    Check(
        "LABELS_PRESENT",
        "Required repository labels present",
        Severity.CRITICAL,
        Phase.LIVE,
        False,
        "Create every required harness label in the target repository.",
        _check_labels_present,
    ),
    Check(
        "GH_REPO_ADMIN",
        "Repository admin collaborator present",
        Severity.WARNING,
        Phase.LIVE,
        False,
        "Ensure the repository has at least one admin collaborator.",
        _check_gh_repo_admin,
    ),
    Check(
        "GH_AUTH",
        "GitHub CLI authentication valid",
        Severity.CRITICAL,
        Phase.LIVE,
        True,
        "Run `gh auth login` to authenticate the GitHub CLI.",
        _check_gh_auth,
    ),
    Check(
        "CRED_OAUTH_VOLUME",
        "Claude OAuth credential file readable",
        Severity.WARNING,
        Phase.LIVE,
        True,
        "Mount a readable Claude OAuth credential file before startup.",
        _check_oauth_volume,
    ),
    VAULT_PEM_DRYRUN_CHECK,
]


def _run_check(check: Check, ctx: DoctorContext) -> CheckResult:
    """Run one check and synthesize a failure if it raises.

    Args:
        check: Catalog check to execute.
        ctx: Injected doctor context.

    Returns:
        The check result or a synthesized FAIL result.
    """
    try:
        outcome = check(ctx)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            check_id=check.check_id,
            phase=check.phase,
            title=check.title,
            severity=check.severity,
            status=CheckStatus.FAIL,
            detail=repr(exc),
            remediation=check.fix,
        )
    return CheckResult(
        check_id=check.check_id,
        phase=check.phase,
        title=check.title,
        severity=check.severity,
        status=outcome.status,
        detail=outcome.detail,
        remediation=check.fix,
    )


def run_report(
    ctx: DoctorContext,
    phases: Sequence[Phase] | None = None,
    checks: Sequence[Check] | None = None,
) -> list[CheckResult]:
    """Run checks in selected phase and catalog order without aborting early.

    Args:
        ctx: Injected doctor context.
        phases: Selected phases, defaulting to every public phase.
        checks: Optional catalog override used by focused callers and tests.

    Returns:
        One result for every selected catalog check.
    """
    selected_phases = dict.fromkeys(Phase if phases is None else phases)
    catalog = CATALOG if checks is None else checks
    return [
        _run_check(check, ctx)
        for phase in selected_phases
        for check in catalog
        if check.phase is phase
    ]


def run_gate(
    ctx: DoctorContext,
    phases: Sequence[Phase],
    checks: Sequence[Check] | None = None,
) -> list[CheckResult]:
    """Collect selected checks and raise once on critical failures.

    Args:
        ctx: Injected doctor context.
        phases: Phases whose checks should run.
        checks: Optional catalog override used by focused callers and tests.

    Returns:
        The complete selected result list when no critical check fails.

    Raises:
        DoctorGateError: After collection if any critical check failed.
    """
    results = run_report(ctx, phases, checks=checks)
    if any(
        result.status is CheckStatus.FAIL
        and result.severity is Severity.CRITICAL
        for result in results
    ):
        raise DoctorGateError(results)
    return results
