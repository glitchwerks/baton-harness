"""Regression tests for issue #215: run_hook must not spawn a login shell.

``run_hook`` in the vendored ``symphony/hooks.py`` currently spawns hook
scripts via ``asyncio.create_subprocess_exec("bash", "-lc", script, ...)``.
The ``-l`` (login shell) flag forces the OS account's ``/etc/profile`` +
``~/.bashrc`` chain to run before the hook script itself executes, which can
clobber environment variables the daemon injected (e.g. ``GH_TOKEN``) before
the hook script ever reads them.

The fix (VP-7, applied in this PR) drops ``-l`` so the invocation becomes
``"bash", "-c", script`` — a non-interactive, non-login shell. Hooks must
never run a login shell.

Coverage:
- ``TestRunHookNoLoginShell``: the subprocess argv passed to
  ``asyncio.create_subprocess_exec`` must be exactly
  ``("bash", "-c", script)`` — ``-lc``/``-l`` must never appear. This test
  guards against a regression back to a login shell.
- ``TestRunHookEnvMergePreserved``: the env merge behaviour (VP-1) that
  layers caller-supplied overrides on top of ``os.environ`` must survive
  the ``-lc`` -> ``-c`` fix. This test guards the VP-1 env-merge behaviour
  against regression.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from baton_harness.vendor.symphony.hooks import run_hook

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_sync(coro: object) -> object:
    """Run an async coroutine synchronously for test use.

    Args:
        coro: An awaitable coroutine to run.

    Returns:
        The result of the coroutine.
    """
    return asyncio.run(coro)  # type: ignore[arg-type]


class _FakeProcess:
    """Minimal asyncio.subprocess stub for use in mock.patch targets.

    Attributes:
        returncode: Exit code the fake process will report.
    """

    def __init__(self, returncode: int = 0) -> None:
        """Initialise the fake process.

        Args:
            returncode: The exit code to report.
        """
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        """Return an empty stdout/stderr pair.

        Returns:
            A tuple of (stdout_bytes, stderr_bytes), both empty.
        """
        return (b"", b"")

    def kill(self) -> None:
        """No-op kill for the timeout path."""


# ---------------------------------------------------------------------------
# Issue #215: no login shell
# ---------------------------------------------------------------------------


class TestRunHookNoLoginShell:
    """run_hook must invoke bash as a non-login shell (issue #215)."""

    def test_argv_omits_login_shell_flag(self, tmp_path: object) -> None:
        """The captured argv must be exactly ("bash", "-c", script).

        Protects issue #215: a login shell (``-lc``/``-l``) forces
        ``/etc/profile`` + ``~/.bashrc`` to run before the hook script,
        which can clobber daemon-injected env vars (e.g. ``GH_TOKEN``)
        ahead of the hook reading them. This test guards against a
        regression back to passing ``"-lc"`` as the second argv element.
        """
        captured_argv: tuple[object, ...] = ()

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeProcess:
            nonlocal captured_argv
            captured_argv = args
            return _FakeProcess(returncode=0)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            _run_sync(run_hook("test", "echo hi", cwd=str(tmp_path)))

        assert captured_argv == ("bash", "-c", "echo hi"), (
            "run_hook must spawn a non-login shell — argv must be exactly "
            '("bash", "-c", script). Got '
            f"{captured_argv!r} (issue #215: -lc/-l must never appear)"
        )
        assert "-lc" not in captured_argv, (
            "run_hook must not pass the login-shell flag -lc (issue #215)"
        )
        assert "-l" not in captured_argv, (
            "run_hook must not pass the login-shell flag -l (issue #215)"
        )


# ---------------------------------------------------------------------------
# Issue #215: env merge must survive the -lc -> -c fix
# ---------------------------------------------------------------------------


class TestRunHookEnvMergePreserved:
    """The VP-1 env merge (os.environ + overrides) must keep working.

    Guards against a regression from the issue #215 fix: dropping the
    login-shell flag must not disturb the existing env-merge behaviour
    that layers caller-supplied overrides (e.g. ``GH_TOKEN``) on top of
    the inherited ``os.environ`` (which still carries ``PATH``, etc.).
    This test guards the VP-1 env-merge behaviour against regression.
    """

    def test_override_merged_with_inherited_baseline_var(
        self, tmp_path: object
    ) -> None:
        """GH_TOKEN override reaches the subprocess env; PATH still present."""
        captured_env: dict[str, str] = {}

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeProcess:
            nonlocal captured_env
            captured_env = dict(kwargs.get("env") or {})  # type: ignore[arg-type]
            return _FakeProcess(returncode=0)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            _run_sync(
                run_hook(
                    "test",
                    "echo hi",
                    cwd=str(tmp_path),
                    env={"GH_TOKEN": "sentinel123"},
                )
            )

        assert captured_env.get("GH_TOKEN") == "sentinel123", (
            "the env= override must reach the subprocess env "
            "(GH_TOKEN was not found or had the wrong value)"
        )
        assert "PATH" in captured_env, (
            "the override must be merged INTO os.environ, not replace it — "
            "PATH (an inherited baseline var) must still be present"
        )
        assert captured_env["PATH"] == os.environ.get("PATH"), (
            "the inherited PATH value must be unchanged by the merge"
        )
