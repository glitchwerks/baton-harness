"""Unit tests for baton_harness.chain.reconcile.

Tests the startup reconciliation sweep module (issue #40).  All I/O is
mocked: ``validate_daemon_token`` is patched to avoid real GitHub API
calls; ``alert`` is patched to record emission; the process-lister seam
is injected or patched so no real ``pgrep`` is called.

Async test functions are driven with ``asyncio.run`` so no
pytest-asyncio dependency is needed (mirrors test_daemon.py convention).

Coverage:
- G3: credential validation (GH token + ANTHROPIC_API_KEY) — happy path
  and fatal-halt paths.
- G3 fatal-ordering: credential check runs first; failure prevents G2/G1.
- G2: ungraceful-prior-exit detection via marker file.
- G1: orphan claude-process sweep via injected process-lister.
- Per-check isolation: non-fatal check failure does not abort siblings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from baton_harness.chain.doctor import Phase

# ---------------------------------------------------------------------------
# Module under test — does not exist yet; tests MUST fail red until
# reconcile.py is implemented.
# ---------------------------------------------------------------------------

# We import lazily inside each test via try/except to produce a clean
# ImportError failure (AttributeError-free red) rather than blowing up the
# collection phase for the entire file.


def _import_reconcile() -> Any:  # noqa: ANN401
    """Return the reconcile module, raising ImportError if absent."""
    import importlib

    return importlib.import_module("baton_harness.chain.reconcile")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_OWNER = "glitchwerks"
_REPO = "baton-harness"

# A valid GitHub App installation token so validate_daemon_token passes
# the token-type gate.  We patch out the validator so the value does
# not matter beyond the ghs_ prefix.
_INSTALLATION_TOKEN = "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _make_obs(tmp_path: Path) -> Any:  # noqa: ANN401
    """Return an ObsConfig-like object with a real tmp_path project root."""
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


def _no_sleep(_: float) -> None:
    """No-op sleep for validate_daemon_token retry injection."""


# ---------------------------------------------------------------------------
# Module-level autouse fixture: neutralise G3c for all tests in this file.
#
# These tests pre-date the G3c OAuth credential-volume gate (issue #108).
# On CI (Ubuntu runner) ~/.claude/.credentials.json is absent, which causes
# G3c to fire sys.exit(1) and fail every test that calls reconcile_startup.
#
# We patch the module-level seam _OAUTH_CRED_PATH to a readable tmp file
# so G3c always passes here, independent of the runner's home directory.
# Tests that specifically validate G3c live in test_reconcile_oauth_cred.py.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_oauth_cred_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point _OAUTH_CRED_PATH at a readable temp file for every test here.

    Ensures G3c passes deterministically on any runner (local or CI)
    without reading the real ~/.claude/.credentials.json.

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


@pytest.fixture(autouse=True)
def _patch_git_credential_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise G3d for all tests in this file (issue #219).

    These tests pre-date the G3d git-push-credential-helper gate. A CI
    runner (or a dev machine) may have no github.com/global git
    credential helper configured, which would cause G3d to fire
    sys.exit(1) and fail every test that calls reconcile_startup.

    We patch the module-level seam _get_git_credential_helpers to
    report a helper as configured, so G3d always passes here,
    independent of the runner's real git config. Tests that
    specifically validate G3d live in
    test_reconcile_git_credential_helper.py.

    Args:
        monkeypatch: Pytest monkeypatch fixture for attribute patching.
    """
    monkeypatch.setattr(
        "baton_harness.chain.reconcile._get_git_credential_helpers",
        lambda: ["!fake credential helper for tests"],
    )


@pytest.fixture(autouse=True)
def _patch_doctor_run_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the POST_BOOTSTRAP doctor gate for tests that don't test it.

    Phase 4 (#193) wires ``doctor.run_gate(ctx, Phase.POST_BOOTSTRAP)``
    into ``reconcile_startup``, between the native G3d block and G2.
    Once wired, any test in this file that doesn't stub the gate would
    hit the real ruleset/label/repo-admin checks (no ruleset provisioned,
    no reachable label list, etc. in the test environment) and fail --
    mirroring the rationale behind ``test_cli_doctor_gate.py``'s
    ``_auto_patch_pre_bootstrap_gate`` fixture (same repo, same Phase-3
    precedent), but scoped to this file. ``test_doctor.py`` calls
    ``doctor.run_gate`` directly and needs the real implementation, so
    this must NOT move to a shared ``conftest.py``.

    Patched at ``doctor.run_gate``'s own defining module
    (``baton_harness.chain.doctor.run_gate``), mirroring the dotted-
    module-call idiom already established by ``test_cli_doctor_gate.py``
    for the Phase-3 wiring. If ``reconcile.py`` instead binds the bare
    name (``from baton_harness.chain.doctor import run_gate``), this
    patch target -- and the equivalent fixture in
    ``test_reconcile_oauth_cred.py`` / ``test_reconcile_git_credential_
    helper.py`` -- will need to move to
    ``baton_harness.chain.reconcile.run_gate``; flagged in the return
    summary.

    Tests that specifically exercise the POST_BOOTSTRAP gate
    (``TestPostBootstrapDoctorGate``) override this fixture with their
    own ``patch(...)`` inside a ``with`` block, which wins as the
    innermost patch (same precedent as ``_patch_oauth_cred_path``'s
    per-test override at line ~1360).

    Args:
        monkeypatch: Pytest monkeypatch fixture for attribute patching.
    """
    monkeypatch.setattr(
        "baton_harness.chain.doctor.run_gate",
        lambda *args, **kwargs: None,
    )


# ---------------------------------------------------------------------------
# G3 — Credential validation
# ---------------------------------------------------------------------------


class TestG3CredentialValidation:
    """Tests for check 1: credential validation (G3)."""

    def test_happy_path_no_alert_no_halt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid token + ANTHROPIC_API_KEY absent → no alert, no halt.

        The architecture mandates OAuth-via-mounted-volume; ANTHROPIC_API_KEY
        must NOT be set.  Absent (or empty) is the correct happy-path state.
        """
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                mock_alert,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            # Must NOT raise SystemExit or any exception.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        mock_alert.assert_not_called()

    def test_missing_gh_token_emits_critical_alert_and_halts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Token validation failure raises critical alert + SystemExit."""
        reconcile = _import_reconcile()

        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        # ANTHROPIC_API_KEY must be absent (OAuth path); this is correct state.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from baton_harness._auth import TokenValidationError

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                side_effect=TokenValidationError("no token found"),
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                mock_alert,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert exc_info.value.code != 0, (
            "SystemExit code must be non-zero on token failure"
        )
        # A critical alert must have been emitted before the halt.
        assert mock_alert.called, (
            "alert() must be called before halting on token failure"
        )
        # Verify severity=critical was passed.
        _, kwargs = mock_alert.call_args
        assert kwargs.get("severity") == "critical", (
            f"Expected severity='critical', got {kwargs!r}"
        )

    def test_anthropic_key_present_emits_critical_alert_and_halts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ANTHROPIC_API_KEY set and non-empty → critical alert + SystemExit.

        Architecture (docs/architecture-spec.md §L318): refuse to start if
        ANTHROPIC_API_KEY is set — prevents accidental per-token billing on
        a subscription-OAuth deployment.
        """
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        # Set the key — this MUST trigger the guard.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                mock_alert,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert exc_info.value.code != 0, (
            "SystemExit code must be non-zero when ANTHROPIC_API_KEY is set"
        )
        assert mock_alert.called, (
            "alert() must be called before halting when "
            "ANTHROPIC_API_KEY is set"
        )
        _, kwargs = mock_alert.call_args
        assert kwargs.get("severity") == "critical", (
            f"Expected severity='critical', got {kwargs!r}"
        )

    def test_anthropic_key_empty_string_treated_as_absent_no_halt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ANTHROPIC_API_KEY set to empty string → treated as absent; no halt.

        An empty string is equivalent to the variable being unset.  The
        guard must only fire for non-empty values (a real key was injected).
        """
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                mock_alert,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            # Must NOT raise — empty string is treated as absent.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # No critical alert for the ANTHROPIC_API_KEY guard must have fired.
        critical_calls = [
            c
            for c in mock_alert.call_args_list
            if c.kwargs.get("severity") == "critical"
        ]
        assert not critical_calls, (
            "Empty ANTHROPIC_API_KEY must not trigger the per-token-billing "
            f"guard; got critical alerts: {critical_calls}"
        )

    def test_alert_passes_issue_none_for_credential_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Credential-failure alert passes issue=None (repo-level).

        Uses the G3b guard path: ANTHROPIC_API_KEY present → halt, and the
        alert emitted before the halt must carry issue=None (repo-level,
        not tied to any specific issue).
        """
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        # Set the key to trigger the G3b billing guard.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                mock_alert,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        # The alert call must pass issue=None (positional arg index 2).
        alert_args, _ = mock_alert.call_args
        assert alert_args[2] is None, (
            "alert issue arg must be None for repo-level credential alerts; "
            f"got {alert_args[2]!r}"
        )


# ---------------------------------------------------------------------------
# G3 fatal ordering — credential failure must block G2 and G1
# ---------------------------------------------------------------------------


class TestG3FatalOrdering:
    """Credential check (G3) must run FIRST and abort before G2/G1."""

    def test_token_failure_prevents_marker_write_and_process_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """★ G3 fatal: token failure → marker NOT created, lister NOT called.

        When validate_daemon_token raises TokenValidationError, the marker
        file for G2 must NOT be written and the G1 process lister must
        NOT be called (G3 halts before them).
        """
        reconcile = _import_reconcile()

        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from baton_harness._auth import TokenValidationError

        mock_lister = MagicMock(return_value=[])
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        # Compute the expected marker path.
        marker = tmp_path / ".baton-harness" / "daemon.alive"

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                side_effect=TokenValidationError("no token"),
            ),
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                mock_lister,
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        # Marker must NOT have been created.
        assert not marker.exists(), (
            "daemon.alive marker must NOT be created when G3 fails fatally"
        )
        # Process lister must NOT have been called.
        mock_lister.assert_not_called()

    def test_anthropic_key_present_prevents_marker_write_and_process_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """★ G3 fatal: ANTHROPIC_API_KEY set; marker and lister skipped.

        When ANTHROPIC_API_KEY is non-empty (per-token-billing guard fires),
        the marker file for G2 must NOT be written and the G1 process lister
        must NOT be called — G3 halts before them.
        """
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        # Set the key to trigger the G3b guard.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value")

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
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert not marker.exists(), (
            "daemon.alive marker must NOT be created when ANTHROPIC_API_KEY "
            "is set (G3b billing guard fires before G2)"
        )
        mock_lister.assert_not_called()


# ---------------------------------------------------------------------------
# G2 — Ungraceful-prior-exit detection (marker file)
# ---------------------------------------------------------------------------


class TestG2UngracefulExitDetection:
    """Tests for check 2: ungraceful-prior-exit marker file (G2)."""

    def test_marker_absent_clean_boot_no_alert_marker_created(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clean boot (no marker) → no alert; marker file is created."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        marker = tmp_path / ".baton-harness" / "daemon.alive"

        assert not marker.exists(), "Precondition: marker must not exist"

        mock_alert = MagicMock(return_value=True)

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
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        assert marker.exists(), (
            "daemon.alive marker must be created on clean boot"
        )
        # No ungraceful-exit alert should have fired.
        ungraceful_calls = [
            c
            for c in mock_alert.call_args_list
            if "ungraceful" in str(c).lower() or "oom" in str(c).lower()
        ]
        assert not ungraceful_calls, (
            "No ungraceful-exit alert expected on clean boot; "
            f"got: {ungraceful_calls}"
        )

    def test_marker_present_prior_crash_emits_critical_alert_recreates_marker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Marker present at startup → critical alert + marker re-created."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        marker = tmp_path / ".baton-harness" / "daemon.alive"

        # Simulate a prior crash: marker already exists.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("alive", encoding="utf-8")
        assert marker.exists(), "Precondition: marker must already exist"

        mock_alert = MagicMock(return_value=True)

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
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # A critical alert about the prior crash must have fired.
        assert mock_alert.called, (
            "alert() must be called on prior-crash detect"
        )
        ungraceful_calls = [
            c
            for c in mock_alert.call_args_list
            if "ungraceful" in str(c).lower()
        ]
        assert ungraceful_calls, (
            "Expected an alert mentioning 'ungraceful' for prior-crash case; "
            f"alerts: {mock_alert.call_args_list}"
        )
        # Severity must be critical.
        _, kwargs = ungraceful_calls[0]
        assert kwargs.get("severity") == "critical", (
            f"Prior-crash alert must use severity='critical'; got {kwargs!r}"
        )
        # Marker must still exist (re-created for this run).
        assert marker.exists(), (
            "daemon.alive marker must be re-created after prior crash"
        )

    def test_prior_crash_alert_passes_issue_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Prior-crash alert passes issue=None (repo-level alert)."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        marker = tmp_path / ".baton-harness" / "daemon.alive"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("alive", encoding="utf-8")

        mock_alert = MagicMock(return_value=True)

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
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # Find the ungraceful-exit alert call and check issue arg.
        ungraceful_calls = [
            c
            for c in mock_alert.call_args_list
            if "ungraceful" in str(c).lower()
        ]
        assert ungraceful_calls
        alert_args, _ = ungraceful_calls[0]
        assert alert_args[2] is None, (
            f"Prior-crash alert must pass issue=None; got {alert_args[2]!r}"
        )

    def test_marker_path_uses_baton_harness_dir_not_symphony(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Marker is in .baton-harness/, NOT .symphony/."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

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
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # Only .baton-harness/daemon.alive must exist; not .symphony/.
        symphony_marker = tmp_path / ".symphony" / "daemon.alive"
        baton_marker = tmp_path / ".baton-harness" / "daemon.alive"

        assert baton_marker.exists(), (
            "Marker must be at .baton-harness/daemon.alive"
        )
        assert not symphony_marker.exists(), (
            "Marker must NOT be at .symphony/daemon.alive — wrong directory"
        )


# ---------------------------------------------------------------------------
# G1 — Orphan claude-process sweep
# ---------------------------------------------------------------------------


class TestG1OrphanProcessSweep:
    """Tests for check 3: orphan claude process sweep (G1)."""

    def test_no_stray_processes_no_alert(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No stray processes at boot → no warn alert emitted."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        mock_alert = MagicMock(return_value=True)

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
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # No orphan-process warn alert must have been emitted.
        warn_calls = [
            c
            for c in mock_alert.call_args_list
            if c.kwargs.get("severity") == "warn"
        ]
        assert not warn_calls, (
            "No warn alert expected when no stray processes; "
            f"got: {warn_calls}"
        )

    def test_stray_pids_emit_warn_alert_with_pid_list(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stray PIDs found → warn alert emitted containing the PID list."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        stray_pids = [1234, 5678]
        mock_alert = MagicMock(return_value=True)

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=stray_pids,
            ),
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # A warn alert must have been emitted.
        warn_calls = [
            c
            for c in mock_alert.call_args_list
            if c.kwargs.get("severity") == "warn"
        ]
        assert warn_calls, (
            "Expected a warn alert for stray PIDs; "
            f"all alerts: {mock_alert.call_args_list}"
        )
        # The PID list must appear in the summary.
        for pid in stray_pids:
            assert str(pid) in str(warn_calls[0]), (
                f"PID {pid} must appear in the orphan-process alert; "
                f"call: {warn_calls[0]}"
            )

    def test_stray_process_alert_passes_issue_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Orphan-process alert passes issue=None (repo-level)."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        mock_alert = MagicMock(return_value=True)

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[9999],
            ),
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        warn_calls = [
            c
            for c in mock_alert.call_args_list
            if c.kwargs.get("severity") == "warn"
        ]
        assert warn_calls
        alert_args, _ = warn_calls[0]
        assert alert_args[2] is None, (
            f"Orphan-process alert must pass issue=None; got {alert_args[2]!r}"
        )

    def test_lister_raises_is_suppressed_other_checks_continue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Process lister raising must be suppressed; G2 still completes."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

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
                side_effect=RuntimeError("pgrep unavailable"),
            ),
        ):
            # Must NOT raise — lister error is suppressed.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # G2 must still have run: marker must have been created.
        assert marker.exists(), (
            "G2 marker must be created even when G1 lister raises; "
            "lister failure must not abort G2"
        )


# ---------------------------------------------------------------------------
# Per-check isolation — non-fatal check failure must not abort siblings
# ---------------------------------------------------------------------------


class TestPerCheckIsolation:
    """★ Each non-fatal check is independently guarded."""

    def test_g2_internal_error_does_not_abort_g1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G2 marker-write raising → G1 process lister still called."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        mock_lister = MagicMock(return_value=[])
        mock_alert = MagicMock(return_value=True)

        # Make marker directory un-writable by patching Path.write_text to
        # raise inside the reconcile module — only for writes to daemon.alive.
        # We achieve this by patching the marker write via the module's
        # Path operations: patch Path.write_text to raise on the marker path.
        original_write_text = Path.write_text

        def failing_write_text(
            self: Path,
            data: str,
            *args: Any,  # noqa: ANN401
            **kwargs: Any,  # noqa: ANN401
        ) -> None:
            if self.name == "daemon.alive":
                raise OSError("disk full")
            return original_write_text(self, data, *args, **kwargs)

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                mock_lister,
            ),
            patch.object(Path, "write_text", failing_write_text),
        ):
            # Must NOT raise — G2 failure is non-fatal.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # G1 lister must still have been called.
        mock_lister.assert_called_once()

    def test_g1_error_does_not_prevent_daemon_continuing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G1 lister error → reconcile_startup returns normally (non-fatal)."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

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
                side_effect=PermissionError("no pgrep access"),
            ),
        ):
            # Must return normally — G1 failure is non-fatal.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )
        # If we reach here the test passes.


# ---------------------------------------------------------------------------
# Module contract pin: _list_claude_procs is a module-level injectable seam
# ---------------------------------------------------------------------------


class TestProcessListerSeam:
    """Pin the injectable seam: _list_claude_procs at module level."""

    def test_list_claude_procs_is_module_level_callable(self) -> None:
        """Module must expose _list_claude_procs as a callable."""
        reconcile = _import_reconcile()

        assert hasattr(reconcile, "_list_claude_procs"), (
            "reconcile module must expose _list_claude_procs at module level"
        )
        assert callable(reconcile._list_claude_procs), (
            "_list_claude_procs must be callable"
        )

    def test_list_claude_procs_returns_list_of_ints(self) -> None:
        """_list_claude_procs() must return list[int] (or empty list)."""
        reconcile = _import_reconcile()

        # The default implementation must be callable and return a list.
        # It may raise on this platform (Windows, no pgrep) — that is
        # acceptable; the suppression test above covers that path.
        # We only assert the return type on a successful call.
        try:
            result = reconcile._list_claude_procs()
        except Exception:  # noqa: BLE001
            # Platform-level failure is acceptable — not a contract break.
            return

        assert isinstance(result, list), (
            f"_list_claude_procs must return list; got {type(result)}"
        )
        for item in result:
            assert isinstance(item, int), (
                f"_list_claude_procs must return list[int]; got item {item!r}"
            )


# ---------------------------------------------------------------------------
# Marker-path constant pin
# ---------------------------------------------------------------------------


class TestMarkerPathConstant:
    """Pin the daemon.alive marker path convention."""

    def test_marker_path_constant_exposed_or_derivable(self) -> None:
        """Module must reference the 'daemon.alive' marker filename."""
        reconcile = _import_reconcile()

        # The implementer may expose MARKER_FILENAME, ALIVE_MARKER, or
        # compute the path at runtime.  We check one of:
        # - a module constant whose value ends in 'daemon.alive'
        # - the module source references 'daemon.alive' (verified by
        #   checking str on the module file).
        found = False
        for attr_name in dir(reconcile):
            attr = getattr(reconcile, attr_name)
            if isinstance(attr, (str, Path)) and "daemon.alive" in str(attr):
                found = True
                break
        # Also allow a module-level string constant named _ALIVE_MARKER,
        # ALIVE_MARKER, or MARKER_FILE, etc.
        if not found:
            module_file = getattr(reconcile, "__file__", None)
            if module_file is not None:
                content = Path(module_file).read_text(encoding="utf-8")
                found = "daemon.alive" in content
        assert found, (
            "reconcile module must reference the literal 'daemon.alive' "
            "marker path (either as a module constant or inline string)"
        )


# ---------------------------------------------------------------------------
# Bug 1 — reconcile_startup must accept installation_token by-value
#
# Required behaviour (codex P1 #154):
#   reconcile_startup(installation_token: str) must accept the minted
#   ghs_ token as a parameter and validate THAT token, not os.environ.
#
# Current behaviour that causes tests to FAIL:
#   reconcile_startup() has no installation_token parameter — calling it
#   with the kwarg raises TypeError.  Even if the kwarg existed, the
#   implementation reads GH_TOKEN/GITHUB_TOKEN from os.environ instead
#   of the threaded value, so the validator receives the ambient env
#   credential, not the minted token.
# ---------------------------------------------------------------------------


class TestReconcileStartupAcceptsInstallationToken:
    """RED: reconcile_startup must thread installation_token by-value.

    These tests fail until the implementation:
    1. Adds ``installation_token: str`` as a parameter to
       ``reconcile_startup``.
    2. Passes that value to ``validate_daemon_token`` instead of reading
       ``os.environ`` internally.
    3. ``cli.main()`` passes the minted token into ``reconcile_startup``.
    """

    def test_reconcile_startup_accepts_installation_token_kwarg(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile_startup(installation_token=...) must not raise TypeError.

        The contract requires the function signature to accept
        ``installation_token`` as a keyword argument.  Currently the
        parameter is absent, so calling with it raises ``TypeError``.

        Args:
            tmp_path: Pytest per-test temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        reconcile = _import_reconcile()

        # Deliberately do NOT set GH_TOKEN/GITHUB_TOKEN in os.environ so
        # that any ambient-env read would fail via TokenValidationError.
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ) as mock_validator,
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            # Must accept installation_token without TypeError.
            asyncio.run(
                reconcile.reconcile_startup(
                    repo_cfgs,
                    obs,
                    runlog=None,
                    installation_token="ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                )
            )

        # The validator must have been called with the threaded token value.
        assert mock_validator.called, (
            "validate_daemon_token must be called during reconcile_startup"
        )
        call_args = mock_validator.call_args
        # The first positional arg (or any arg) must equal the minted token.
        token_seen = (
            call_args.args[0]
            if call_args.args
            else next(iter(call_args.kwargs.values()), None)
        )
        assert token_seen == "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", (
            f"validate_daemon_token must receive the threaded installation "
            f"token, got: {token_seen!r}"
        )

    def test_reconcile_startup_ignores_ambient_env_when_token_threaded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Threaded token is validated; ambient GH_TOKEN is ignored.

        Even when GH_TOKEN=garbage is set in the environment, the
        ``validate_daemon_token`` call must receive the threaded
        ``installation_token`` value — not the ambient env value.

        Args:
            tmp_path: Pytest per-test temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        reconcile = _import_reconcile()

        # Poison the ambient env with an invalid value.
        monkeypatch.setenv("GH_TOKEN", "garbage_not_a_real_token")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        received_tokens: list[str] = []

        def _capture_validate(token: str) -> None:
            received_tokens.append(token)

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                side_effect=_capture_validate,
            ),
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            asyncio.run(
                reconcile.reconcile_startup(
                    repo_cfgs,
                    obs,
                    runlog=None,
                    installation_token="ghs_VALID_TOKEN",
                )
            )

        assert received_tokens, (
            "validate_daemon_token must be called during reconcile_startup"
        )
        assert received_tokens[0] == "ghs_VALID_TOKEN", (
            "reconcile_startup must pass the threaded installation_token "
            f"to validate_daemon_token; received {received_tokens[0]!r} "
            "instead — the ambient GH_TOKEN=garbage must be ignored"
        )

    def test_reconcile_startup_accepts_refreshable_provider(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile_startup must validate a resolved token from a provider."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", "garbage_not_a_real_token")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        received_tokens: list[str] = []

        class FakeProvider:
            """Minimal token provider used to model refreshable auth."""

            def get_token(self) -> str:
                return "ghs_PROVIDER_TOKEN"

        def _capture_validate(token: str) -> None:
            received_tokens.append(token)

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                side_effect=_capture_validate,
            ),
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            asyncio.run(
                reconcile.reconcile_startup(
                    repo_cfgs,
                    obs,
                    runlog=None,
                    installation_token=FakeProvider(),
                )
            )

        assert received_tokens == ["ghs_PROVIDER_TOKEN"], (
            "reconcile_startup must resolve the threaded provider and "
            f"validate its token; got {received_tokens!r}"
        )


# ---------------------------------------------------------------------------
# Gap 3 — reconcile_startup alert() calls must thread installation_token
# (codex 3347f83 P2)
# ---------------------------------------------------------------------------


class TestReconcileStartupAlertsThreadToken:
    """Gap 3: all alert() calls inside reconcile_startup must forward token.

    Each gate (G3a, G3b, G3c, G2) fires alert() on failure.  Currently none
    of those calls pass installation_token=, so the GitHub comment posts
    under ambient credentials rather than the App token.

    All tests in this class currently FAIL because reconcile.py:139,
    reconcile.py:157, reconcile.py:183, and reconcile.py:199 call alert()
    without installation_token=.
    """

    def test_g3b_anthropic_api_key_alert_passes_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G3b guard: alert() must receive installation_token= on key present.

        Sets ANTHROPIC_API_KEY to a non-empty value to trigger the G3b
        billing guard.  Patches alert() at the reconcile module.  Asserts
        that every alert() call carries installation_token= equal to the
        threaded value.

        Currently FAILS because reconcile.py:157 calls alert() without
        installation_token=.

        Args:
            tmp_path: Pytest per-test temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        reconcile = _import_reconcile()

        _token = "ghs_TEST_G3b_alert_token"
        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        # Trigger G3b: non-empty ANTHROPIC_API_KEY.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-billing")

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

        assert alert_calls, (
            "alert() must be called on G3b failure before sys.exit"
        )
        for call in alert_calls:
            got = call["kwargs"].get("installation_token")
            assert got == _token, (
                "G3b alert() call must forward installation_token= to "
                f"alert(); expected {_token!r}, got {got!r}"
            )

    def test_g3c_oauth_cred_missing_alert_passes_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G3c: alert() must receive installation_token= when cred absent.

        Points _OAUTH_CRED_PATH at a non-existent path to trigger the G3c
        guard.  Patches alert() to record calls.  Asserts installation_token=
        is forwarded.

        Currently FAILS because reconcile.py:183 calls alert() without
        installation_token=.

        Args:
            tmp_path: Pytest per-test temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        reconcile = _import_reconcile()

        _token = "ghs_TEST_G3c_alert_token"
        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Override the autouse fixture: point at an absent path to trigger G3c.
        monkeypatch.setattr(
            "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
            tmp_path / "absent_credentials.json",
        )

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

        assert alert_calls, (
            "alert() must be called on G3c failure before sys.exit"
        )
        for call in alert_calls:
            got = call["kwargs"].get("installation_token")
            assert got == _token, (
                "G3c alert() call must forward installation_token= to "
                f"alert(); expected {_token!r}, got {got!r}"
            )

    def test_g3a_invalid_token_alert_passes_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G3a guard: alert() must receive installation_token= on bad token.

        Passes a malformed token so validate_daemon_token raises
        TokenValidationError (G3a).  Patches alert() to record calls.
        Asserts installation_token= is forwarded — either the invalid
        token value itself or the threaded value, whichever the
        implementation chooses to propagate.

        Currently FAILS because reconcile.py:139 calls alert() without
        installation_token=.

        Args:
            tmp_path: Pytest per-test temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        from baton_harness._auth import TokenValidationError  # noqa: PLC0415

        reconcile = _import_reconcile()

        _token = "not_a_ghs_token_at_all"
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
                side_effect=TokenValidationError("bad token format"),
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                side_effect=_capture_alert,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
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

        assert alert_calls, (
            "alert() must be called on G3a (token validation) failure"
        )
        # The alert must carry the threaded installation_token kwarg.
        # Any non-empty token value forwarded is acceptable; the key
        # invariant is that the kwarg is present (not absent/empty default).
        for call in alert_calls:
            got = call["kwargs"].get("installation_token")
            assert got is not None and got != "", (
                "G3a alert() call must forward installation_token= to "
                f"alert(); expected a non-empty value, got {got!r}"
            )

    def test_g2_marker_present_alert_passes_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G2: alert() must receive installation_token= when marker exists.

        Pre-creates the daemon.alive marker so the G2 ungraceful-exit alert
        fires.  Patches alert() to record calls.  Asserts installation_token=
        is forwarded.

        Currently FAILS because reconcile.py:199 calls alert() without
        installation_token=.

        Args:
            tmp_path: Pytest per-test temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        reconcile = _import_reconcile()

        _token = "ghs_TEST_G2_alert_token"
        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Pre-create the marker file so G2 detects an ungraceful prior exit.
        harness_dir = tmp_path / ".baton-harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        marker = harness_dir / "daemon.alive"
        marker.write_text("alive", encoding="utf-8")

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
        ):
            # G2 is non-fatal — reconcile_startup must not raise here.
            asyncio.run(
                reconcile.reconcile_startup(
                    repo_cfgs,
                    obs,
                    runlog=None,
                    installation_token=_token,
                )
            )

        # Find the G2-specific alert call (critical severity, no issue number).
        g2_calls = [
            c
            for c in alert_calls
            if c["kwargs"].get("severity") == "critical"
            and c["args"][2] is None
        ]
        assert g2_calls, (
            "A critical alert with issue=None must be emitted when the G2 "
            "marker is present; none found in alert_calls: "
            f"{alert_calls!r}"
        )
        for call in g2_calls:
            got = call["kwargs"].get("installation_token")
            assert got == _token, (
                "G2 alert() call must forward installation_token= to "
                f"alert(); expected {_token!r}, got {got!r}"
            )


# ---------------------------------------------------------------------------
# Phase 4 (#193): doctor.run_gate(ctx, Phase.POST_BOOTSTRAP) wired into
# reconcile_startup, after the native G3a/b/c/d block and before G2.
# ---------------------------------------------------------------------------


def _assert_run_gate_called_with_post_bootstrap(gate_mock: MagicMock) -> None:
    """Assert ``run_gate`` was invoked once, with ``Phase.POST_BOOTSTRAP``.

    Tolerates either a positional (``run_gate(ctx, Phase.POST_BOOTSTRAP)``)
    or keyword (``run_gate(ctx, phase=Phase.POST_BOOTSTRAP)``) call shape,
    mirroring ``test_cli_doctor_gate.py``'s
    ``_assert_run_gate_called_with_pre_bootstrap`` helper for the Phase-3
    gate, so this test pins the observable phase argument, not the call
    convention the implementer chooses.

    Args:
        gate_mock: The mock standing in for ``doctor.run_gate``.
    """
    gate_mock.assert_called_once()
    call = gate_mock.call_args
    phase_arg = call.kwargs.get("phase")
    if phase_arg is None and len(call.args) >= 2:
        phase_arg = call.args[1]
    assert phase_arg is Phase.POST_BOOTSTRAP, (
        "run_gate must be called with phase=Phase.POST_BOOTSTRAP from "
        f"reconcile_startup, got {phase_arg!r} (call={call!r})"
    )


def _extract_ctx_from_gate_call(call: Any) -> Any:  # noqa: ANN401
    """Return the ``DoctorContext`` positional/keyword arg of a gate call.

    Args:
        call: A ``unittest.mock.call`` object captured from a patched
            ``doctor.run_gate``.

    Returns:
        The ``ctx`` argument, however it was passed.
    """
    ctx = call.kwargs.get("ctx")
    if ctx is None and call.args:
        ctx = call.args[0]
    return ctx


class TestPostBootstrapDoctorGate:
    """Phase 4 (#193): the Phase-B hard gate folded into reconcile_startup.

    Covers the plan's section 8 Phase B bullet and section 4's placement
    rationale: ``reconcile_startup`` must call ``doctor.run_gate(ctx,
    Phase.POST_BOOTSTRAP)`` strictly AFTER the native G3a/b/c/d credential
    block and strictly BEFORE G2 (the ungraceful-prior-exit marker check)
    -- never as ``reconcile_startup``'s first step, so
    ``bin/verify-recovery.sh``'s G3a/G3b stderr greps are never
    suppressed by a ruleset/label failure short-circuiting first.

    Patch-target note: mirrors ``test_cli_doctor_gate.py``'s Phase-3
    precedent -- ``doctor.run_gate`` is patched at its defining module
    (``baton_harness.chain.doctor.run_gate``), on the assumption
    ``reconcile.py`` calls it as ``doctor.run_gate(...)`` after a
    ``from baton_harness.chain import doctor`` import. If the
    implementation instead imports the bare name, this patch target (and
    the ``_patch_doctor_run_gate`` autouse fixture above) will need to
    move to ``baton_harness.chain.reconcile.run_gate`` -- flagged in the
    return summary.
    """

    def test_gate_called_with_post_bootstrap_phase(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile_startup calls the gate with Phase.POST_BOOTSTRAP."""
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

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
                "baton_harness.chain.doctor.run_gate",
                return_value=None,
            ) as gate_mock,
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        _assert_run_gate_called_with_post_bootstrap(gate_mock)

    def test_gate_ctx_carries_reconcile_startups_installation_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The DoctorContext passed to the gate threads installation_token.

        Section 3/4: the minted App token is threaded by value into
        ``DoctorContext.installation_token`` -- never read from
        ``os.environ``.
        """
        reconcile = _import_reconcile()

        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        token = "ghs_POST_BOOTSTRAP_GATE_TEST_TOKEN_xxxxxxxxxxxxxxxxx"
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
                "baton_harness.chain.doctor.run_gate",
                return_value=None,
            ) as gate_mock,
        ):
            asyncio.run(
                reconcile.reconcile_startup(
                    repo_cfgs,
                    obs,
                    runlog=None,
                    installation_token=token,
                )
            )

        gate_mock.assert_called_once()
        ctx = _extract_ctx_from_gate_call(gate_mock.call_args)
        assert ctx is not None, "run_gate must be called with a DoctorContext"
        assert getattr(ctx, "installation_token", None) == token, (
            "DoctorContext.installation_token must carry "
            "reconcile_startup's own installation_token parameter (by "
            f"value); got {ctx!r}"
        )

    def test_gate_not_invoked_when_g3a_token_check_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A G3a (token) failure halts before the POST_BOOTSTRAP gate runs.

        Pins the "not reconcile_startup's first step" placement invariant
        (plan section 4/8): the native G3 credential gates must fire and
        halt BEFORE run_gate(POST_BOOTSTRAP) is ever reached, so a
        misconfigured-ruleset repo can never suppress the G3a/G3b alert
        text ``bin/verify-recovery.sh`` greps for.

        NOTE: this assertion passes vacuously today (the gate isn't wired
        in at all yet, so it is trivially "not invoked" for any input);
        it becomes a real green-phase regression guard once Phase 4 wires
        the gate in. It is not counted as a red-confirming test on its
        own -- see the return summary.
        """
        reconcile = _import_reconcile()
        from baton_harness._auth import TokenValidationError  # noqa: PLC0415

        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                side_effect=TokenValidationError("no token found"),
            ),
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.doctor.run_gate",
                return_value=None,
            ) as gate_mock,
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        gate_mock.assert_not_called()

    def test_gate_runs_after_full_g3_block_and_before_g2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gate call order: G3a -> G3d -> POST_BOOTSTRAP gate -> G2 -> G1.

        The load-bearing placement test (plan section 4/8): asserts the
        gate runs strictly after the native G3a (token) and G3d (git
        credential helper) checks, and strictly before G2 writes the
        ``daemon.alive`` marker. Proven via a call-order list plus an
        in-gate assertion that the marker does not exist yet (rather than
        only checking call order), so a bug that reorders the *marker
        write* relative to the gate -- without touching mock call order
        -- would still be caught.
        """
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        call_order: list[str] = []
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        marker = tmp_path / ".baton-harness" / "daemon.alive"

        def _fake_validate(token: str) -> None:
            call_order.append("g3a")

        def _fake_git_helpers() -> list[str]:
            call_order.append("g3d")
            return ["!fake credential helper for tests"]

        def _fake_gate(*args: object, **kwargs: object) -> None:
            call_order.append("gate")
            assert not marker.exists(), (
                "POST_BOOTSTRAP gate ran AFTER G2 already wrote the "
                "daemon.alive marker -- it must run strictly BEFORE G2"
            )

        def _fake_list_procs() -> list[int]:
            call_order.append("g1")
            return []

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                side_effect=_fake_validate,
            ),
            patch(
                "baton_harness.chain.reconcile._get_git_credential_helpers",
                side_effect=_fake_git_helpers,
            ),
            patch("baton_harness.chain.reconcile.alert", return_value=True),
            patch(
                "baton_harness.chain.doctor.run_gate",
                side_effect=_fake_gate,
            ) as gate_mock,
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                side_effect=_fake_list_procs,
            ),
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        gate_mock.assert_called_once()
        assert marker.exists(), (
            "G2 must still run (and create the marker) after the gate"
        )
        assert call_order == ["g3a", "g3d", "gate", "g1"], (
            "expected order G3a -> G3d -> POST_BOOTSTRAP gate -> G2 "
            "(marker write, asserted separately above) -> G1; got "
            f"{call_order!r}"
        )

    def test_critical_gate_failure_alerts_and_exits_1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A CRITICAL POST_BOOTSTRAP gate failure alerts then exits 1.

        Section 8: "A CRITICAL FAIL emits alert(..., severity='critical')
        then sys.exit(1), matching the existing G3a/b/c/d fatal pattern."
        Simulates the CRITICAL fail via ``run_gate``'s own documented
        contract (raises ``SystemExit(1)``) rather than constructing a
        real failing ``DoctorContext`` -- ``run_gate``'s own
        check-selection/short-circuit behavior is already exhaustively
        covered by ``test_doctor.py``; this test only proves
        ``reconcile_startup`` reacts to that contract by emitting its own
        repo-level critical alert (mirroring G3a-d) before the process
        exits non-zero. Only ``severity`` is asserted on the alert call,
        never message text (the exact wording is an implementation
        choice).
        """
        reconcile = _import_reconcile()

        monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        mock_alert = MagicMock(return_value=True)

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                return_value=None,
            ),
            patch("baton_harness.chain.reconcile.alert", mock_alert),
            patch(
                "baton_harness.chain.doctor.run_gate",
                side_effect=SystemExit(1),
            ) as gate_mock,
        ):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert exc_info.value.code != 0, (
            "reconcile_startup must exit non-zero when the POST_BOOTSTRAP "
            "gate reports a CRITICAL failure"
        )
        gate_mock.assert_called_once()
        assert mock_alert.called, (
            "alert() must be called (mirrors the G3a-d fatal pattern) "
            "when the POST_BOOTSTRAP gate CRITICAL-fails"
        )
        _, kwargs = mock_alert.call_args
        assert kwargs.get("severity") == "critical", (
            f"Expected severity='critical', got {kwargs!r}"
        )
