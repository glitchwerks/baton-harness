"""Regressions for credential isolation at real vendored spawn boundaries."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.hooks import run_hook
from baton_harness.vendor.symphony.worker import Worker

_DAEMON_ENV = {
    "GH_TOKEN": "ambient-github-token",
    "GITHUB_TOKEN": "ambient-github-alias",
    "GH_INSTALLATION_TOKEN": "ambient-installation-token",
    "BH_GITHUB_APP_KEY_PROVIDER": "file",
    "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/private/app.pem",
    "BWS_ACCESS_TOKEN": "vault-access-token",
    "BWS_PEM_SECRET_ID": "pem-secret-id",
    "BWS_APP_ID": "app-id",
    "BWS_INSTALLATION_ID": "installation-id",
    "BWS_GH_TOKEN_SECRET_ID": "pat-secret-id",
    "BWS_HEARTBEAT_PING_URL_SECRET_ID": "heartbeat-secret-id",
}


def test_claude_spawn_filters_daemon_credentials() -> None:
    """Catch implicit ambient inheritance in the actual Claude spawn."""
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b'{"result":"done"}', b"")
    with (
        patch.dict(os.environ, {**_DAEMON_ENV, "HOME": "/home/worker"}),
        patch("asyncio.create_subprocess_exec", return_value=process) as spawn,
    ):
        result = asyncio.run(Worker(WorkflowConfig()).run_turn("task", "."))
        child_env = spawn.call_args.kwargs.get("env")
        assert child_env is not None, "Claude must receive an explicit env"
        assert not _DAEMON_ENV.keys() & child_env.keys()
        assert not set(_DAEMON_ENV.values()) & set(child_env.values())
        assert child_env["PATH"] == os.environ["PATH"]
        assert child_env["HOME"] == "/home/worker"
        assert all(
            os.environ[key] == value for key, value in _DAEMON_ENV.items()
        )
    assert result.success


@pytest.mark.parametrize(
    "name", ["after_create", "before_run", "after_run", "before_remove"]
)
@pytest.mark.parametrize("override_daemon_keys", [False, True])
def test_hook_spawn_filters_daemon_credentials(
    name: str, override_daemon_keys: bool
) -> None:
    """Catch ambient or override-based daemon authority in every hook."""
    overrides = {"CHAIN_BASE_BRANCH": "feature/base", "BH_VENV": "/venv"}
    if override_daemon_keys:
        overrides.update(_DAEMON_ENV)
        # Only explicit worker PATs, never ambient tokens, are authorized.
        overrides.pop("GH_TOKEN")
        overrides.pop("GITHUB_TOKEN")
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b"", b"")
    with (
        patch.dict(os.environ, {**_DAEMON_ENV, "HOME": "/home/worker"}),
        patch("asyncio.create_subprocess_exec", return_value=process) as spawn,
    ):
        result = asyncio.run(run_hook(name, "true", ".", env=overrides))
        child_env = spawn.call_args.kwargs["env"]
        assert not _DAEMON_ENV.keys() & child_env.keys()
        assert not set(_DAEMON_ENV.values()) & set(child_env.values())
        assert child_env["PATH"] == os.environ["PATH"]
        assert child_env["HOME"] == "/home/worker"
        assert child_env["CHAIN_BASE_BRANCH"] == "feature/base"
        assert child_env["BH_VENV"] == "/venv"
        assert all(
            os.environ[key] == value for key, value in _DAEMON_ENV.items()
        )
    assert result.ok


@pytest.mark.parametrize(
    "name", ["after_create", "before_run", "after_run", "before_remove"]
)
def test_only_before_run_receives_explicit_worker_pat(name: str) -> None:
    """Keep the authorized PAT override confined to the before_run hook."""
    worker_pat = "github_pat_explicit-worker-token"
    overrides = {"GH_TOKEN": worker_pat, "GITHUB_TOKEN": worker_pat}
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b"", b"")
    with (
        patch.dict(os.environ, _DAEMON_ENV),
        patch("asyncio.create_subprocess_exec", return_value=process) as spawn,
    ):
        result = asyncio.run(run_hook(name, "true", ".", env=overrides))
        child_env = spawn.call_args.kwargs["env"]
    assert result.ok
    if name == "before_run":
        assert child_env["GH_TOKEN"] == worker_pat
        assert child_env["GITHUB_TOKEN"] == worker_pat
    else:
        assert "GH_TOKEN" not in child_env
        assert "GITHUB_TOKEN" not in child_env
    assert not set(_DAEMON_ENV.values()) & set(child_env.values())
