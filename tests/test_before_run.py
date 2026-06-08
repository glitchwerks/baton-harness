"""Unit tests for baton_harness.before_run — branch sync onto chain base.

All subprocess calls are intercepted by monkeypatching the module-local
``_run`` helper so no real git operations are performed.

The hook now performs three steps: fetch, rev-parse (resolve base ref to a
SHA), and rebase onto the resolved SHA.  CHAIN_BASE_BRANCH controls the
base ref (default origin/main).

Coverage:
- success path: fetch + rev-parse + rebase all succeed → exit 0
- already-current path: rebase exits 0 with no-op output → exit 0
- conflict path: rebase fails → abort is called → exit non-zero
- abort itself fails after rebase failure → still exit non-zero
- fetch failure → rebase is skipped → exit non-zero
- rev-parse failure → rebase is skipped → exit non-zero
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


_FAKE_SHA = "abc1234" * 6  # 42-char fake SHA for rev-parse mocks


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess that signals success.

    Args:
        stdout: Simulated standard output (e.g. a SHA from rev-parse).

    Returns:
        A CompletedProcess with returncode=0.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _fail(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess that signals failure.

    Args:
        stdout: Simulated standard output.

    Returns:
        A CompletedProcess with returncode=1.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout=stdout, stderr=""
    )


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


class TestBeforeRunSuccess:
    """Tests for the happy-path branch sync."""

    def test_fetch_revparse_and_rebase_called_in_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fetch → rev-parse → rebase in order; rebase uses resolved SHA.

        The hook now resolves the base ref to a concrete SHA before rebasing
        (CHAIN_BASE_BRANCH env-awareness, chain spec §3.7).
        """
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "rev-parse" in cmd:
                return _ok(stdout=_FAKE_SHA + "\n")
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result == 0
        assert calls[0] == ["git", "fetch", "origin", "main"]
        assert calls[1] == ["git", "rev-parse", "origin/main"]
        # Rebase uses the resolved SHA, not the string ref
        assert calls[2] == ["git", "rebase", _FAKE_SHA]

    def test_already_current_returns_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When rebase exits 0 (already up to date), main returns 0."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "rev-parse" in cmd:
                return _ok(stdout=_FAKE_SHA + "\n")
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

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
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "rev-parse" in cmd:
                return _ok(stdout=_FAKE_SHA + "\n")
            # fetch succeeds; rebase fails; abort succeeds
            if "rebase" in cmd and "--abort" not in cmd:
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
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "rev-parse" in cmd:
                return _ok(stdout=_FAKE_SHA + "\n")
            if "rebase" in cmd and "--abort" not in cmd:
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
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "rev-parse" in cmd:
                return _ok(stdout=_FAKE_SHA + "\n")
            # Both rebase (non-abort) and abort fail.
            if "rebase" in cmd:
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
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _fail()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result != 0
        # Rebase should not run if fetch failed.
        rebase_calls = [c for c in calls if "rebase" in c]
        assert not rebase_calls, "Rebase must not run if fetch failed"

    def test_rev_parse_failure_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing git rev-parse returns non-zero without rebasing."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "rev-parse" in cmd:
                return _fail()
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = main()

        assert result != 0
        rebase_calls = [c for c in calls if "rebase" in c]
        assert not rebase_calls, "Rebase must not run if rev-parse failed"


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
