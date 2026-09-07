"""Tests for the git push credential-helper presence check (issue #219).

Root cause (#219): ``gh auth login`` authenticates the ``gh`` CLI but does
NOT install a git credential helper, so the daemon's ``git push`` fails
with "Password authentication is not supported" even though a GitHub
token (``GH_TOKEN``) is present. ``gh auth setup-git`` is the fix
(wired into ``bin/init-sandbox.sh``); this module tests the companion
startup-preflight gate (G3d) that fails loud at daemon startup instead
of at first push if no git credential helper is configured.

Spec:

- G3d: A github.com-scoped or global git credential helper is
  configured → check passes (no alert, startup proceeds).
- G3d: No credential helper configured (scoped or global) → critical
  ``alert()`` + ``sys.exit(1)`` (fatal, same mechanism as G3a/G3b/G3c),
  with a remediation message naming ``gh auth setup-git``.
- Presence/shape check only: the probe inspects ``git config`` key
  NAMES only; it never reads, logs, or asserts on credential VALUES.

All I/O is mocked: the ``git config`` probe (``_get_git_credential_helpers``)
is patched directly per-test so no real subprocess or git config is
touched, and the test does not depend on the host machine's actual git
credential configuration.

Test conventions mirror ``test_reconcile_oauth_cred.py``:
- Lazy import via ``_import_reconcile()`` so a missing symbol fails with
  a clean ``AttributeError``/``AssertionError``, not a collection error.
- ``asyncio.run(reconcile.reconcile_startup(...))`` (no pytest-asyncio).
- Fatal signal: ``pytest.raises(SystemExit)`` with a non-zero code.
- Alert assertions via ``MagicMock`` on
  ``baton_harness.chain.reconcile.alert``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_OWNER = "glitchwerks"
_REPO = "baton-harness"
_INSTALLATION_TOKEN = "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _import_reconcile() -> Any:  # noqa: ANN401
    """Return the reconcile module, raising ImportError if absent."""
    import importlib

    return importlib.import_module("baton_harness.chain.reconcile")


def _make_obs(tmp_path: Path) -> Any:  # noqa: ANN401
    """Return an ObsConfig-like object rooted at tmp_path."""
    from baton_harness.chain.obs_config import ObsConfig

    harness_dir = tmp_path / ".baton-harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    return ObsConfig(
        runlog_path=harness_dir / "runlog.jsonl",
        heartbeat_file=harness_dir / "heartbeat",
        redispatch_window_ticks=10,
        redispatch_max=3,
        heartbeat_stall_s=7200.0,
        heartbeat_ping_url=None,
        redispatch_counts_path=harness_dir / "dispatch-counts.json",
    )


def _make_repo_cfg(tmp_path: Path) -> Any:  # noqa: ANN401
    """Return a minimal RepoConfig pointing at tmp_path."""
    from baton_harness.chain.registry import RepoConfig

    return RepoConfig(
        owner=_OWNER,
        repo=_REPO,
        project_root=tmp_path,
    )


@pytest.fixture(autouse=True)
def _patch_oauth_cred_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point _OAUTH_CRED_PATH at a readable temp file for every test here.

    Ensures G3c passes deterministically on any runner so tests in this
    module exercise G3d specifically, independent of the runner's real
    ``~/.claude/.credentials.json``.

    Args:
        tmp_path: Pytest-provided per-test temporary directory.
        monkeypatch: Pytest monkeypatch fixture for attribute patching.
    """
    cred_file = tmp_path / "fake_credentials.json"
    cred_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
        cred_file,
    )


def _patch_passing_prereqs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars so the G3a/G3b gates pass without touching G3d.

    Removes ANTHROPIC_API_KEY (must be absent) and sets GH_TOKEN to a
    fake ghs_ installation token so the token-type check in
    validate_daemon_token passes before we patch it out.

    Args:
        monkeypatch: Pytest monkeypatch fixture for attribute patching.
    """
    monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# G3d: credential helper configured — happy path
# ---------------------------------------------------------------------------


class TestG3dCredentialHelperConfigured:
    """Credential helper configured → no alert, startup proceeds."""

    def test_configured_helper_no_alert_no_halt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A configured helper → check passes silently (no critical alert)."""
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        assert hasattr(reconcile, "_get_git_credential_helpers"), (
            "reconcile must expose _get_git_credential_helpers seam "
            "(test_module_exposes_git_credential_helper_seam pins this)"
        )

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                return_value=["!'/usr/bin/gh' auth git-credential"],
            ),
        ):
            # Must NOT raise SystemExit or any exception.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        critical_calls = [
            c
            for c in mock_alert.call_args_list
            if c.kwargs.get("severity") == "critical"
        ]
        assert not critical_calls, (
            "Configured credential helper must not trigger a critical "
            f"alert; got: {critical_calls}"
        )

    def test_scoped_helper_alone_is_sufficient(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A github.com-scoped helper (no global fallback) still passes."""
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                return_value=["manager"],
            ),
        ):
            # Must NOT raise.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )
        # Reaching here is the assertion: no exception was raised.


# ---------------------------------------------------------------------------
# G3d: no credential helper configured — fatal
# ---------------------------------------------------------------------------


class TestG3dCredentialHelperAbsent:
    """No credential helper configured → critical alert + SystemExit."""

    def test_absent_helper_emits_critical_alert_and_halts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No configured helper → critical alert fired + sys.exit(1).

        Mirrors test_reconcile_oauth_cred.py::
        TestG3cCredentialFileAbsent::
        test_absent_credential_file_emits_critical_alert_and_halts.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert exc_info.value.code != 0, (
            "SystemExit code must be non-zero when no credential helper "
            "is configured"
        )
        assert mock_alert.called, (
            "alert() must be called before halting on absent credential helper"
        )
        _, kwargs = mock_alert.call_args
        assert kwargs.get("severity") == "critical", (
            "Absent credential helper must trigger severity='critical'; "
            f"got {kwargs!r}"
        )

    def test_absent_helper_alert_names_remediation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fatal alert message must name the fix (`gh auth setup-git`)."""
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        alert_args, _ = mock_alert.call_args
        message = str(alert_args[3])
        assert "gh auth setup-git" in message, (
            "Remediation alert must name `gh auth setup-git`; "
            f"got message: {message!r}"
        )

    def test_absent_helper_alert_passes_issue_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Absent-helper alert carries issue=None (repo-level)."""
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        alert_args, _ = mock_alert.call_args
        assert alert_args[2] is None, (
            "Absent credential helper alert must pass issue=None "
            f"(positional arg index 2); got {alert_args[2]!r}"
        )

    def test_absent_helper_prevents_g2_marker_and_g1_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G3d fatal → G2 marker NOT written, G1 scan skipped.

        The credential-helper check must run before G2/G1; a fatal here
        must prevent both the marker write and the process scan.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        mock_lister = MagicMock(return_value=[])
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        marker = tmp_path / ".baton-harness" / "daemon.alive"

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                mock_lister,
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert not marker.exists(), (
            "daemon.alive marker must NOT be created when G3d fails fatally"
        )
        mock_lister.assert_not_called()

    def test_absent_helper_alert_forwards_installation_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G3d alert() must receive installation_token= (mirrors G3a-c)."""
        reconcile = _import_reconcile()
        _token = "ghs_TEST_G3d_alert_token"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        alert_calls: list[dict[str, object]] = []

        def _capture_alert(*args: object, **kwargs: object) -> bool:
            alert_calls.append({"args": args, "kwargs": kwargs})
            return True

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                side_effect=_capture_alert,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(
                        repo_cfgs,
                        obs,
                        runlog=None,
                        installation_token=_token,
                    )
                )

        assert alert_calls, "alert() must be called on G3d failure"
        for call in alert_calls:
            got = call["kwargs"].get("installation_token")
            assert got == _token, (
                "G3d alert() call must forward installation_token= to "
                f"alert(); expected {_token!r}, got {got!r}"
            )


# ---------------------------------------------------------------------------
# _get_git_credential_helpers — probe seam / subprocess mocking
# ---------------------------------------------------------------------------


class TestGetGitCredentialHelpersProbe:
    """Direct unit tests for the ``_get_git_credential_helpers`` probe.

    All subprocess invocations are mocked — this test class never
    depends on (or touches) the host machine's actual git credential
    configuration.
    """

    def test_module_exposes_probe_seam(self) -> None:
        """Module must expose _get_git_credential_helpers as callable."""
        reconcile = _import_reconcile()

        assert hasattr(reconcile, "_get_git_credential_helpers"), (
            "reconcile module must expose _get_git_credential_helpers "
            "at module level"
        )
        assert callable(reconcile._get_git_credential_helpers), (
            "_get_git_credential_helpers must be callable"
        )

    def test_scoped_helper_configured_returns_nonempty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A github.com-scoped helper is returned without a fallback.

        The scoped key is present, so no second (global fallback)
        subprocess call is required.
        """
        reconcile = _import_reconcile()

        mock_run = MagicMock()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="!'/usr/bin/gh' auth git-credential\n",
        )
        monkeypatch.setattr(
            "baton_harness.chain.reconcile.subprocess.run", mock_run
        )

        result = reconcile._get_git_credential_helpers()

        assert result == ["!'/usr/bin/gh' auth git-credential"]
        # Falls back only when the scoped key is absent — here it is
        # present, so only one subprocess call is expected.
        assert mock_run.call_count == 1

    def test_falls_back_to_global_helper_when_scoped_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scoped key absent (exit 1) → falls back to global helper key."""
        reconcile = _import_reconcile()

        def _fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
            key = cmd[-1]
            if key == "credential.https://github.com.helper":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="manager\n")

        monkeypatch.setattr(
            "baton_harness.chain.reconcile.subprocess.run", _fake_run
        )

        result = reconcile._get_git_credential_helpers()

        assert result == ["manager"]

    def test_no_helper_configured_anywhere_returns_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Neither scoped nor global helper configured → empty list."""
        reconcile = _import_reconcile()

        mock_run = MagicMock(return_value=MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(
            "baton_harness.chain.reconcile.subprocess.run", mock_run
        )

        result = reconcile._get_git_credential_helpers()

        assert result == []

    def test_git_not_found_returns_empty_not_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Git binary missing (OSError) → treated as no helper, no crash."""
        reconcile = _import_reconcile()

        def _raise_oserror(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(
            "baton_harness.chain.reconcile.subprocess.run", _raise_oserror
        )

        result = reconcile._get_git_credential_helpers()

        assert result == []

    def test_probe_never_asserts_on_credential_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Probe result is a shape/presence signal, not a value check.

        The value string itself is never validated or required to
        match a known helper program; any non-empty configured name
        satisfies the gate.
        """
        reconcile = _import_reconcile()

        mock_run = MagicMock(
            return_value=MagicMock(
                returncode=0,
                stdout="some-arbitrary-unrecognised-helper-name\n",
            )
        )
        monkeypatch.setattr(
            "baton_harness.chain.reconcile.subprocess.run", mock_run
        )

        result = reconcile._get_git_credential_helpers()

        assert result, (
            "Any non-empty configured helper name must satisfy the probe "
            "— the gate does not validate which helper is installed"
        )
