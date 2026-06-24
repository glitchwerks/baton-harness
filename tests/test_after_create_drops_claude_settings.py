"""Tests for bh-after-create .claude/settings.json drop (slice 3b task 5).

Verifies that after_create writes .claude/settings.json with the
force-pr-not-merge PreToolUse hook registered, pointing at
$BH_VENV/{Scripts,bin}/bh-force-pr-not-merge.

C4: BH_VENV absence is FATAL — _write_claude_settings_if_configured
returns non-zero and must log via err() (not log()), and must NOT create
.claude/settings.json.

Coverage:
- Helper shape: claude_settings_json_for_worktree returns the expected
  dict with a single PreToolUse entry whose command points at the venv
  console-script (Windows Scripts/ or POSIX bin/).
- Happy path: _write_claude_settings writes $cwd/.claude/settings.json
  with the expected JSON shape and returns 0.
- BH_VENV absent is fatal: _write_claude_settings_if_configured returns
  non-zero, writes nothing, and emits the error via err() not log().
- Idempotency: calling _write_claude_settings twice re-writes the file
  (no error, same content, rc 0 on both calls).

All imports of the not-yet-implemented symbols are deferred to test
bodies so pytest collection succeeds and tests fail with
ImportError/AttributeError (the expected red) rather than collection
errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helper shape tests (claude_settings_json_for_worktree)
# ---------------------------------------------------------------------------


class TestClaudeSettingsJsonShape:
    """Tests for the canonical JSON-shape helper in _cli.py."""

    def test_top_level_hooks_key_present(self, tmp_path: Path) -> None:
        """Returned dict contains a top-level 'hooks' key."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)

        assert "hooks" in settings

    def test_pre_tool_use_key_present(self, tmp_path: Path) -> None:
        """hooks['PreToolUse'] is present and is a list."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        pre = settings["hooks"]["PreToolUse"]

        assert isinstance(pre, list)

    def test_pre_tool_use_has_exactly_one_entry(self, tmp_path: Path) -> None:
        """PreToolUse list contains exactly one hook registration."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        pre = settings["hooks"]["PreToolUse"]

        assert len(pre) == 1

    def test_matcher_is_bash(self, tmp_path: Path) -> None:
        """The single PreToolUse entry matches the 'Bash' tool."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        entry = settings["hooks"]["PreToolUse"][0]

        assert entry["matcher"] == "Bash"

    def test_hooks_sublist_has_type_command(self, tmp_path: Path) -> None:
        """The nested hooks list has an entry with type='command'."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        nested = settings["hooks"]["PreToolUse"][0]["hooks"]

        assert isinstance(nested, list)
        assert len(nested) == 1
        assert nested[0]["type"] == "command"

    def test_command_ends_with_bh_force_pr_not_merge(
        self, tmp_path: Path
    ) -> None:
        """The command path ends with the bh-force-pr-not-merge script."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        # Accept both bare name (POSIX) and .exe suffix (Windows).
        assert cmd.endswith("bh-force-pr-not-merge") or cmd.endswith(
            "bh-force-pr-not-merge.exe"
        )

    def test_command_references_venv_scripts_or_bin(
        self, tmp_path: Path
    ) -> None:
        """The command path passes through venv's Scripts/ or bin/."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        normalized = cmd.replace("\\", "/")
        assert ("Scripts" in cmd) or ("/bin/" in normalized)

    def test_command_contains_venv_root_path(self, tmp_path: Path) -> None:
        """The command is absolute and contains the venv_root path."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        # The venv root name must appear in the path.
        assert "venv" in cmd

    def test_posix_bin_layout_is_preferred_when_exists(
        self, tmp_path: Path
    ) -> None:
        """When bin/ layout exists it is preferred (POSIX consistency)."""
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "bh-force-pr-not-merge").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        # POSIX bin/ must appear in the command.
        assert "/bin/" in cmd.replace("\\", "/")

    def test_windows_scripts_exe_is_accepted(self, tmp_path: Path) -> None:
        """Windows .exe variant accepted when Scripts/*.exe is the only form.

        The helper must not hard-fail if Scripts/bh-force-pr-not-merge.exe
        is the only script present.
        """
        from baton_harness._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge.exe").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        assert "bh-force-pr-not-merge" in cmd


# ---------------------------------------------------------------------------
# _write_claude_settings happy path
# ---------------------------------------------------------------------------


class TestWriteClaudeSettingsHappyPath:
    """Tests for _write_claude_settings with a valid venv_root."""

    def test_returns_zero_on_success(self, tmp_path: Path) -> None:
        """_write_claude_settings returns 0 when the write succeeds."""
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        rc = _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert rc == 0

    def test_creates_dot_claude_directory(self, tmp_path: Path) -> None:
        """_write_claude_settings creates the .claude/ directory."""
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert (worktree / ".claude").is_dir()

    def test_creates_settings_json_file(self, tmp_path: Path) -> None:
        """_write_claude_settings writes $cwd/.claude/settings.json."""
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert (worktree / ".claude" / "settings.json").exists()

    def test_settings_json_is_valid_json(self, tmp_path: Path) -> None:
        """The written settings.json can be parsed as JSON."""
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        raw = (worktree / ".claude" / "settings.json").read_text(
            encoding="utf-8"
        )
        payload = json.loads(raw)  # Must not raise.
        assert isinstance(payload, dict)

    def test_settings_json_has_pre_tool_use_bash_matcher(
        self, tmp_path: Path
    ) -> None:
        """Written JSON contains a Bash PreToolUse registration."""
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        payload = json.loads(
            (worktree / ".claude" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        pre = payload["hooks"]["PreToolUse"]
        assert pre[0]["matcher"] == "Bash"

    def test_settings_json_matches_helper_output(self, tmp_path: Path) -> None:
        """Written JSON is identical to claude_settings_json_for_worktree.

        This is the key source-of-truth assertion: after_create must use
        the helper, not duplicate the JSON structure.
        """
        from baton_harness._cli import claude_settings_json_for_worktree
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        written = json.loads(
            (worktree / ".claude" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        expected = claude_settings_json_for_worktree(venv)

        assert written == expected


# ---------------------------------------------------------------------------
# BH_VENV absence is FATAL (C4)
# ---------------------------------------------------------------------------


class TestBhVenvAbsentIsFatal:
    """C4: missing BH_VENV must cause a loud, non-zero failure."""

    def test_returns_nonzero_when_bh_venv_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing BH_VENV causes non-zero return (C4).

        A worker without the force-pr-not-merge hook silently loses
        defense-in-depth — the operator must notice at worktree creation
        time, not at the first merge attempt.
        """
        from baton_harness.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)

        rc = _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert rc != 0, "BH_VENV absent must return non-zero (C4)"

    def test_no_settings_json_written_when_bh_venv_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No .claude/settings.json is written when BH_VENV is absent."""
        from baton_harness.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)

        _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert not (worktree / ".claude").exists()

    def test_error_written_to_stderr_not_stdout_when_bh_venv_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """BH_VENV absence logs via err() — message appears on stderr.

        err() writes to stderr; log() writes to stdout.  The C4 failure must
        be loud: an operator tailing stdout (Baton's default) must see it.
        Using err() also causes non-zero-exit shell pipelines to propagate
        the failure correctly.
        """
        from baton_harness.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)

        _write_claude_settings_if_configured(issue=42, cwd=worktree)

        captured = capsys.readouterr()
        assert captured.err != "", (
            "BH_VENV absent must emit an error on stderr (C4)"
        )

    def test_no_stdout_log_when_bh_venv_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """BH_VENV absence must NOT log a success line to stdout.

        If log() were called instead of err(), a monitoring pipeline that
        treats any stdout output as success would miss the failure.
        """
        from baton_harness.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)

        _write_claude_settings_if_configured(issue=42, cwd=worktree)

        captured = capsys.readouterr()
        assert captured.out == "", (
            "BH_VENV absent must NOT write to stdout (must use err())"
        )

    def test_bh_venv_set_to_empty_string_is_fatal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty-string BH_VENV is treated as absent (also fatal)."""
        from baton_harness.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.setenv("BH_VENV", "")

        rc = _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert rc != 0, "Empty BH_VENV must be treated as absent (C4)"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestWriteClaudeSettingsIdempotency:
    """Tests confirming re-running the write is safe."""

    def test_second_call_returns_zero(self, tmp_path: Path) -> None:
        """Calling _write_claude_settings twice both return 0."""
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        rc1 = _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)
        rc2 = _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert rc1 == 0
        assert rc2 == 0

    def test_second_call_produces_same_file_content(
        self, tmp_path: Path
    ) -> None:
        """A second write produces the same settings.json as the first."""
        from baton_harness.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()

        settings_path = worktree / ".claude" / "settings.json"

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)
        content_first = settings_path.read_text(encoding="utf-8")

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)
        content_second = settings_path.read_text(encoding="utf-8")

        assert json.loads(content_first) == json.loads(content_second)

    def test_if_configured_second_call_returns_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_write_claude_settings_if_configured is idempotent when BH_VENV set.

        Re-running with the same BH_VENV must succeed (rc 0) and not raise.
        """
        from baton_harness.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "bh-force-pr-not-merge").touch()
        monkeypatch.setenv("BH_VENV", str(venv))

        rc1 = _write_claude_settings_if_configured(issue=42, cwd=worktree)
        rc2 = _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert rc1 == 0
        assert rc2 == 0
