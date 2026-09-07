"""Sandbox config reader and validator for ``.bh/config.env`` files.

Provides pure configuration resolution and explicit effect boundaries for
``.bh/config.env``. ``read_and_validate`` retains the legacy composition:
it resolves against ``os.environ``, confirms the target repository through
``gh api``, then applies the result to ``os.environ``.

The subprocess call is injected via the ``run`` parameter so callers
control the transport layer in tests — no real ``gh`` binary is
invoked during unit tests.

Environment overrides
----------------------
For each overridable config key, a non-empty pre-existing ``os.environ``
value takes precedence over the value parsed from the file (an empty
string is treated as absent). The resolved value — whichever source it
came from — is validated with the same per-key rules. The derived
``BWS_APP_ID`` / ``BWS_INSTALLATION_ID`` twins always follow the
*resolved* ``BH_GITHUB_APP_ID`` / ``BH_GITHUB_APP_INSTALLATION_ID`` and
are not independently overridable via their own env vars.

Fail-closed semantics
---------------------
Every failure path raises ``SandboxConfigError`` rather than returning a
partial or empty result:

- Missing config file raises before any parsing or subprocess call.
- Missing required keys raise after env-override resolution completes.
- Invalid values raise with the line number and offending value.
- Non-zero ``gh api`` validation raises with the target repo slug.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from baton_harness.chain.app_private_key import (
    AppPrivateKeyConfigError,
    AppPrivateKeyProvider,
    resolve_app_private_key_config,
)
from baton_harness.chain.identity import Identity, env_for

# ---------------------------------------------------------------------------
# Type alias for the injectable run callable
# ---------------------------------------------------------------------------

#: Type of the injected subprocess runner. Signature: ``(args, **kwargs)``.
RunFn = Callable[..., subprocess.CompletedProcess[str]]

_LINE_RE = re.compile(r"^(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}$"
)
_IGNORED_KEYS = {"BWS_APP_ID", "BWS_INSTALLATION_ID"}
_REQUIRED_KEYS = (
    "BH_REPO_OWNER",
    "BH_REPO_NAME",
    "BH_GITHUB_APP_ID",
    "BH_GITHUB_APP_INSTALLATION_ID",
    "BH_GITHUB_APP_KEY_PROVIDER",
)
#: Config keys eligible for an ``os.environ`` override. A non-empty
#: pre-existing env var wins over the file's value for each of these.
_ENV_OVERRIDABLE_KEYS = (
    "BH_REPO_OWNER",
    "BH_REPO_NAME",
    "BH_GITHUB_APP_ID",
    "BH_GITHUB_APP_INSTALLATION_ID",
    "BH_GITHUB_APP_KEY_PROVIDER",
    "BWS_PEM_SECRET_ID",
    "BH_GITHUB_APP_PRIVATE_KEY_FILE",
    "BWS_GH_TOKEN_SECRET_ID",
    "BWS_HEARTBEAT_PING_URL_SECRET_ID",
)
_VALUE_VALIDATED_KEYS = (
    "BH_REPO_OWNER",
    "BH_REPO_NAME",
    "BH_GITHUB_APP_ID",
    "BH_GITHUB_APP_INSTALLATION_ID",
    "BWS_GH_TOKEN_SECRET_ID",
    "BWS_HEARTBEAT_PING_URL_SECRET_ID",
)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SandboxConfigError(RuntimeError):
    """Raised when sandbox config parsing or validation fails.

    Attributes:
        message: Human-readable description of the failure.
    """

    def __init__(self, message: str) -> None:
        """Initialise with a human-readable failure description.

        Args:
            message: Describes what went wrong while reading, validating,
                or network-checking the sandbox config.
        """
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxConfig:
    """Validated sandbox configuration loaded from ``.bh/config.env``.

    Attributes:
        repo_owner: GitHub repository owner.
        repo_name: GitHub repository name.
        github_app_id: GitHub App numeric ID as a string.
        github_app_installation_id: GitHub App installation numeric ID
            as a string.
        github_app_key_provider: Selected GitHub App private-key provider.
        bws_pem_secret_id: Bitwarden Secrets UUID for the GitHub App PEM,
            when the BWS provider is selected.
        github_app_private_key_file: Absolute GitHub App PEM path, when
            the file provider is selected.
        bws_gh_token_secret_id: Optional Bitwarden Secrets UUID for a
            GitHub token secret.
        bws_heartbeat_ping_url_secret_id: Optional Bitwarden Secrets UUID
            for a heartbeat webhook secret.
    """

    repo_owner: str
    repo_name: str
    github_app_id: str
    github_app_installation_id: str
    github_app_key_provider: AppPrivateKeyProvider
    bws_pem_secret_id: str | None
    github_app_private_key_file: Path | None
    bws_gh_token_secret_id: str = ""
    bws_heartbeat_ping_url_secret_id: str = ""


# ---------------------------------------------------------------------------
# Default run implementation
# ---------------------------------------------------------------------------


def _default_run(
    args: list[str],
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Invoke a subprocess and return the completed process.

    Wraps ``subprocess.run`` with ``capture_output=True`` and
    ``text=True`` (UTF-8) so stdout/stderr are available as strings.
    Unknown keyword arguments are ignored so test stubs with extra kwargs
    still work against this default.

    Args:
        args: Command and arguments list.
        **_kwargs: Additional keyword arguments accepted for signature
            compatibility and ignored by the default implementation.

    Returns:
        A ``subprocess.CompletedProcess[str]`` with captured
        ``stdout``, ``stderr``, and ``returncode``.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env_for(Identity.WORKER),
    )


# ---------------------------------------------------------------------------
# Per-key validation — shared by file-sourced and env-sourced values
# ---------------------------------------------------------------------------


def _is_valid(key: str, value: str) -> bool:
    """Check whether ``value`` satisfies the format rule for ``key``.

    Applies the same rule regardless of whether ``value`` came from the
    config file or from an ``os.environ`` override, so a resolved value
    is validated identically no matter its source.

    Args:
        key: Config key name (e.g. ``"BH_GITHUB_APP_ID"``).
        value: Candidate value to check.

    Returns:
        True if ``value`` is valid for ``key``. Keys outside the
        recognized set are always considered valid (unvalidated, as
        before). The two optional secret-ID keys accept an empty
        string as valid (not-configured).
    """
    if key in {"BH_REPO_OWNER", "BH_REPO_NAME"}:
        return bool(value) and _REPO_PART_RE.fullmatch(value) is not None
    if key in {"BH_GITHUB_APP_ID", "BH_GITHUB_APP_INSTALLATION_ID"}:
        return value.isdigit() and int(value) > 0
    if key in {"BWS_GH_TOKEN_SECRET_ID", "BWS_HEARTBEAT_PING_URL_SECRET_ID"}:
        return not value or _UUID_RE.fullmatch(value) is not None
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_overridable_keys(
    parsed: Mapping[str, str],
    env: Mapping[str, str],
    keys: tuple[str, ...],
) -> dict[str, str]:
    """Resolve config values that may be overridden by the environment.

    For each key, a non-empty environment value wins over the parsed
    file value. An empty or missing environment value falls back to the
    parsed value, which may itself be absent or empty.

    Args:
        parsed: Values parsed from the config file.
        env: Environment values eligible to override the file values.
        keys: Config keys to resolve.

    Returns:
        A fresh dictionary containing one resolved value for each key.
    """
    resolved: dict[str, str] = {}
    for key in keys:
        env_value = env.get(key, "")
        resolved[key] = env_value if env_value else parsed.get(key, "")
    return resolved


def select_config_path(
    explicit: str | None,
    env: Mapping[str, str],
) -> Path:
    """Select an explicit or project-root-relative sandbox config path.

    Args:
        explicit: Optional explicit config-file path.
        env: Environment from which to read ``BH_PROJECT_ROOT``.

    Returns:
        The resolved config-file path.

    Raises:
        SandboxConfigError: If no explicit path or project root is supplied.
    """
    if explicit:
        return Path(explicit).resolve()
    project_root = env.get("BH_PROJECT_ROOT", "")
    if not project_root:
        raise SandboxConfigError(
            "BH_PROJECT_ROOT is required when --config is not supplied"
        )
    return (Path(project_root) / ".bh" / "config.env").resolve()


def resolve_config(
    path: Path,
    env: Mapping[str, str],
) -> SandboxConfig:
    """Parse and validate sandbox config without external effects.

    Args:
        path: Path to the ``config.env`` file.
        env: Environment values eligible to override file values.

    Returns:
        A validated ``SandboxConfig`` instance.

    Raises:
        SandboxConfigError: If the file is missing, malformed, invalid, or
            omits a required key.
    """
    file_path = os.fspath(path)
    try:
        with open(file_path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError as exc:
        raise SandboxConfigError(
            f"sandbox config file does not exist: {file_path}"
        ) from exc

    parsed: dict[str, str] = {}
    parsed_line_numbers: dict[str, int] = {}

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = _LINE_RE.match(line)
        if match is None:
            raise SandboxConfigError(
                f"invalid sandbox config line {line_number}: {line!r}"
            )

        key, value = match.groups()
        if (
            len(value) >= 2
            and value[0] in {"'", '"'}
            and value[-1] == value[0]
        ):
            value = value[1:-1]

        if key in _IGNORED_KEYS:
            continue

        parsed[key] = value
        parsed_line_numbers[key] = line_number

    # Resolve each overridable key: a non-empty environment value wins
    # over the file's value (empty env is treated as absent). The
    # completely resolved base and optional values are validated below.
    resolved = resolve_overridable_keys(parsed, env, _ENV_OVERRIDABLE_KEYS)

    for required_key in _REQUIRED_KEYS:
        if not resolved.get(required_key):
            raise SandboxConfigError(f"missing required key: {required_key}")

    for key in _VALUE_VALIDATED_KEYS:
        value = resolved[key]
        if _is_valid(key, value):
            continue

        env_value = env.get(key, "")
        if env_value:
            raise SandboxConfigError(
                f"{key} invalid (from environment variable): {value!r}"
            )

        source_line_number = parsed_line_numbers.get(key)
        if source_line_number is None:
            raise SandboxConfigError(f"{key} invalid: {value!r}")
        raise SandboxConfigError(
            f"{key} invalid at line {source_line_number}: {value!r}"
        )

    try:
        app_key_config = resolve_app_private_key_config(resolved)
    except AppPrivateKeyConfigError as exc:
        raise SandboxConfigError(str(exc)) from exc

    return SandboxConfig(
        repo_owner=resolved["BH_REPO_OWNER"],
        repo_name=resolved["BH_REPO_NAME"],
        github_app_id=resolved["BH_GITHUB_APP_ID"],
        github_app_installation_id=resolved["BH_GITHUB_APP_INSTALLATION_ID"],
        github_app_key_provider=app_key_config.provider,
        bws_pem_secret_id=app_key_config.bws_secret_id,
        github_app_private_key_file=app_key_config.file_path,
        bws_gh_token_secret_id=resolved["BWS_GH_TOKEN_SECRET_ID"],
        bws_heartbeat_ping_url_secret_id=resolved[
            "BWS_HEARTBEAT_PING_URL_SECRET_ID"
        ],
    )


def validate_repository(config: SandboxConfig, run: RunFn) -> None:
    """Confirm that a resolved sandbox repository exists.

    Args:
        config: Fully resolved sandbox configuration.
        run: Injected subprocess runner for the GitHub API probe.

    Raises:
        SandboxConfigError: If the repository validation command fails.
    """
    gh_result = run(
        [
            "gh",
            "api",
            f"repos/{config.repo_owner}/{config.repo_name}",
            "--jq",
            ".id",
        ]
    )
    if gh_result.returncode != 0:
        raise SandboxConfigError(
            "sandbox repo validation failed for "
            f"{config.repo_owner}/{config.repo_name}"
        )


def apply_config(
    config: SandboxConfig,
    env: MutableMapping[str, str],
) -> None:
    """Apply a resolved sandbox configuration to a mutable environment.

    Args:
        config: Fully resolved sandbox configuration.
        env: Target environment to populate with selected configuration.
    """
    env["BH_REPO_OWNER"] = config.repo_owner
    env["BH_REPO_NAME"] = config.repo_name
    env["BH_GITHUB_APP_ID"] = config.github_app_id
    env["BH_GITHUB_APP_INSTALLATION_ID"] = config.github_app_installation_id
    env["BH_GITHUB_APP_KEY_PROVIDER"] = config.github_app_key_provider.value
    if config.github_app_key_provider is AppPrivateKeyProvider.BWS:
        assert config.bws_pem_secret_id is not None
        env["BWS_PEM_SECRET_ID"] = config.bws_pem_secret_id
        env.pop("BH_GITHUB_APP_PRIVATE_KEY_FILE", None)
    else:
        assert config.github_app_private_key_file is not None
        env["BH_GITHUB_APP_PRIVATE_KEY_FILE"] = str(
            config.github_app_private_key_file
        )
        env.pop("BWS_PEM_SECRET_ID", None)
    env["BWS_GH_TOKEN_SECRET_ID"] = config.bws_gh_token_secret_id
    env["BWS_HEARTBEAT_PING_URL_SECRET_ID"] = (
        config.bws_heartbeat_ping_url_secret_id
    )
    env["BWS_APP_ID"] = config.github_app_id
    env["BWS_INSTALLATION_ID"] = config.github_app_installation_id


def read_and_validate(
    path: str | os.PathLike[str],
    *,
    run: RunFn = _default_run,
) -> SandboxConfig:
    """Resolve, validate remotely, and apply sandbox config compatibly.

    Args:
        path: Path to the ``config.env`` file.
        run: Injected subprocess runner used for the ``gh api`` repo
            existence check.

    Returns:
        A validated ``SandboxConfig`` instance.

    Raises:
        SandboxConfigError: If resolution or repository validation fails.
    """
    config = resolve_config(Path(path), os.environ)
    validate_repository(config, run)
    apply_config(config, os.environ)
    return config
