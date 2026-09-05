"""Per-spawn authentication identity broker for chain subprocesses.

The privileged-key set covers daemon-only authentication configuration
as well as minted tokens.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Mapping

from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    resolve_installation_token,
)

_PRIVILEGED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_INSTALLATION_TOKEN",
        "BH_GITHUB_APP_KEY_PROVIDER",
        "BH_GITHUB_APP_PRIVATE_KEY_FILE",
        "BWS_ACCESS_TOKEN",
        "BWS_PEM_SECRET_ID",
        "BWS_APP_ID",
        "BWS_INSTALLATION_ID",
        "BWS_GH_TOKEN_SECRET_ID",
        "BWS_HEARTBEAT_PING_URL_SECRET_ID",
    }
)


class Identity(enum.Enum):
    """Spawn identity for chain subprocesses."""

    APP = "app"
    WORKER = "worker"


def env_for(
    identity: Identity,
    *,
    installation_token: InstallationTokenSource | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an explicit subprocess env for the requested identity.

    Args:
        identity: Authority to grant the subprocess.
        installation_token: App token source, or a token to exclude from
            worker values.
        base_env: Complete environment to filter, including caller
            overrides. Defaults to the ambient environment.

    Returns:
        A new environment dictionary with the requested authority.

    Raises:
        ValueError: If App identity is requested without a usable token.
    """
    env = dict(os.environ if base_env is None else base_env)

    if identity is Identity.APP:
        if installation_token is None:
            raise ValueError(
                "Identity.APP requires a non-empty installation_token"
            )
        token = resolve_installation_token(installation_token)
        if not token:
            raise ValueError(
                "Identity.APP requires a non-empty installation_token"
            )
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
        env["GH_INSTALLATION_TOKEN"] = token
        return env

    worker_token: str | None = (
        installation_token
        if isinstance(installation_token, str) and installation_token
        else None
    )
    filtered = {
        key: value
        for key, value in env.items()
        if key not in _PRIVILEGED_ENV_KEYS
        and (worker_token is None or value != worker_token)
    }
    return filtered
