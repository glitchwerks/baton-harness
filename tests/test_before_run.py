"""Unit tests for baton_harness.before_run — branch sync onto main.

All subprocess calls are intercepted by monkeypatching the module-local
``_run`` helper so no real git operations are performed.

Coverage:
- success path: fetch + rebase both succeed → exit 0
- already-current path: rebase exits 0 with no-op output → exit 0
- conflict path: rebase fails → abort is called → exit non-zero
- abort itself fails after rebase failure → still exit non-zero
- fetch failure → rebase is skipped → exit non-zero
- unresolvable issue number → exit 1
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import baton_harness.before_run as before_run_mod
from baton_harness.before_run import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok() -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess that signals success."""
    return subprocess.CompletedProcess(args=[], returncode=0)


def _fail() -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess that signals failure."""
    return subprocess.CompletedProcess(args=[], returncode=1)


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


class TestBeforeRunSuccess:
    """Tests for the happy-path branch sync."""

    def test_fetch_and_rebase_called_in_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``git fetch origin main`` then ``git rebase origin/main`` in order.

        Both commands must appear in sequence in the subprocess call list.
        """
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result == 0
        assert calls[0] == ["git", "fetch", "origin", "main"]
        assert calls[1] == ["git", "rebase", "origin/main"]

    def test_already_current_returns_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When rebase exits 0 (already up to date), main returns 0."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        monkeypatch.setattr(before_run_mod, "_run", lambda _cmd: _ok())

        result = main()

        assert result == 0


# ---------------------------------------------------------------------------
# Conflict / failure paths
# ---------------------------------------------------------------------------


class TestBeforeRunConflict:
    """Tests for the conflict/failure branch sync path."""

    def test_rebase_conflict_calls_abort(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When rebase fails, ``git rebase --abort`` is called."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            # fetch succeeds; rebase fails; abort succeeds
            if cmd == ["git", "rebase", "origin/main"]:
                return _fail()
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result != 0
        assert ["git", "rebase", "--abort"] in calls

    def test_rebase_conflict_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rebase conflict causes main to return a non-zero exit code."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if cmd == ["git", "rebase", "origin/main"]:
                return _fail()
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result != 0

    def test_abort_failure_still_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even if abort itself fails, main still returns non-zero."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            # Both rebase and abort fail.
            if cmd in (
                ["git", "rebase", "origin/main"],
                ["git", "rebase", "--abort"],
            ):
                return _fail()
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result != 0

    def test_fetch_failure_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing git fetch returns non-zero without rebasing."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _fail()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result != 0
        # Rebase should not run if fetch failed.
        assert ["git", "rebase", "origin/main"] not in calls


# ---------------------------------------------------------------------------
# Bad worktree name
# ---------------------------------------------------------------------------


class TestBeforeRunBadWorktreeName:
    """Tests for the error path when issue number cannot be resolved."""

    def test_unresolvable_issue_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worktree name without a number returns exit 1."""
        bad_dir = tmp_path / "nodashes"
        bad_dir.mkdir()
        monkeypatch.chdir(bad_dir)

        result = main()

        assert result == 1
