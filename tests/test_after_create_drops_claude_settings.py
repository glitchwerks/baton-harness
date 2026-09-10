"""Tests for CodeReeve after-create settings generation.

Verifies that after_create writes .claude/settings.json with the
force-pr-not-merge PreToolUse hook registered through the canonical
``codereeve hook force-pr-not-merge`` command.

C4: CODEREEVE_VENV absence is FATAL — _write_claude_settings_if_configured
returns non-zero and must log via err() (not log()), and must NOT create
.claude/settings.json.

Coverage:
- Helper shape: claude_settings_json_for_worktree returns the expected
  dict with a single PreToolUse entry whose command points at the venv
  console-script (Windows Scripts/ or POSIX bin/).
- Happy path: _write_claude_settings writes $cwd/.claude/settings.json
  with the expected JSON shape and returns 0.
- CODEREEVE_VENV absent is fatal: _write_claude_settings_if_configured returns
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
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helper shape tests (claude_settings_json_for_worktree)
# ---------------------------------------------------------------------------


class TestClaudeSettingsJsonShape:
    """Tests for the canonical JSON-shape helper in _cli.py."""

    def test_top_level_hooks_key_present(self, tmp_path: Path) -> None:
        """Returned dict contains a top-level 'hooks' key."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)

        assert "hooks" in settings

    def test_pre_tool_use_key_present(self, tmp_path: Path) -> None:
        """hooks['PreToolUse'] is present and is a list."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)
        pre = settings["hooks"]["PreToolUse"]

        assert isinstance(pre, list)

    def test_pre_tool_use_has_exactly_one_entry(self, tmp_path: Path) -> None:
        """PreToolUse list contains exactly one hook registration."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)
        pre = settings["hooks"]["PreToolUse"]

        assert len(pre) == 1

    def test_matcher_is_bash(self, tmp_path: Path) -> None:
        """The single PreToolUse entry matches the 'Bash' tool."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)
        entry = settings["hooks"]["PreToolUse"][0]

        assert entry["matcher"] == "Bash"

    def test_hooks_sublist_has_type_command(self, tmp_path: Path) -> None:
        """The nested hooks list has an entry with type='command'."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)
        nested = settings["hooks"]["PreToolUse"][0]["hooks"]

        assert isinstance(nested, list)
        assert len(nested) == 1
        assert nested[0]["type"] == "command"

    def test_windows_command_uses_canonical_subcommand(
        self, tmp_path: Path
    ) -> None:
        """Windows settings invoke the canonical executable and hook route."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv with spaces & hooks"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve.exe").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        executable = str(venv / "Scripts" / "codereeve.exe").replace("\\", "/")
        assert cmd == f"'{executable}' hook force-pr-not-merge"

    def test_command_references_venv_scripts_or_bin(
        self, tmp_path: Path
    ) -> None:
        """The command path passes through venv's Scripts/ or bin/."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        normalized = cmd.replace("\\", "/")
        assert ("Scripts" in cmd) or ("/bin/" in normalized)

    def test_command_contains_venv_root_path(self, tmp_path: Path) -> None:
        """The command is absolute and contains the venv_root path."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        # The venv root name must appear in the path.
        assert "venv" in cmd

    def test_posix_bin_layout_is_preferred_when_exists(
        self, tmp_path: Path
    ) -> None:
        """When bin/ layout exists it is preferred (POSIX consistency)."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv with spaces & hooks"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "codereeve").touch()

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        assert cmd == (
            f"'{str(venv / 'bin' / 'codereeve')}' hook force-pr-not-merge"
        )

    @pytest.mark.skipif(os.name != "nt", reason="Windows launcher test")
    def test_windows_generated_command_preserves_hook_contract(
        self, tmp_path: Path
    ) -> None:
        """The generated command runs the installed canonical hook launcher."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv with spaces"
        (venv / "Scripts").mkdir(parents=True)
        source = Path(sys.executable).parent / "codereeve.exe"
        shutil.copy2(source, venv / "Scripts" / "codereeve.exe")

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        bash = Path("C:/Program Files/Git/usr/bin/bash.exe")
        if not bash.exists():
            pytest.skip("Git Bash is unavailable")
        result = subprocess.run(
            [str(bash), "-lc", cmd],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr merge 42"},
                }
            ),
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )

        assert result.returncode == 2
        assert result.stderr.startswith("BH_WORKER_TRIED_MERGE:")

    @pytest.mark.skipif(os.name == "nt", reason="native POSIX launcher test")
    def test_posix_generated_command_preserves_hook_contract(
        self, tmp_path: Path
    ) -> None:
        """The generated command runs the installed canonical POSIX hook."""
        from codereeve._cli import claude_settings_json_for_worktree

        venv = tmp_path / "venv with spaces & hooks"
        (venv / "bin").mkdir(parents=True)
        source = Path(sys.executable).parent / "codereeve"
        shutil.copy2(source, venv / "bin" / "codereeve")

        settings = claude_settings_json_for_worktree(venv)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        result = subprocess.run(
            ["sh", "-c", cmd],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr merge 42"},
                }
            ),
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )

        assert result.returncode == 2
        assert result.stderr.startswith("BH_WORKER_TRIED_MERGE:")


# ---------------------------------------------------------------------------
# _write_claude_settings happy path
# ---------------------------------------------------------------------------


class TestWriteClaudeSettingsHappyPath:
    """Tests for _write_claude_settings with a valid venv_root."""

    def test_returns_zero_on_success(self, tmp_path: Path) -> None:
        """_write_claude_settings returns 0 when the write succeeds."""
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        rc = _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert rc == 0

    def test_creates_dot_claude_directory(self, tmp_path: Path) -> None:
        """_write_claude_settings creates the .claude/ directory."""
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert (worktree / ".claude").is_dir()

    def test_creates_settings_json_file(self, tmp_path: Path) -> None:
        """_write_claude_settings writes $cwd/.claude/settings.json."""
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert (worktree / ".claude" / "settings.json").exists()

    def test_settings_json_is_valid_json(self, tmp_path: Path) -> None:
        """The written settings.json can be parsed as JSON."""
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

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
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

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
        from codereeve._cli import claude_settings_json_for_worktree
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        written = json.loads(
            (worktree / ".claude" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        expected = claude_settings_json_for_worktree(venv)

        assert written == expected


# ---------------------------------------------------------------------------
# CODEREEVE_VENV absence is FATAL (C4)
# ---------------------------------------------------------------------------


class TestCodereeveVenvAbsentIsFatal:
    """C4: missing CODEREEVE_VENV causes a loud, non-zero failure."""

    def test_returns_nonzero_when_codereeve_venv_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing CODEREEVE_VENV causes non-zero return (C4).

        A worker without the force-pr-not-merge hook silently loses
        defense-in-depth — the operator must notice at worktree creation
        time, not at the first merge attempt.
        """
        from codereeve.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)
        monkeypatch.delenv("CODEREEVE_VENV", raising=False)

        rc = _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert rc != 0, "CODEREEVE_VENV absent must return non-zero (C4)"

    def test_no_settings_json_written_when_codereeve_venv_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No settings are written when CODEREEVE_VENV is absent."""
        from codereeve.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)
        monkeypatch.delenv("CODEREEVE_VENV", raising=False)

        _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert not (worktree / ".claude").exists()

    def test_error_written_to_stderr_when_codereeve_venv_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CODEREEVE_VENV absence logs through ``err`` to stderr.

        err() writes to stderr; log() writes to stdout.  The C4 failure must
        be loud so the operator can diagnose the failed hook.
        Using err() also causes non-zero-exit shell pipelines to propagate
        the failure correctly.
        """
        from codereeve.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)
        monkeypatch.delenv("CODEREEVE_VENV", raising=False)

        _write_claude_settings_if_configured(issue=42, cwd=worktree)

        captured = capsys.readouterr()
        assert captured.err != "", (
            "CODEREEVE_VENV absent must emit an error on stderr (C4)"
        )

    def test_no_stdout_log_when_codereeve_venv_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CODEREEVE_VENV absence must not log success to stdout.

        If log() were called instead of err(), a monitoring pipeline that
        treats any stdout output as success would miss the failure.
        """
        from codereeve.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)
        monkeypatch.delenv("CODEREEVE_VENV", raising=False)

        _write_claude_settings_if_configured(issue=42, cwd=worktree)

        captured = capsys.readouterr()
        assert captured.out == "", (
            "CODEREEVE_VENV absent must not write to stdout"
        )

    def test_codereeve_venv_set_to_empty_string_is_fatal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty CODEREEVE_VENV is treated as absent and fatal."""
        from codereeve.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.delenv("BH_VENV", raising=False)
        monkeypatch.setenv("CODEREEVE_VENV", "")

        rc = _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert rc != 0, "Empty CODEREEVE_VENV must be treated as absent"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestWriteClaudeSettingsIdempotency:
    """Tests confirming re-running the write is safe."""

    def test_second_call_returns_zero(self, tmp_path: Path) -> None:
        """Calling _write_claude_settings twice both return 0."""
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

        rc1 = _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)
        rc2 = _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)

        assert rc1 == 0
        assert rc2 == 0

    def test_second_call_produces_same_file_content(
        self, tmp_path: Path
    ) -> None:
        """A second write produces the same settings.json as the first."""
        from codereeve.after_create import _write_claude_settings

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()

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
        """The configured settings write is idempotent.

        Re-running with the same CODEREEVE_VENV succeeds without raising.
        """
        from codereeve.after_create import (
            _write_claude_settings_if_configured,
        )

        worktree = tmp_path / "wt"
        worktree.mkdir()
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "codereeve").touch()
        monkeypatch.delenv("BH_VENV", raising=False)
        monkeypatch.setenv("CODEREEVE_VENV", str(venv))

        rc1 = _write_claude_settings_if_configured(issue=42, cwd=worktree)
        rc2 = _write_claude_settings_if_configured(issue=42, cwd=worktree)

        assert rc1 == 0
        assert rc2 == 0


@pytest.mark.parametrize(
    "project", ["package.json", "requirements.txt", "pyproject.toml"]
)
def test_canonical_hook_rejects_alias_conflicts_before_effects(
    project: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Alias conflicts prevent dependency installation and settings writes."""
    from unittest.mock import Mock

    from codereeve import after_create, cli
    from codereeve.config_env import PRODUCT_ALIASES

    alias = next(s for s in PRODUCT_ALIASES if s.canonical == "CODEREEVE_VENV")
    monkeypatch.setenv(alias.canonical, "canonical-secret-sentinel")
    monkeypatch.setenv(alias.legacy, "legacy-secret-sentinel")
    monkeypatch.chdir(tmp_path)
    (tmp_path / project).touch()
    monkeypatch.setattr(after_create, "resolve_issue_number", lambda: 395)
    effects = Mock(return_value=0)
    for name in (
        "_install_npm",
        "_install_requirements",
        "_install_pyproject",
        "_write_claude_settings",
    ):
        monkeypatch.setattr(after_create, name, effects)

    assert cli.main(["hook", "after-create"]) != 0
    effects.assert_not_called()
    output = capsys.readouterr().err
    assert alias.canonical in output and alias.legacy in output
    assert "canonical-secret-sentinel" not in output
    assert "legacy-secret-sentinel" not in output


@pytest.mark.parametrize("spelling", ["canonical", "legacy"])
def test_canonical_hook_uses_validated_snapshot(
    spelling: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either spelling works even if installation changes the environment."""
    from unittest.mock import Mock

    from codereeve import after_create, cli
    from codereeve.config_env import PRODUCT_ALIASES

    alias = next(s for s in PRODUCT_ALIASES if s.canonical == "CODEREEVE_VENV")
    monkeypatch.delenv(alias.canonical, raising=False)
    monkeypatch.delenv(alias.legacy, raising=False)
    venv = tmp_path / "original-venv"
    monkeypatch.setenv(getattr(alias, spelling), str(venv))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "requirements.txt").touch()
    monkeypatch.setattr(after_create, "resolve_issue_number", lambda: 395)

    def install(issue: int) -> int:
        """Simulate an installer changing environment after validation."""
        monkeypatch.setenv(getattr(alias, spelling), str(tmp_path / "changed"))
        return 0

    monkeypatch.setattr(after_create, "_install_requirements", install)
    writer = Mock(return_value=0)
    monkeypatch.setattr(after_create, "_write_claude_settings", writer)
    assert cli.main(["hook", "after-create"]) == 0
    writer.assert_called_once_with(issue=395, cwd=tmp_path, venv_root=venv)
