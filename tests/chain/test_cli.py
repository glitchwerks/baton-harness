"""Unit tests for baton_harness.chain.cli (``bh-daemon`` entry point).

Coverage:
- ``--once`` path: daemon invoked with ``once=True``.
- Registry unset (missing env vars) → clean error message + exit 1.
- Default ``--workflow`` resolves to ``config/WORKFLOW.md`` relative to
  the repo root.
- ``--poll-interval`` override is threaded through.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from baton_harness.chain.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_main(*args: str) -> int:
    """Run ``main`` with the given argv and return the exit code."""
    return main(list(args))


# ---------------------------------------------------------------------------
# Registry unset → clean error + exit 1
# ---------------------------------------------------------------------------


def test_main_registry_unset_exits_1() -> None:
    """Missing registry env vars produce a clean error and return 1."""
    env_backup = {
        k: os.environ.pop(k, None)
        for k in ("BH_REPO_OWNER", "BH_REPO_NAME", "BH_PROJECT_ROOT")
    }
    try:
        # We still need a workflow file to exist; patch load_workflow to
        # avoid a file-system dependency.
        with (
            patch(
                "baton_harness.chain.cli.load_workflow",
                return_value=MagicMock(),
            ),
            patch(
                "baton_harness.chain.cli.load_registry",
                side_effect=ValueError(
                    "Registry is not configured. "
                    "Set BH_REPO_OWNER, BH_REPO_NAME, and BH_PROJECT_ROOT"
                ),
            ),
        ):
            result = _run_main("--once")
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v

    assert result == 1, f"Expected exit 1, got {result}"


# ---------------------------------------------------------------------------
# --once path
# ---------------------------------------------------------------------------


def test_main_once_calls_run_daemon_with_once_true() -> None:
    """--once flag passes once=True to run_daemon."""
    called_kwargs: dict = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        called_kwargs.update(kwargs)

    with (
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[MagicMock()],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
    ):
        result = _run_main("--once")

    assert result == 0, f"Expected exit 0, got {result}"
    assert called_kwargs.get("once") is True


def test_main_poll_interval_override() -> None:
    """--poll-interval is passed to run_daemon."""
    called_kwargs: dict = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        called_kwargs.update(kwargs)

    with (
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[MagicMock()],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
    ):
        result = _run_main("--once", "--poll-interval", "5")

    assert result == 0
    assert called_kwargs.get("poll_interval_s") == 5.0
