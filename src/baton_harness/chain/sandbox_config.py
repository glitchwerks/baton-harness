"""Sandbox config reader and validator for ``.bh/config.env`` files.

Provides a single public function, ``read_and_validate``, that reads a
``KEY=VALUE`` environment file, validates the required sandbox settings,
confirms the target repository exists via ``gh api``, then populates
``os.environ`` with both the parsed keys and the derived ``BWS_*`` twins.

The subprocess call is injected via the ``run`` parameter so callers
control the transport layer in tests — no real ``gh`` binary is
invoked during unit tests.

Fail-closed semantics
---------------------
Every failure path raises ``SandboxConfigError`` rather than returning a
partial or empty result:

- Missing config file raises before any parsing or subprocess call.
- Missing required keys raise after parsing completes.
- Invalid values raise with the line number and offending value.
- Non-zero ``gh api`` validation raises with the target repo slug.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Type alias for the injectable run callable
# ---------------------------------------------------------------------------

#: Type of the injected subprocess runner. Signature: ``(args, **kwargs)``.
RunFn = Callable[..., subprocess.CompletedProcess[str]]

_LINE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
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
    "BWS_PEM_SECRET_ID",
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


@dataclass
class SandboxConfig:
    """Validated sandbox configuration loaded from ``.bh/config.env``.

    Attributes:
        repo_owner: GitHub repository owner.
        repo_name: GitHub repository name.
        github_app_id: GitHub App numeric ID as a string.
        github_app_installation_id: GitHub App installation numeric ID
            as a string.
        bws_pem_secret_id: Bitwarden Secrets UUID for the GitHub App PEM.
        bws_gh_token_secret_id: Optional Bitwarden Secrets UUID for a
            GitHub token secret.
        bws_heartbeat_ping_url_secret_id: Optional Bitwarden Secrets UUID
            for a heartbeat webhook secret.
    """

    repo_owner: str
    repo_name: str
    github_app_id: str
    github_app_installation_id: str
    bws_pem_secret_id: str
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
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_and_validate(
    path: str | os.PathLike[str],
    *,
    run: RunFn = _default_run,
) -> SandboxConfig:
    """Read, validate, and export sandbox config from ``.bh/config.env``.

    Args:
        path: Path to the ``config.env`` file.
        run: Injected subprocess runner used for the ``gh api`` repo
            existence check.

    Returns:
        A validated ``SandboxConfig`` instance.

    Raises:
        SandboxConfigError: If the file is missing, malformed, contains
            invalid values, omits a required key, or the repo check fails.
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

        if key in {"BH_REPO_OWNER", "BH_REPO_NAME"}:
            if not value or _REPO_PART_RE.fullmatch(value) is None:
                raise SandboxConfigError(
                    f"{key} invalid at line {line_number}: {value!r}"
                )
        elif key in {
            "BH_GITHUB_APP_ID",
            "BH_GITHUB_APP_INSTALLATION_ID",
        }:
            if not value.isdigit() or int(value) <= 0:
                raise SandboxConfigError(
                    f"{key} invalid at line {line_number}: {value!r}"
                )
        elif key == "BWS_PEM_SECRET_ID":
            if _UUID_RE.fullmatch(value) is None:
                raise SandboxConfigError(
                    f"{key} invalid at line {line_number}: {value!r}"
                )
        elif key in {
            "BWS_GH_TOKEN_SECRET_ID",
            "BWS_HEARTBEAT_PING_URL_SECRET_ID",
        }:
            if value and _UUID_RE.fullmatch(value) is None:
                raise SandboxConfigError(
                    f"{key} invalid at line {line_number}: {value!r}"
                )

        parsed[key] = value

    for required_key in _REQUIRED_KEYS:
        if required_key not in parsed:
            raise SandboxConfigError(f"missing required key: {required_key}")

    owner = parsed["BH_REPO_OWNER"]
    repo = parsed["BH_REPO_NAME"]
    gh_result = run(["gh", "api", f"repos/{owner}/{repo}", "--jq", ".id"])
    if gh_result.returncode != 0:
        raise SandboxConfigError(
            f"sandbox repo validation failed for {owner}/{repo}"
        )

    os.environ["BH_REPO_OWNER"] = owner
    os.environ["BH_REPO_NAME"] = repo
    os.environ["BH_GITHUB_APP_ID"] = parsed["BH_GITHUB_APP_ID"]
    os.environ["BH_GITHUB_APP_INSTALLATION_ID"] = parsed[
        "BH_GITHUB_APP_INSTALLATION_ID"
    ]
    os.environ["BWS_PEM_SECRET_ID"] = parsed["BWS_PEM_SECRET_ID"]
    os.environ["BWS_GH_TOKEN_SECRET_ID"] = parsed.get(
        "BWS_GH_TOKEN_SECRET_ID",
        "",
    )
    os.environ["BWS_HEARTBEAT_PING_URL_SECRET_ID"] = parsed.get(
        "BWS_HEARTBEAT_PING_URL_SECRET_ID",
        "",
    )
    os.environ["BWS_APP_ID"] = parsed["BH_GITHUB_APP_ID"]
    os.environ["BWS_INSTALLATION_ID"] = parsed["BH_GITHUB_APP_INSTALLATION_ID"]

    return SandboxConfig(
        repo_owner=owner,
        repo_name=repo,
        github_app_id=parsed["BH_GITHUB_APP_ID"],
        github_app_installation_id=parsed["BH_GITHUB_APP_INSTALLATION_ID"],
        bws_pem_secret_id=parsed["BWS_PEM_SECRET_ID"],
        bws_gh_token_secret_id=parsed.get("BWS_GH_TOKEN_SECRET_ID", ""),
        bws_heartbeat_ping_url_secret_id=parsed.get(
            "BWS_HEARTBEAT_PING_URL_SECRET_ID",
            "",
        ),
    )
