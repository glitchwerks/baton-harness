"""Unit tests for baton_harness._cli — shared CLI helpers.

Tests cover:
- ``resolve_issue_number``: derives the GitHub issue number from a worktree
  directory path whose basename matches the ``<prefix>-<issue>-<slug>`` or
  ``<prefix><issue>-<slug>`` conventions used by the harness worktree naming
  scheme (e.g. ``feat-10-python-scaffold``, ``fix-42-auth-bug``).
- ``log`` / ``err``: write correctly-prefixed lines to stdout/stderr.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baton_harness._cli import err, log, resolve_issue_number

# ---------------------------------------------------------------------------
# resolve_issue_number
# ---------------------------------------------------------------------------


class TestResolveIssueNumber:
    """Tests for resolve_issue_number()."""

    def test_feat_branch_convention(self) -> None:
        """feat-10-python-scaffold → 10."""
        path = Path("/some/harness/.worktrees/feat-10-python-scaffold")
        assert resolve_issue_number(path) == 10

    def test_fix_branch_convention(self) -> None:
        """fix-42-auth-bug → 42."""
        path = Path("/repo/.worktrees/fix-42-auth-bug")
        assert resolve_issue_number(path) == 42

    def test_chore_branch_convention(self) -> None:
        """chore-7-cleanup → 7."""
        path = Path("/repo/.worktrees/chore-7-cleanup")
        assert resolve_issue_number(path) == 7

    def test_multi_digit_issue(self) -> None:
        """feat-123-big-feature → 123."""
        path = Path("/repo/.worktrees/feat-123-big-feature")
        assert resolve_issue_number(path) == 123

    def test_bare_numeric_suffix_not_matched(self) -> None:
        """A directory whose name is purely numeric returns None."""
        path = Path("/repo/.worktrees/12345")
        assert resolve_issue_number(path) is None

    def test_no_issue_number_in_name(self) -> None:
        """A directory name with no embedded number returns None."""
        path = Path("/repo/.worktrees/feat-python-scaffold")
        assert resolve_issue_number(path) is None

    def test_cwd_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_issue_number(None) reads the current working directory."""
        worktree = tmp_path / "feat-99-test-thing"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        assert resolve_issue_number(None) == 99


# ---------------------------------------------------------------------------
# log / err
# ---------------------------------------------------------------------------


class TestLog:
    """Tests for log() — writes to stdout with a hook-tagged prefix."""

    def test_log_writes_to_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """log() writes the message to stdout."""
        log("after-run", 7, "hello world")
        captured = capsys.readouterr()
        assert "hello world" in captured.out
        assert captured.err == ""

    def test_log_includes_hook_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """log() prefixes the output with the hook name."""
        log("before-run", 42, "syncing branch")
        captured = capsys.readouterr()
        assert "before-run" in captured.out

    def test_log_includes_issue_number(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """log() includes the issue number in the prefix."""
        log("after-create", 10, "installing deps")
        captured = capsys.readouterr()
        assert "#10" in captured.out

    def test_log_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        """log() produces the canonical [<hook> #<issue>] <message> format."""
        log("after-run", 3, "done")
        captured = capsys.readouterr()
        assert captured.out.strip() == "[after-run #3] done"


class TestErr:
    """Tests for err() — writes to stderr with a hook-tagged prefix."""

    def test_err_writes_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """err() writes the message to stderr."""
        err("after-run", 7, "something failed")
        captured = capsys.readouterr()
        assert "something failed" in captured.err
        assert captured.out == ""

    def test_err_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        """err() produces the canonical [<hook> #<issue>] <message> format."""
        err("before-run", 5, "network error")
        captured = capsys.readouterr()
        assert captured.err.strip() == "[before-run #5] network error"
