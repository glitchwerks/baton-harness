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
- capture regression: rev-parse subprocess must use capture_output so
  .stdout is not None (real-repo integration test)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import baton_harness.before_run as before_run_mod
from baton_harness.before_run import _run_capture, main

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

        After the capture fix (issue #63), rev-parse is dispatched via
        ``_run_capture`` (not ``_run``), so both helpers must be patched.
        ``_run`` handles streaming calls (fetch, rebase, abort);
        ``_run_capture`` handles the single call needing ``.stdout``
        (rev-parse).
        """
        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        stream_calls: list[list[str]] = []
        capture_calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            stream_calls.append(cmd)
            return _ok()

        def fake_run_capture(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            capture_calls.append(cmd)
            return _ok(stdout=_FAKE_SHA + "\n")

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        monkeypatch.setattr(before_run_mod, "_run_capture", fake_run_capture)

        result = main()

        assert result == 0
        assert stream_calls[0] == ["git", "fetch", "origin", "main"]
        assert capture_calls[0] == ["git", "rev-parse", "origin/main"]
        # Rebase uses the resolved SHA, not the string ref
        assert stream_calls[1] == ["git", "rebase", _FAKE_SHA]

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
            return _ok()

        def fake_run_capture(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return _ok(stdout=_FAKE_SHA + "\n")

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        monkeypatch.setattr(before_run_mod, "_run_capture", fake_run_capture)

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

        stream_calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            stream_calls.append(cmd)
            # fetch succeeds; rebase fails; abort succeeds
            if "rebase" in cmd and "--abort" not in cmd:
                return _fail()
            return _ok()

        def fake_run_capture(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return _ok(stdout=_FAKE_SHA + "\n")

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        monkeypatch.setattr(before_run_mod, "_run_capture", fake_run_capture)

        result = main()

        assert result != 0
        assert ["git", "rebase", "--abort"] in stream_calls

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
            if "rebase" in cmd and "--abort" not in cmd:
                return _fail()
            return _ok()

        def fake_run_capture(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return _ok(stdout=_FAKE_SHA + "\n")

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        monkeypatch.setattr(before_run_mod, "_run_capture", fake_run_capture)

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
            # Both rebase (non-abort) and abort fail.
            if "rebase" in cmd:
                return _fail()
            return _ok()

        def fake_run_capture(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return _ok(stdout=_FAKE_SHA + "\n")

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        monkeypatch.setattr(before_run_mod, "_run_capture", fake_run_capture)

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

        stream_calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            stream_calls.append(cmd)
            return _fail()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        # _run_capture is NOT patched: fetch failure means rev-parse is
        # never reached, so _run_capture should never be called.

        result = main()

        assert result != 0
        # Rebase should not run if fetch failed.
        rebase_calls = [c for c in stream_calls if "rebase" in c]
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

        stream_calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            stream_calls.append(cmd)
            return _ok()

        def fake_run_capture(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            # Rev-parse fails.
            return _fail()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        monkeypatch.setattr(before_run_mod, "_run_capture", fake_run_capture)

        result = main()

        assert result != 0
        rebase_calls = [c for c in stream_calls if "rebase" in c]
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


# ---------------------------------------------------------------------------
# Capture regression — real subprocess, no monkeypatching of _run
# ---------------------------------------------------------------------------


class TestRevParseCapture:
    """Integration test: rev-parse path must capture stdout from subprocess.

    This test drives ``main()`` against a real throwaway git repository so
    that ``_run`` is called with the actual subprocess machinery.  If the
    rev-parse call does not capture output (i.e. ``capture_output`` is
    missing), ``result.stdout`` is ``None`` and ``None.strip()`` raises
    ``AttributeError`` — the test errors rather than passing, which is
    exactly the regression signal we want.

    Design rationale: monkeypatching ``_run`` to return a
    ``CompletedProcess`` with ``.stdout`` set hides the bug (the previous
    tests do this correctly for unit isolation, but cannot catch a
    regression to the non-capturing form).  Only driving the *real*
    subprocess call proves that ``.stdout`` is populated at the subprocess
    boundary.  A separate ``_run_capture`` helper is added to
    ``before_run.py`` for the rev-parse call; this test also imports that
    helper to confirm it exists and returns captured output.
    """

    def test_run_capture_returns_stdout(self, tmp_path: Path) -> None:
        """``_run_capture`` populates ``.stdout`` from a real subprocess.

        Regression test for the non-capturing ``_run()`` bug: if
        ``_run_capture`` omits ``capture_output`` / ``text`` the result
        ``.stdout`` is ``None`` and the assertion below fails.
        """
        git_repo = tmp_path / "repo"
        git_repo.mkdir()
        subprocess.run(
            ["git", "init", str(git_repo)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "config", "user.email", "t@t.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "config", "user.name", "T"],
            check=True,
            capture_output=True,
        )
        (git_repo / "f.txt").write_text("hello", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(git_repo), "add", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        # Invoke _run_capture with a real git rev-parse command.
        result = _run_capture(
            ["git", "-C", str(git_repo), "rev-parse", "HEAD"]
        )

        assert result.stdout is not None, (
            "_run_capture must capture stdout; got None — "
            "capture_output or text=True is missing"
        )
        sha = result.stdout.strip()
        assert len(sha) == 40, f"Expected 40-char SHA, got {sha!r}"
        assert all(c in "0123456789abcdef" for c in sha), (
            f"SHA is not hex: {sha!r}"
        )

    def test_main_rev_parse_uses_captured_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``main()`` succeeds and resolves a real 40-hex SHA via rev-parse.

        Sets ``CHAIN_BASE_BRANCH=HEAD`` in a real git repo so that Step 1
        (fetch) is skipped and Step 2 (rev-parse HEAD) runs using the
        actual ``_run_capture`` subprocess call.  If the implementation
        reverts to the non-capturing ``_run``, ``None.strip()`` raises
        ``AttributeError`` and this test errors — not passes.
        """
        # Build a minimal real git repo.
        git_repo = tmp_path / "feat-63-sync"
        git_repo.mkdir()
        subprocess.run(
            ["git", "init", str(git_repo)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "config", "user.email", "t@t.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "config", "user.name", "T"],
            check=True,
            capture_output=True,
        )
        (git_repo / "f.txt").write_text("hello", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(git_repo), "add", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        # Point cwd at the worktree-named dir; set CHAIN_BASE_BRANCH=HEAD
        # so fetch is skipped and rev-parse HEAD runs in the real repo.
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv("CHAIN_BASE_BRANCH", "HEAD")

        # Patch only the *streaming* _run (fetch / rebase / abort) so git
        # rebase doesn't actually run; leave _run_capture untouched so the
        # rev-parse subprocess call is real and we test the capture path.
        def fake_stream_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=None, stderr=None
            )

        monkeypatch.setattr(before_run_mod, "_run", fake_stream_run)

        rc = main()

        assert rc == 0, f"main() returned {rc!r}; expected 0"
