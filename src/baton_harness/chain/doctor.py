"""Preflight readiness checks for the Baton harness daemon."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from baton_harness.chain import ruleset_status

RunFn = Callable[..., subprocess.CompletedProcess[str]]
FetchSecretFn = Callable[..., str]
RunnerFn = Callable[[list[str]], subprocess.CompletedProcess[str]]
WhichFn = Callable[[str], str | None]
CheckFn = Callable[["DoctorContext"], "CheckResult"]

_LINE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}$"
)
_REQUIRED_KEYS = (
    "BH_REPO_OWNER",
    "BH_REPO_NAME",
    "BH_GITHUB_APP_ID",
    "BH_GITHUB_APP_INSTALLATION_ID",
    "BWS_PEM_SECRET_ID",
)
_OPTIONAL_SECRET_IDS = (
    "BWS_GH_TOKEN_SECRET_ID",
    "BWS_HEARTBEAT_PING_URL_SECRET_ID",
)


class Severity(Enum):
    """Severity assigned to a preflight check."""

    CRITICAL = auto()
    WARNING = auto()


class CheckStatus(Enum):
    """Outcome of a preflight check."""

    PASS = auto()
    FAIL = auto()
    WARN = auto()
    SKIP = auto()


class Phase(Enum):
    """Daemon startup phase in which a check applies."""

    PRE_BOOTSTRAP = auto()
    POST_BOOTSTRAP = auto()


@dataclass
class CheckResult:
    """Result returned by a preflight check.

    Attributes:
        check_id: Stable identifier for the check.
        title: Human-readable check title.
        severity: Operational severity of a failure.
        status: Outcome of the check.
        detail: Secret-safe explanation of the outcome.
        fix: Secret-safe remediation guidance.
    """

    check_id: str
    title: str
    severity: Severity
    status: CheckStatus
    detail: str
    fix: str


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
    """

    project_root: str
    home_dir: str
    env: dict[str, str]
    which: WhichFn
    runner: RunnerFn
    run: RunFn
    fetch_secret: FetchSecretFn
    installation_token: str = ""


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


def _result(
    check_id: str,
    title: str,
    severity: Severity,
    status: CheckStatus,
    detail: str,
    fix: str,
) -> CheckResult:
    """Build a check result from secret-safe values.

    Args:
        check_id: Stable identifier for the check.
        title: Human-readable check title.
        severity: Operational severity of a failure.
        status: Outcome of the check.
        detail: Secret-safe explanation of the outcome.
        fix: Secret-safe remediation guidance.

    Returns:
        A populated check result.
    """
    return CheckResult(check_id, title, severity, status, detail, fix)


def _parse_config(path: Path) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` config lines without external I/O.

    Args:
        path: Config file to parse.

    Returns:
        Parsed key-value pairs. Malformed non-comment lines are ignored.
    """
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match is None:
            continue
        key, value = match.groups()
        if (
            len(value) >= 2
            and value[0] in {"'", '"'}
            and value[-1] == value[0]
        ):
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _is_valid(key: str, value: str) -> bool:
    """Apply the sandbox config shape rule for a known key.

    Args:
        key: Config key name.
        value: Candidate config value.

    Returns:
        True when the value has the required shape.
    """
    if key in {"BH_REPO_OWNER", "BH_REPO_NAME"}:
        return bool(value) and _REPO_PART_RE.fullmatch(value) is not None
    if key in {"BH_GITHUB_APP_ID", "BH_GITHUB_APP_INSTALLATION_ID"}:
        return value.isdigit() and int(value) > 0
    if key == "BWS_PEM_SECRET_ID":
        return _UUID_RE.fullmatch(value) is not None
    if key in _OPTIONAL_SECRET_IDS:
        return not value or _UUID_RE.fullmatch(value) is not None
    return True


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
    return _cli_result(
        ctx,
        check_id="CLI_BWS",
        title="Bitwarden Secrets CLI available",
        binary="bws",
        severity=Severity.CRITICAL,
        missing_status=CheckStatus.FAIL,
        fix="Install bws and ensure it is on PATH.",
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
    path = Path(ctx.project_root) / ".bh" / "config.env"
    if path.exists():
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
    path = Path(ctx.project_root) / ".bh" / "config.env"
    if not path.exists():
        return _result(
            "CFG_REQUIRED_KEYS",
            title,
            Severity.CRITICAL,
            CheckStatus.FAIL,
            ".bh/config.env is missing, so required keys cannot be checked.",
            fix,
        )
    parsed = _parse_config(path)
    for key in _REQUIRED_KEYS:
        if key not in parsed:
            return _result(
                "CFG_REQUIRED_KEYS",
                title,
                Severity.CRITICAL,
                CheckStatus.FAIL,
                f"Required config key {key} is missing.",
                fix,
            )
        if not _is_valid(key, parsed[key]):
            return _result(
                "CFG_REQUIRED_KEYS",
                title,
                Severity.CRITICAL,
                CheckStatus.FAIL,
                f"Required config key {key} is malformed.",
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
    path = Path(ctx.project_root) / ".bh" / "config.env"
    if not path.exists():
        return _result(
            "CFG_OPTIONAL_SECRET_IDS",
            title,
            Severity.WARNING,
            CheckStatus.SKIP,
            ".bh/config.env is missing; optional IDs are not applicable.",
            fix,
        )
    parsed = _parse_config(path)
    for key in _OPTIONAL_SECRET_IDS:
        if key in parsed and not _is_valid(key, parsed[key]):
            return _result(
                "CFG_OPTIONAL_SECRET_IDS",
                title,
                Severity.WARNING,
                CheckStatus.WARN,
                f"Optional config key {key} is malformed.",
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
    token = ctx.env.get("BWS_ACCESS_TOKEN", "")
    if token:
        status = CheckStatus.PASS
        detail = f"BWS_ACCESS_TOKEN is set ({len(token)} characters)."
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
    owner = ctx.env["BH_REPO_OWNER"]
    repo = ctx.env["BH_REPO_NAME"]
    app_id = ctx.env["BH_GITHUB_APP_ID"]

    def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
        return ctx.runner(["gh", *args])

    status = ruleset_status.ruleset_is_provisioned(
        owner,
        repo,
        app_id=app_id,
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
        "blocked",
        "agent-in-progress",
        "agent-merged",
    }
    owner = ctx.env["BH_REPO_OWNER"]
    repo = ctx.env["BH_REPO_NAME"]
    result = ctx.runner(
        [
            "gh",
            "label",
            "list",
            "-R",
            f"{owner}/{repo}",
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
    owner = ctx.env["BH_REPO_OWNER"]
    repo = ctx.env["BH_REPO_NAME"]
    try:
        result = ctx.runner(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/collaborators?permission=admin",
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


CATALOG: list[Check] = [
    Check(
        "CLI_GH",
        "GitHub CLI available",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Install gh and ensure it is on PATH.",
        _check_cli_gh,
    ),
    Check(
        "CLI_BWS",
        "Bitwarden Secrets CLI available",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Install bws and ensure it is on PATH.",
        _check_cli_bws,
    ),
    Check(
        "CLI_CLAUDE",
        "Claude CLI available",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Install claude and ensure it is on PATH.",
        _check_cli_claude,
    ),
    Check(
        "CLI_UV",
        "uv package manager available",
        Severity.WARNING,
        Phase.PRE_BOOTSTRAP,
        False,
        "Install uv and ensure it is on PATH.",
        _check_cli_uv,
    ),
    Check(
        "ENV_PROJECT_ROOT",
        "Project root is valid",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Set BH_PROJECT_ROOT to an existing directory.",
        _check_project_root,
    ),
    Check(
        "ENV_HOST_ENV",
        "Host environment file present",
        Severity.WARNING,
        Phase.PRE_BOOTSTRAP,
        False,
        "Create ~/.config/baton-harness/host.env if it is needed.",
        _check_host_env,
    ),
    Check(
        "CFG_CONFIG_ENV",
        "Sandbox config file present",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Create .bh/config.env in BH_PROJECT_ROOT.",
        _check_config_env,
    ),
    Check(
        "CFG_REQUIRED_KEYS",
        "Required sandbox config keys valid",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Set all required .bh/config.env keys to valid values.",
        _check_required_keys,
    ),
    Check(
        "CFG_OPTIONAL_SECRET_IDS",
        "Optional secret IDs valid",
        Severity.WARNING,
        Phase.PRE_BOOTSTRAP,
        False,
        "Use UUID values for optional BWS secret ID settings.",
        _check_optional_secret_ids,
    ),
    Check(
        "ENV_BWS_ACCESS_TOKEN",
        "BWS access token present",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Set BWS_ACCESS_TOKEN to a non-empty access token.",
        _check_bws_access_token,
    ),
    Check(
        "GITIGNORE_SYMPHONY",
        "Symphony state is gitignored",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        False,
        "Add an exact .symphony/ line to the repository .gitignore.",
        _check_gitignore_symphony,
    ),
    Check(
        "CRED_ANTHROPIC_UNSET",
        "Anthropic API key is unset",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        True,
        "Unset ANTHROPIC_API_KEY and use mounted OAuth credentials.",
        _check_anthropic_unset,
    ),
    Check(
        "FORCE_PR_TRIPWIRE",
        "Force-PR-not-merge tripwire passes",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        True,
        "Restore the force-pr-not-merge hook and its startup self-test.",
        _check_force_pr_tripwire,
    ),
    Check(
        "GIT_CRED_HELPER",
        "Git credential helper configured",
        Severity.CRITICAL,
        Phase.PRE_BOOTSTRAP,
        True,
        "Run `gh auth setup-git` to configure a credential helper.",
        _check_git_credential_helper,
    ),
    Check(
        "RULESET_MAIN",
        "Main branch ruleset provisioned",
        Severity.CRITICAL,
        Phase.POST_BOOTSTRAP,
        False,
        "Run bin/provision-ruleset.sh to provision the required rulesets.",
        _check_ruleset_main,
    ),
    Check(
        "RULESET_FEATURE",
        "Feature branch ruleset provisioned",
        Severity.CRITICAL,
        Phase.POST_BOOTSTRAP,
        False,
        "Run bin/provision-ruleset.sh to provision the required rulesets.",
        _check_ruleset_feature,
    ),
    Check(
        "LABELS_PRESENT",
        "Required repository labels present",
        Severity.CRITICAL,
        Phase.POST_BOOTSTRAP,
        False,
        "Create every required harness label in the target repository.",
        _check_labels_present,
    ),
    Check(
        "GH_REPO_ADMIN",
        "Repository admin collaborator present",
        Severity.WARNING,
        Phase.POST_BOOTSTRAP,
        False,
        "Ensure the repository has at least one admin collaborator.",
        _check_gh_repo_admin,
    ),
    Check(
        "GH_AUTH",
        "GitHub CLI authentication valid",
        Severity.CRITICAL,
        Phase.POST_BOOTSTRAP,
        True,
        "Run `gh auth login` to authenticate the GitHub CLI.",
        _check_gh_auth,
    ),
    Check(
        "CRED_OAUTH_VOLUME",
        "Claude OAuth credential file readable",
        Severity.WARNING,
        Phase.POST_BOOTSTRAP,
        True,
        "Mount a readable Claude OAuth credential file before startup.",
        _check_oauth_volume,
    ),
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
        return check(ctx)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            check_id=check.check_id,
            title=check.title,
            severity=check.severity,
            status=CheckStatus.FAIL,
            detail=repr(exc),
            fix=check.fix,
        )


def run_report(ctx: DoctorContext) -> list[CheckResult]:
    """Run every catalog check in order without aborting early.

    Args:
        ctx: Injected doctor context.

    Returns:
        One result for every catalog check.
    """
    return [_run_check(check, ctx) for check in CATALOG]


def run_gate(ctx: DoctorContext, phase: Phase) -> None:
    """Run non-native checks for one phase and fail on critical errors.

    Args:
        ctx: Injected doctor context.
        phase: Startup phase whose checks should run.

    Raises:
        SystemExit: With code 1 on the first critical failed check.
    """
    for check in CATALOG:
        if check.phase is not phase or check.daemon_native:
            continue
        result = _run_check(check, ctx)
        if (
            result.status is CheckStatus.FAIL
            and result.severity is Severity.CRITICAL
        ):
            print(
                f"Preflight check {result.check_id} failed: "
                f"{result.detail} Fix: {result.fix}",
                file=sys.stderr,
            )
            raise SystemExit(1)
