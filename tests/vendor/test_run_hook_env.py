"""Tests for VP-1: run_hook gains env= parameter (merged into os.environ).

VP-1 adds an optional ``env: dict[str, str] | None`` keyword argument to
``run_hook`` in the vendored ``symphony/hooks.py``.  The override dict is
merged INTO ``os.environ`` — never passed as-is — so that ``PATH``, ``HOME``,
and every other inherited environment variable remain accessible to git/gh
inside the hook subprocess (CONCERN-1 in issue #42).

Coverage:
- VP-1 signature: ``run_hook`` accepts ``env=`` keyword without error.
- Override keys reach the subprocess env (e.g. CHAIN_BASE_BRANCH, BH_VENV).
- PATH and HOME survive the merge (CONCERN-1 regression guard).
- env=None (default) is identical to passing no env — os.environ only.
- Empty script path → True returned without spawning a subprocess.
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
        _communicate_result: Bytes pair returned by communicate().
    """

    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        """Initialise the fake process.

        Args:
            returncode: The exit code to report.
            stdout: Bytes to return as stdout from communicate().
            stderr: Bytes to return as stderr from communicate().
        """
        self.returncode = returncode
        self._communicate_result = (stdout, stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        """Return the pre-canned stdout/stderr pair.

        Returns:
            A tuple of (stdout_bytes, stderr_bytes).
        """
        return self._communicate_result

    def kill(self) -> None:
        """No-op kill for timeout path."""


# ---------------------------------------------------------------------------
# VP-1 signature tests
# ---------------------------------------------------------------------------


class TestRunHookSignature:
    """run_hook accepts the env= keyword argument (VP-1 signature check)."""

    def test_accepts_env_keyword_none(self) -> None:
        """run_hook(env=None) does not raise TypeError."""
        # Empty script → returns True without spawning a process.
        result = _run_sync(run_hook("test", None, cwd="/tmp", env=None))
        assert result is True

    def test_accepts_env_keyword_dict(self) -> None:
        """run_hook(env={...}) does not raise TypeError."""
        result = _run_sync(
            run_hook("test", None, cwd="/tmp", env={"KEY": "value"})
        )
        assert result is True

    def test_empty_script_returns_true_without_env(self) -> None:
        """Empty script returns True; no subprocess is spawned."""
        result = _run_sync(run_hook("test", "", cwd="/tmp"))
        assert result is True

    def test_whitespace_script_returns_true(self) -> None:
        """Whitespace-only script returns True; no subprocess is spawned."""
        result = _run_sync(run_hook("test", "   ", cwd="/tmp"))
        assert result is True


# ---------------------------------------------------------------------------
# VP-1 env threading tests
# ---------------------------------------------------------------------------


class TestRunHookEnvThreading:
    """Override env keys reach the subprocess (VP-1 threading)."""

    def test_override_key_reaches_subprocess_env(self) -> None:
        """CHAIN_BASE_BRANCH from env= appears in the subprocess env dict."""
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
                    "before_run",
                    "true",
                    cwd="/tmp",
                    env={"CHAIN_BASE_BRANCH": "feature/my-branch"},
                )
            )

        assert captured_env.get("CHAIN_BASE_BRANCH") == "feature/my-branch"

    def test_bh_venv_override_reaches_subprocess_env(self) -> None:
        """BH_VENV from env= appears in the subprocess env dict."""
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
                    "before_run",
                    "true",
                    cwd="/tmp",
                    env={"BH_VENV": "/repo/.venv"},
                )
            )

        assert captured_env.get("BH_VENV") == "/repo/.venv"


# ---------------------------------------------------------------------------
# CONCERN-1 regression: PATH and HOME must survive the merge
# ---------------------------------------------------------------------------


class TestRunHookEnvMerge:
    """Override is merged into os.environ — PATH/HOME are never stripped.

    CONCERN-1 (issue #42): passing an overrides-only dict strips PATH and
    HOME, making git/gh unresolvable inside the hook.  The VP-1 patch must
    merge via ``{**os.environ, **(env or {})}``, never replace.
    """

    def test_path_survives_override_merge(self) -> None:
        """PATH from os.environ is present in subprocess env after merge."""
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
                    "before_run",
                    "true",
                    cwd="/tmp",
                    env={"CHAIN_BASE_BRANCH": "origin/main"},
                )
            )

        # PATH must be present — it comes from os.environ, not from the
        # override dict (which only contains CHAIN_BASE_BRANCH).
        assert "PATH" in captured_env, (
            "PATH was stripped from the subprocess env — "
            "the override dict must be merged INTO os.environ, not replace it "
            "(CONCERN-1 regression)"
        )

    def test_home_survives_override_merge(self) -> None:
        """HOME from os.environ is present in subprocess env after merge."""
        captured_env: dict[str, str] = {}

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeProcess:
            nonlocal captured_env
            captured_env = dict(kwargs.get("env") or {})  # type: ignore[arg-type]
            return _FakeProcess(returncode=0)

        # Ensure HOME is set in os.environ for this test.
        fake_home = "/home/testuser"
        with patch.dict(os.environ, {"HOME": fake_home}, clear=False):
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ):
                _run_sync(
                    run_hook(
                        "before_run",
                        "true",
                        cwd="/tmp",
                        env={"CHAIN_BASE_BRANCH": "origin/main"},
                    )
                )

        assert "HOME" in captured_env, (
            "HOME was stripped from the subprocess env — "
            "the override dict must be merged INTO os.environ, not replace it "
            "(CONCERN-1 regression)"
        )
        assert captured_env["HOME"] == fake_home

    def test_override_key_wins_over_existing_env_value(self) -> None:
        """An override key replaces any existing os.environ value for that key.

        The merge is ``{**os.environ, **overrides}`` so the override wins.
        """
        captured_env: dict[str, str] = {}

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeProcess:
            nonlocal captured_env
            captured_env = dict(kwargs.get("env") or {})  # type: ignore[arg-type]
            return _FakeProcess(returncode=0)

        with patch.dict(
            os.environ, {"CHAIN_BASE_BRANCH": "old-value"}, clear=False
        ):
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ):
                _run_sync(
                    run_hook(
                        "before_run",
                        "true",
                        cwd="/tmp",
                        env={"CHAIN_BASE_BRANCH": "new-value"},
                    )
                )

        assert captured_env.get("CHAIN_BASE_BRANCH") == "new-value", (
            "Override key must win over the existing os.environ value "
            "(merge order: {**os.environ, **overrides})"
        )


# ---------------------------------------------------------------------------
# env=None default is unchanged (no regression on no-env callers)
# ---------------------------------------------------------------------------


class TestRunHookEnvDefault:
    """env=None (the default) behaves identically to the pre-VP-1 code.

    Callers that pass no ``env`` argument must see no behaviour change.
    """

    def test_no_env_arg_passes_os_environ_to_subprocess(self) -> None:
        """When env= is omitted, the subprocess receives os.environ."""
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
            _run_sync(run_hook("before_run", "true", cwd="/tmp"))

        # At minimum, PATH should survive — it is in os.environ.
        assert "PATH" in captured_env

    def test_env_none_explicit_same_as_omitting_env(self) -> None:
        """Explicitly passing env=None is the same as omitting the argument."""
        captured_envs: list[dict[str, str]] = []

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeProcess:
            captured_envs.append(dict(kwargs.get("env") or {}))  # type: ignore[arg-type]
            return _FakeProcess(returncode=0)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            _run_sync(run_hook("a", "true", cwd="/tmp"))
            _run_sync(run_hook("b", "true", cwd="/tmp", env=None))

        # Both calls should produce identical env dicts.
        assert captured_envs[0] == captured_envs[1], (
            "env=None must be identical to omitting the env argument"
        )
