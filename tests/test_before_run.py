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
- auth gate: token validation failure → non-zero, no git fetch called
- auth gate: token validation success → proceeds to git fetch
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import baton_harness.before_run as before_run_mod
from baton_harness._auth import TokenValidationError
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
# Module-wide auth fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op ``validate_github_token`` for all tests in this module.

    Patches the name as imported into ``before_run`` (the resolved
    reference used at call time), not in ``_auth`` itself.  Tests that
    exercise auth-gate failures override this via their own
    ``monkeypatch.setattr`` on the same target — last write wins within
    the same ``monkeypatch`` scope.
    """
    monkeypatch.setattr(before_run_mod, "validate_github_token", lambda: None)


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


# ---------------------------------------------------------------------------
# Auth gate integration
# ---------------------------------------------------------------------------


class TestBeforeRunAuthGate:
    """Auth gate is called first; failure short-circuits all git ops."""

    def test_auth_failure_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When token validation raises, main returns non-zero exit code."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(args=[], returncode=0)

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        # Override the autouse no-op to raise instead.

        def _raise_classic() -> None:
            raise TokenValidationError("classic PAT detected")

        monkeypatch.setattr(
            before_run_mod, "validate_github_token", _raise_classic
        )

        result = main()

        assert result != 0

    def test_auth_failure_does_not_call_git_fetch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When validation fails, ``git fetch`` is never called."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(args=[], returncode=0)

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        def _raise_missing() -> None:
            raise TokenValidationError("no token found")

        monkeypatch.setattr(
            before_run_mod, "validate_github_token", _raise_missing
        )

        main()

        assert ["git", "fetch", "origin", "main"] not in calls

    def test_auth_success_proceeds_to_git_fetch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When validation succeeds, the normal git fetch/rebase path runs."""
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(args=[], returncode=0)

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        # Autouse fixture already patches to no-op; explicit here for clarity.

        result = main()

        assert result == 0
        assert ["git", "fetch", "origin", "main"] in calls
