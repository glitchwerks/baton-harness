"""Unit tests for baton_harness.chain.subproc and Phase 1 (#269) migration.

Two coverage layers:

1. ``run_cmd`` itself — direct unit tests, some mocking ``subprocess.run``
   to inspect the exact kwargs it is called with, some invoking a real
   child process to prove ``timeout``/``check``/encoding actually work
   end-to-end rather than merely being forwarded to a mock.
2. **Characterization tests per migrated wrapper** (#268 plan Phase 1) —
   ``branches._run``, ``merge._run``, ``escalation._run``, ``gh_deps._run``,
   ``recovery._run``, ``daemon._run``, and ``daemon._run_gh``. Most existing
   test suites patch the module-local ``_run`` symbol directly and so never
   exercise ``run_cmd``'s actual ``subprocess.run(...)`` call. These tests
   patch ``subprocess.run`` (one level deeper, inside ``subproc.py``) and
   assert each wrapper's resulting invocation matches its pre-migration
   ``subprocess.run`` call byte-for-byte: argv, ``capture_output``, ``text``,
   ``env``, ``check``, and (where applicable) ``timeout``.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from baton_harness.chain.subproc import run_cmd

import baton_harness.chain.branches as branches_mod
import baton_harness.chain.daemon as daemon_mod
import baton_harness.chain.escalation as escalation_mod
import baton_harness.chain.gh_deps as gh_deps_mod
import baton_harness.chain.merge as merge_mod
import baton_harness.chain.recovery as recovery_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingRun:
    """A ``subprocess.run`` stand-in that records the call it received.

    Attributes:
        cmd: The positional ``cmd`` argument from the most recent call.
        kwargs: The keyword arguments from the most recent call.
    """

    def __init__(self) -> None:
        """Initialize with no recorded call yet."""
        self.cmd: list[str] | None = None
        self.kwargs: dict[str, Any] = {}

    def __call__(
        self,
        cmd: list[str],
        **kwargs: Any,  # noqa: ANN401
    ) -> subprocess.CompletedProcess[str]:
        """Record the call and return a fake successful CompletedProcess.

        Args:
            cmd: Command and arguments, as passed to ``subprocess.run``.
            **kwargs: All other keyword arguments passed through.

        Returns:
            A ``CompletedProcess`` with ``returncode=0``.
        """
        self.cmd = cmd
        self.kwargs = kwargs
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )


@pytest.fixture
def recording_run(
    monkeypatch: pytest.MonkeyPatch,
) -> _RecordingRun:
    """Patch ``subprocess.run`` inside ``subproc.py`` and return the spy.

    Args:
        monkeypatch: pytest's monkeypatch fixture.

    Returns:
        The ``_RecordingRun`` spy installed as ``subprocess.run``.
    """
    spy = _RecordingRun()
    monkeypatch.setattr("baton_harness.chain.subproc.subprocess.run", spy)
    return spy


# ---------------------------------------------------------------------------
# run_cmd — direct unit tests
# ---------------------------------------------------------------------------


class TestRunCmdKwargShape:
    """Verify run_cmd's own subprocess.run call shape (mocked)."""

    def test_defaults_are_capture_text_check_true(
        self, recording_run: _RecordingRun
    ) -> None:
        """Default kwargs: capture/text/check True, no env/timeout set."""
        run_cmd(["echo", "hi"])

        assert recording_run.cmd == ["echo", "hi"]
        assert recording_run.kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "env": None,
            "timeout": None,
            "check": True,
        }

    def test_check_false_is_forwarded(
        self, recording_run: _RecordingRun
    ) -> None:
        """check=False is forwarded to subprocess.run unchanged."""
        run_cmd(["git", "status"], check=False)

        assert recording_run.kwargs["check"] is False

    def test_env_dict_forwarded_unchanged(
        self, recording_run: _RecordingRun
    ) -> None:
        """An explicit env dict is forwarded to subprocess.run unchanged."""
        env = {"GH_TOKEN": "abc123"}

        run_cmd(["gh", "api", "foo"], env=env)

        assert recording_run.kwargs["env"] is env

    def test_timeout_forwarded_to_subprocess_run(
        self, recording_run: _RecordingRun
    ) -> None:
        """Timeout is forwarded to subprocess.run unchanged."""
        run_cmd(["git", "push"], timeout=12.5)

        assert recording_run.kwargs["timeout"] == 12.5

    def test_capture_false_text_false_omits_text_and_encoding(
        self, recording_run: _RecordingRun
    ) -> None:
        """capture/text=False passes capture_output=False, no text/encoding."""
        run_cmd(["git", "log"], capture=False, text=False, check=False)

        assert recording_run.cmd == ["git", "log"]
        assert recording_run.kwargs == {
            "capture_output": False,
            "env": None,
            "timeout": None,
            "check": False,
        }


class TestRunCmdEndToEnd:
    """Real subprocess invocations.

    Proves forwarding actually works, not just that kwargs are passed
    to a mock.
    """

    def test_captures_stdout_as_text(self) -> None:
        """capture=True, text=True (the defaults) return decoded stdout."""
        result = run_cmd([sys.executable, "-c", "print('hello')"], check=False)

        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_timeout_raises_timeout_expired(self) -> None:
        """A positive timeout shorter than the command raises TimeoutExpired.

        Guards the #223 push-probe reliance on a positive timeout value
        (pinned by test_daemon_push_probe.py:1260 for daemon._run).
        """
        with pytest.raises(subprocess.TimeoutExpired):
            run_cmd(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=0.2,
                check=False,
            )

    def test_check_true_raises_on_nonzero_exit(self) -> None:
        """check=True raises CalledProcessError on a non-zero exit code."""
        with pytest.raises(subprocess.CalledProcessError):
            run_cmd([sys.executable, "-c", "import sys; sys.exit(3)"])

    def test_check_false_returns_nonzero_without_raising(self) -> None:
        """check=False (the migrated wrappers' shape) does not raise."""
        result = run_cmd(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            check=False,
        )

        assert result.returncode == 3


# ---------------------------------------------------------------------------
# Characterization tests — capture-family wrappers migrated in #269
# ---------------------------------------------------------------------------


class TestBranchesRunCharacterization:
    """branches._run defaults env=None to env_for(Identity.WORKER)."""

    def test_default_env_resolves_to_worker_identity_env(
        self,
        recording_run: _RecordingRun,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """None env is replaced with env_for(Identity.WORKER)."""
        sentinel_env = {"WORKER_ENV": "1"}
        monkeypatch.setattr(
            branches_mod, "env_for", lambda identity: sentinel_env
        )

        branches_mod._run(["git", "status"])

        assert recording_run.cmd == ["git", "status"]
        assert recording_run.kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "env": sentinel_env,
            "timeout": None,
            "check": False,
        }

    def test_explicit_env_bypasses_worker_default(
        self,
        recording_run: _RecordingRun,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit env dict is passed through, not overridden."""
        monkeypatch.setattr(
            branches_mod,
            "env_for",
            lambda identity: pytest.fail(
                "env_for should not be called when env is explicit"
            ),
        )
        explicit_env = {"X": "1"}

        branches_mod._run(["git", "status"], env=explicit_env)

        assert recording_run.kwargs["env"] == explicit_env


class TestMergeRunCharacterization:
    """merge._run leaves env=None as None (inherit os.environ)."""

    def test_none_env_stays_none(self, recording_run: _RecordingRun) -> None:
        """env=None is forwarded to run_cmd as None, not substituted."""
        merge_mod._run(["gh", "pr", "view"])

        assert recording_run.cmd == ["gh", "pr", "view"]
        assert recording_run.kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "env": None,
            "timeout": None,
            "check": False,
        }

    def test_explicit_env_forwarded(
        self, recording_run: _RecordingRun
    ) -> None:
        """An explicit env dict is forwarded unchanged."""
        explicit_env = {"GH_TOKEN": "tok"}

        merge_mod._run(["gh", "pr", "view"], env=explicit_env)

        assert recording_run.kwargs["env"] == explicit_env


class TestEscalationRunCharacterization:
    """escalation._run leaves env=None as None (inherit os.environ)."""

    def test_none_env_stays_none(self, recording_run: _RecordingRun) -> None:
        """env=None is forwarded to run_cmd as None, not substituted."""
        escalation_mod._run(["gh", "api", "/repos/foo/issues"])

        assert recording_run.cmd == ["gh", "api", "/repos/foo/issues"]
        assert recording_run.kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "env": None,
            "timeout": None,
            "check": False,
        }


class TestGhDepsRunCharacterization:
    """gh_deps._run (keyword-only env) leaves env=None as None."""

    def test_none_env_stays_none(self, recording_run: _RecordingRun) -> None:
        """env=None is forwarded to run_cmd as None, not substituted."""
        gh_deps_mod._run(["gh", "api", "/repos/foo/issues/1"])

        assert recording_run.cmd == ["gh", "api", "/repos/foo/issues/1"]
        assert recording_run.kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "env": None,
            "timeout": None,
            "check": False,
        }

    def test_env_is_keyword_only(self, recording_run: _RecordingRun) -> None:
        """Env remains keyword-only after migration (signature preserved)."""
        explicit_env = {"GH_TOKEN": "tok"}

        gh_deps_mod._run(["gh", "api", "/x"], env=explicit_env)

        assert recording_run.kwargs["env"] == explicit_env


class TestRecoveryRunCharacterization:
    """recovery._run leaves env=None as None (inherit os.environ)."""

    def test_none_env_stays_none(self, recording_run: _RecordingRun) -> None:
        """env=None is forwarded to run_cmd as None, not substituted."""
        recovery_mod._run(["git", "worktree", "list"])

        assert recording_run.cmd == ["git", "worktree", "list"]
        assert recording_run.kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "env": None,
            "timeout": None,
            "check": False,
        }


class TestDaemonRunCharacterization:
    """daemon._run leaves env=None as None and forwards timeout."""

    def test_none_env_and_none_timeout(
        self, recording_run: _RecordingRun
    ) -> None:
        """env=None and timeout=None are both forwarded unchanged."""
        daemon_mod._run(["git", "push"])

        assert recording_run.cmd == ["git", "push"]
        assert recording_run.kwargs == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "env": None,
            "timeout": None,
            "check": False,
        }

    def test_explicit_env_and_timeout_forwarded(
        self, recording_run: _RecordingRun
    ) -> None:
        """Explicit env and timeout are both forwarded unchanged.

        Guards the #223 push-probe's reliance on a positive timeout
        (test_daemon_push_probe.py:1260 pins this at the integration
        level; this test pins it at the subprocess.run boundary).
        """
        explicit_env = {"GH_TOKEN": "tok"}

        daemon_mod._run(["git", "push"], env=explicit_env, timeout=30.0)

        assert recording_run.kwargs["env"] == explicit_env
        assert recording_run.kwargs["timeout"] == 30.0


class TestDaemonRunGhCharacterization:
    """daemon._run_gh omits env= entirely when no override applies.

    Unrelated to run_cmd directly (``_run_gh`` calls the local ``_run``
    wrapper, not ``run_cmd``), but the omit-vs-pass-env behavior is part
    of the pre-migration contract this issue must preserve exactly.
    """

    def test_omits_env_kwarg_when_gh_call_env_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No env override -> _run is called with no env= kwarg at all."""
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(
            cmd: list[str],
            **kwargs: Any,  # noqa: ANN401
        ) -> subprocess.CompletedProcess[str]:
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(daemon_mod, "_run", fake_run)

        daemon_mod._run_gh(["gh", "api", "/x"], None)

        assert calls == [(["gh", "api", "/x"], {})]

    def test_passes_env_kwarg_when_gh_call_env_is_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An env override is passed through as env=."""
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(
            cmd: list[str],
            **kwargs: Any,  # noqa: ANN401
        ) -> subprocess.CompletedProcess[str]:
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(daemon_mod, "_run", fake_run)
        env = {"GH_TOKEN": "tok"}

        daemon_mod._run_gh(["gh", "api", "/x"], env)

        assert calls == [(["gh", "api", "/x"], {"env": env})]
