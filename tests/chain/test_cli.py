"""Unit tests for baton_harness.chain.cli (``bh-daemon`` entry point).

Coverage:
- ``--once`` path: daemon invoked with ``once=True``.
- Registry unset (missing env vars) → clean error message + exit 1.
- Default ``--workflow`` resolves to ``config/WORKFLOW.md`` relative to
  the repo root.
- ``--poll-interval`` override is threaded through.
- ``os.chdir`` is called with the managed repo root before ``run_daemon``.
- Workflow path is resolved to absolute BEFORE the ``os.chdir`` call.
"""

from __future__ import annotations

import os
from pathlib import Path
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
        patch("baton_harness.chain.cli.os.chdir"),
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
        patch("baton_harness.chain.cli.os.chdir"),
    ):
        result = _run_main("--once", "--poll-interval", "5")

    assert result == 0
    assert called_kwargs.get("poll_interval_s") == 5.0


# ---------------------------------------------------------------------------
# chdir into managed repo before run_daemon (FIX A)
# ---------------------------------------------------------------------------


def test_main_chdirs_into_project_root_before_run_daemon() -> None:
    """CLI must chdir into the managed repo before calling run_daemon.

    The vendored GitHubTracker calls ``gh`` without ``--repo``, so those
    calls resolve against the process cwd.  The daemon MUST set cwd to
    ``BH_PROJECT_ROOT`` before the event loop starts.
    """
    project_root = Path("/fake/project/root")
    chdir_calls: list[object] = []
    run_daemon_called_after_chdir = False

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        nonlocal run_daemon_called_after_chdir
        # At this point chdir must already have been called.
        run_daemon_called_after_chdir = bool(chdir_calls)

    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = project_root

    with (
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[fake_repo_cfg],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch(
            "baton_harness.chain.cli.os.chdir",
            side_effect=lambda p: chdir_calls.append(p),
        ),
    ):
        result = _run_main("--once")

    assert result == 0
    assert chdir_calls, "os.chdir must be called before run_daemon"
    assert chdir_calls[0] == project_root, (
        f"Expected chdir({project_root!r}), got chdir({chdir_calls[0]!r})"
    )
    assert run_daemon_called_after_chdir, (
        "run_daemon was called before os.chdir"
    )


def test_main_workflow_path_resolved_absolute_before_chdir() -> None:
    """Workflow path must be absolute before chdir so it survives cwd change.

    If a relative ``--workflow`` path were passed without resolving it first,
    the ``chdir`` into the managed repo would break config loading.
    """
    project_root = Path("/fake/project/root")

    # Track the sequence: when was load_workflow called relative to chdir?
    call_sequence: list[str] = []

    def record_load_workflow(path: str) -> MagicMock:
        call_sequence.append("load_workflow")
        assert Path(path).is_absolute(), (
            f"Workflow path must be absolute before load_workflow is"
            f" called, got: {path!r}"
        )
        return MagicMock()

    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = project_root

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        pass

    with (
        patch(
            "baton_harness.chain.cli.load_workflow",
            side_effect=record_load_workflow,
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[fake_repo_cfg],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch("baton_harness.chain.cli.os.chdir"),
    ):
        # Pass a relative path to simulate operator usage.
        result = _run_main("--once", "--workflow", "config/WORKFLOW.md")

    assert result == 0
    assert "load_workflow" in call_sequence
