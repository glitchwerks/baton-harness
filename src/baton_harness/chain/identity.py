"""Per-spawn GitHub auth identity broker for chain subprocesses."""

from __future__ import annotations

import enum
import os

from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    resolve_installation_token,
)

_PRIVILEGED_ENV_KEYS: frozenset[str] = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "GH_INSTALLATION_TOKEN"}
)


class Identity(enum.Enum):
    """Spawn identity for chain subprocesses."""

    APP = "app"
    WORKER = "worker"


def env_for(
    identity: Identity,
    *,
    installation_token: InstallationTokenSource | None = None,
) -> dict[str, str]:
    """Return an explicit subprocess env for the requested identity."""
    env = dict(os.environ)

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
