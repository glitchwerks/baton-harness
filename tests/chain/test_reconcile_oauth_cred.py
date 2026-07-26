"""Tests for the OAuth credential-volume health-check in reconcile_startup.

Spec (issue #108): ``reconcile_startup`` must validate the OAuth credential
file at daemon startup:

- G3c: Present and readable → check passes (no alert, startup proceeds).
- G3c: Absent → critical ``alert()`` + ``sys.exit(1)`` (fatal, same
  mechanism as the G3a GH-token gate and G3b ANTHROPIC_API_KEY guard).
- G3c: Present but unreadable (``open()`` raises) → critical alert + fatal.
- Structural-only: the check MUST NOT parse/read credential contents; a
  file containing invalid JSON must still pass when it is readable.

The check path defaults to ``~/.claude/.credentials.json`` but must
honour an overridable/configured path if the project exposes one.
We test the behaviour via the module-level seam rather than the
default path to avoid touching real credentials.

All I/O is mocked.  ``validate_daemon_token``, ``alert``, and the
credential-file open are each patched per-test so no real files or
network calls are made.

Test conventions mirror the existing ``test_reconcile.py``:
- Lazy import via ``_import_reconcile()`` so a missing symbol fails with
  a clean ``AttributeError``/``AssertionError``, not a collection error.
- ``asyncio.run(reconcile.reconcile_startup(...))`` (no pytest-asyncio).
- Fatal signal: ``pytest.raises(SystemExit)`` with non-zero code.
- Alert assertions via ``MagicMock`` on
  ``baton_harness.chain.reconcile.alert``.
"""

from __future__ import annotations

import asyncio
import builtins
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirrored from test_reconcile.py to keep this file self-contained)
# ---------------------------------------------------------------------------

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
def _patch_doctor_run_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the POST_BOOTSTRAP doctor gate for every test in this file.

    Phase 4 (#193) wires ``doctor.run_gate(ctx, Phase.POST_BOOTSTRAP)``
    into ``reconcile_startup``, after the native G3d block and before G2.
    This file's tests call ``reconcile.reconcile_startup(...)`` directly
    and pre-date that gate; without this stub they would hit the real
    ruleset/label/repo-admin checks (unreachable in this test
    environment) once Phase 4 lands, breaking every G3c test in this
    file. Dedicated POST_BOOTSTRAP gate tests live in
    ``test_reconcile.py::TestPostBootstrapDoctorGate``, not here.

    Patched at ``doctor.run_gate``'s own defining module, matching
    ``test_reconcile.py``'s identical fixture -- see that fixture's
    docstring for the dotted-import assumption and its fallback patch
    target.

    Args:
        monkeypatch: Pytest monkeypatch fixture for attribute patching.
    """
    monkeypatch.setattr(
        "baton_harness.chain.doctor.run_gate",
        lambda *args, **kwargs: None,
    )


def _patch_passing_prereqs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set env vars so the G3a/G3b gates pass without touching G3c.

    Removes ANTHROPIC_API_KEY (must be absent) and sets GH_TOKEN to
    a fake ghs_ installation token so the token-type check in
    validate_daemon_token passes before we patch it out.

    Also neutralises G3d (git push credential-helper presence, #219)
    so tests focused on G3c are not sensitive to the runner's real git
    configuration — mirrors the same neutralisation applied in
    test_reconcile.py for tests that pre-date a given gate. Tests that
    specifically validate G3d live in
    test_reconcile_git_credential_helper.py.
    """
    monkeypatch.setenv("GH_TOKEN", _INSTALLATION_TOKEN)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "baton_harness.chain.reconcile._get_git_credential_helpers",
        lambda: ["!fake credential helper for tests"],
    )


# ---------------------------------------------------------------------------
# G3c: credential file present and readable — happy path
# ---------------------------------------------------------------------------


class TestG3cCredentialFilePresent:
    """Credential file present and readable → no alert, startup proceeds."""

    def test_credential_file_readable_no_alert_no_halt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Readable credential file → check passes silently.

        The file may contain any bytes (even invalid JSON) — the check
        must never parse it.  We supply invalid JSON to prove the check
        does not call ``json.load`` or similar.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        # Seam must exist before this test is meaningful.
        assert hasattr(reconcile, "_OAUTH_CRED_PATH"), (
            "reconcile must expose _OAUTH_CRED_PATH seam "
            "(test_module_exposes_oauth_cred_path_seam pins this)"
        )

        # Create the fake credential file with intentionally invalid JSON
        # to prove the check is structural-only (no parsing).
        cred_file = tmp_path / "credentials.json"
        cred_file.write_text(
            "THIS IS NOT JSON {{{{ invalid %%%%",
            encoding="utf-8",
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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                cred_file,
            ),
        ):
            # Must NOT raise SystemExit or any exception.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        # No critical alert must have fired for the credential check.
        critical_calls = [
            c
            for c in mock_alert.call_args_list
            if c.kwargs.get("severity") == "critical"
        ]
        assert not critical_calls, (
            "Readable credential file must not trigger a critical alert; "
            f"got: {critical_calls}"
        )

    def test_credential_file_readable_invalid_json_still_passes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid JSON content + readable file → check PASSES (structural).

        The spec mandates the check never reads or parses contents.
        A file with invalid JSON that is still openable must pass the
        health-check, proving no ``json.load`` / ``json.loads`` is called.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        # Seam must exist before this test is meaningful.
        assert hasattr(reconcile, "_OAUTH_CRED_PATH"), (
            "reconcile must expose _OAUTH_CRED_PATH seam "
            "(test_module_exposes_oauth_cred_path_seam pins this)"
        )

        cred_file = tmp_path / "credentials.json"
        # Write bytes that are guaranteed to cause json.loads to raise.
        cred_file.write_bytes(b"\xff\xfe THIS IS NOT UTF-8 JSON\x00\x01")

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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                cred_file,
            ),
        ):
            # Must NOT raise — invalid content must not matter to a
            # structural-only check.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )
        # Reaching here is the assertion: no exception was raised.


# ---------------------------------------------------------------------------
# G3c: credential file absent — fatal
# ---------------------------------------------------------------------------


class TestG3cCredentialFileAbsent:
    """Credential file absent → critical alert + SystemExit (fatal)."""

    def test_absent_credential_file_emits_critical_alert_and_halts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing credential file → critical alert fired + sys.exit(1).

        Mirrors test_reconcile.py::TestG3::
        test_missing_gh_token_emits_critical_alert_and_halts exactly.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        # Deliberate non-existent path — must not be created by the test.
        missing_cred = tmp_path / "nonexistent" / "credentials.json"
        assert not missing_cred.exists(), "Precondition: file must not exist"

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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                missing_cred,
                create=True,
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert exc_info.value.code != 0, (
            "SystemExit code must be non-zero when credential file is absent"
        )
        assert mock_alert.called, (
            "alert() must be called before halting on absent credential file"
        )
        _, kwargs = mock_alert.call_args
        assert kwargs.get("severity") == "critical", (
            "Absent credential file must trigger severity='critical'; "
            f"got {kwargs!r}"
        )

    def test_absent_credential_file_alert_passes_issue_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Absent credential file alert carries issue=None (repo-level).

        Mirrors the issue=None pattern from the G3a/G3b gate tests so
        the alert is correctly tagged as repo-level, not tied to an issue.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        missing_cred = tmp_path / "missing" / "credentials.json"
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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                missing_cred,
                create=True,
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        alert_args, _ = mock_alert.call_args
        assert alert_args[2] is None, (
            "Absent credential file alert must pass issue=None (positional "
            f"arg index 2); got {alert_args[2]!r}"
        )

    def test_absent_credential_prevents_g2_marker_and_g1_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G3c fatal (absent file) → G2 marker NOT written, G1 scan skipped.

        The credential check must run before G2/G1; a fatal here must
        prevent both the marker write and the process scan.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        missing_cred = tmp_path / "missing" / "credentials.json"
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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                missing_cred,
                create=True,
            ),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert not marker.exists(), (
            "daemon.alive marker must NOT be created when G3c fails fatally"
        )
        mock_lister.assert_not_called()


# ---------------------------------------------------------------------------
# G3c: credential file present but unreadable — fatal
# ---------------------------------------------------------------------------


class TestG3cCredentialFileUnreadable:
    """Credential file present but unreadable → critical alert + fatal."""

    def test_unreadable_credential_file_emits_critical_alert_and_halts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Permission error on open() → critical alert + sys.exit(1).

        We simulate an unreadable file by patching ``open`` (or the
        Path.open method used by the check) to raise ``PermissionError``
        only for the credential path.  The check must treat any OSError
        on open as unreadable and fire the fatal path.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        # File must physically exist so the presence test passes, but
        # the open() call will be patched to raise.
        cred_file = tmp_path / "credentials.json"
        cred_file.write_text("placeholder", encoding="utf-8")

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        original_open = builtins.open

        def _selective_open(
            file: Any,  # noqa: ANN401
            mode: str = "r",
            *args: Any,  # noqa: ANN401
            **kwargs: Any,  # noqa: ANN401
        ) -> Any:  # noqa: ANN401
            """Raise PermissionError only for the credential path."""
            try:
                path_str = str(file)
            except Exception:  # noqa: BLE001
                path_str = ""
            if "credentials.json" in path_str and mode in ("r", "rb", ""):
                raise PermissionError(
                    f"[Errno 13] Permission denied: '{file}'"
                )
            return original_open(file, mode, *args, **kwargs)

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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                cred_file,
                create=True,
            ),
            patch("builtins.open", side_effect=_selective_open),
        ):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert exc_info.value.code != 0, (
            "SystemExit code must be non-zero when credential file is "
            "unreadable"
        )
        assert mock_alert.called, (
            "alert() must be called before halting on unreadable credential "
            "file"
        )
        _, kwargs = mock_alert.call_args
        assert kwargs.get("severity") == "critical", (
            "Unreadable credential file must trigger severity='critical'; "
            f"got {kwargs!r}"
        )

    def test_unreadable_credential_file_alert_passes_issue_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unreadable credential file alert carries issue=None (repo-level)."""
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        cred_file = tmp_path / "credentials.json"
        cred_file.write_text("placeholder", encoding="utf-8")

        mock_alert = MagicMock(return_value=True)
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]

        original_open = builtins.open

        def _raise_on_cred(
            file: Any,  # noqa: ANN401
            mode: str = "r",
            *args: Any,  # noqa: ANN401
            **kwargs: Any,  # noqa: ANN401
        ) -> Any:  # noqa: ANN401
            if "credentials.json" in str(file) and mode in ("r", "rb", ""):
                raise PermissionError("denied")
            return original_open(file, mode, *args, **kwargs)

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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                cred_file,
                create=True,
            ),
            patch("builtins.open", side_effect=_raise_on_cred),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        alert_args, _ = mock_alert.call_args
        assert alert_args[2] is None, (
            "Unreadable credential file alert must pass issue=None; "
            f"got {alert_args[2]!r}"
        )

    def test_unreadable_credential_prevents_g2_marker_and_g1_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unreadable file → G2 marker NOT written, G1 lister NOT called."""
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        cred_file = tmp_path / "credentials.json"
        cred_file.write_text("placeholder", encoding="utf-8")

        mock_lister = MagicMock(return_value=[])
        obs = _make_obs(tmp_path)
        repo_cfgs = [_make_repo_cfg(tmp_path)]
        marker = tmp_path / ".baton-harness" / "daemon.alive"

        original_open = builtins.open

        def _raise_on_cred(
            file: Any,  # noqa: ANN401
            mode: str = "r",
            *args: Any,  # noqa: ANN401
            **kwargs: Any,  # noqa: ANN401
        ) -> Any:  # noqa: ANN401
            if "credentials.json" in str(file) and mode in ("r", "rb", ""):
                raise PermissionError("denied")
            return original_open(file, mode, *args, **kwargs)

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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                cred_file,
                create=True,
            ),
            patch("builtins.open", side_effect=_raise_on_cred),
        ):
            with pytest.raises(SystemExit):
                asyncio.run(
                    reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
                )

        assert not marker.exists(), (
            "daemon.alive marker must NOT be created when G3c fails fatally "
            "(unreadable credential)"
        )
        mock_lister.assert_not_called()


# ---------------------------------------------------------------------------
# G3c: structural-only — check must not read or parse file contents
# ---------------------------------------------------------------------------


class TestG3cStructuralOnly:
    """Confirm the check is structural: presence + readability only."""

    def test_check_does_not_require_valid_json_content(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A completely empty credential file still passes.

        An empty file is trivially openable (readable), so the check must
        pass.  The check is allowed to open and immediately close the
        file without reading any bytes — it must NOT reject an empty file.
        """
        reconcile = _import_reconcile()
        _patch_passing_prereqs(monkeypatch)

        # Seam must exist before this test is meaningful.
        assert hasattr(reconcile, "_OAUTH_CRED_PATH"), (
            "reconcile must expose _OAUTH_CRED_PATH seam "
            "(test_module_exposes_oauth_cred_path_seam pins this)"
        )

        cred_file = tmp_path / "credentials.json"
        # Write empty file — zero bytes; json.loads("") would raise ValueError.
        cred_file.write_bytes(b"")

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
                "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
                cred_file,
            ),
        ):
            # Must NOT raise — empty file is readable.
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        critical_calls = [
            c
            for c in mock_alert.call_args_list
            if c.kwargs.get("severity") == "critical"
        ]
        assert not critical_calls, (
            "Empty credential file (readable) must not trigger a critical "
            f"alert; got: {critical_calls}"
        )

    def test_module_exposes_oauth_cred_path_seam(self) -> None:
        """Module must expose _OAUTH_CRED_PATH as a module-level attribute.

        This attribute is the seam for overriding the credential path in
        tests and in configured deployments.  Without it there is no safe
        way to test the check without touching ``~/.claude/.credentials.json``.
        """
        reconcile = _import_reconcile()

        assert hasattr(reconcile, "_OAUTH_CRED_PATH"), (
            "reconcile module must expose _OAUTH_CRED_PATH at module level "
            "so the credential path is overridable in tests/config"
        )
        path_val = reconcile._OAUTH_CRED_PATH
        assert isinstance(path_val, Path), (
            f"_OAUTH_CRED_PATH must be a pathlib.Path; got {type(path_val)}"
        )

    def test_default_cred_path_points_to_dot_claude_dir(self) -> None:
        """Default _OAUTH_CRED_PATH must resolve under ~/.claude/.

        The spec says the default is ``~/.claude/.credentials.json``.
        We assert that the default path's parent directory name is
        ``.claude`` so the convention is documented in the module — without
        inspecting what the file contains.
        """
        reconcile = _import_reconcile()

        path_val: Path = getattr(reconcile, "_OAUTH_CRED_PATH", None)
        if path_val is None:
            pytest.skip(
                "_OAUTH_CRED_PATH not yet implemented "
                "(test_module_exposes_oauth_cred_path_seam covers the miss)"
            )

        parent_name = path_val.parent.name
        assert parent_name == ".claude", (
            "Default _OAUTH_CRED_PATH must be inside ~/.claude/; "
            f"parent dir name is {parent_name!r} — expected '.claude'"
        )
        assert path_val.name == ".credentials.json", (
            "Default credential file must be named .credentials.json; "
            f"got {path_val.name!r}"
        )
