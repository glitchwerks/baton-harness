"""Tests for hook env-awareness: CHAIN_BASE_BRANCH support.

Covers:
- before_run reads CHAIN_BASE_BRANCH from env (default origin/main).
- after_run reads CHAIN_BASE_BRANCH from env (default origin/main).
- after_run resolves CHAIN_BASE_BRANCH to a concrete SHA at entry and uses
  that SHA as the git cherry base.
- Priority-3 no-longer-leaves-agent-ready: on COMMITTED_NO_PR / NO_COMMITS,
  after_run removes agent-ready and sets blocked instead of leaving
  agent-ready for Baton retry.

All subprocess calls are mocked; no real git or gh required.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import baton_harness.before_run as before_run_mod
from baton_harness.after_run import (
    RunOutcome,
    _classify,
    _reconcile_labels,
)
from baton_harness.before_run import main as before_run_main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess.

    Args:
        stdout: Simulated standard output.

    Returns:
        A CompletedProcess with returncode=0.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _fail(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess.

    Args:
        stdout: Simulated standard output.

    Returns:
        A CompletedProcess with returncode=1.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout=stdout, stderr=""
    )


# ---------------------------------------------------------------------------
# before_run: CHAIN_BASE_BRANCH env-awareness
# ---------------------------------------------------------------------------


class TestBeforeRunChainBaseBranch:
    """before_run reads CHAIN_BASE_BRANCH and resolves it to a SHA."""

    def test_defaults_to_origin_main_when_env_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CHAIN_BASE_BRANCH is not set, resolves and rebases origin/main.

        The hook resolves origin/main via rev-parse and rebases onto the
        returned SHA.  The rev-parse call must use origin/main as the ref.
        """
        worktree = tmp_path / "feat-10-thing"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        fake_sha = "aabbccdd" * 5
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "rev-parse" in cmd:
                return _ok(stdout=fake_sha + "\n")
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = before_run_main()

        assert result == 0
        # Rev-parse must use origin/main (the default base ref).
        rev_parse_calls = [c for c in calls if "rev-parse" in c]
        assert len(rev_parse_calls) == 1
        assert "origin/main" in rev_parse_calls[0]
        # Rebase must use the resolved SHA.
        rebase_calls = [
            c for c in calls if "rebase" in c and "--abort" not in c
        ]
        assert len(rebase_calls) == 1
        assert fake_sha in rebase_calls[0]

    def test_uses_chain_base_branch_when_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CHAIN_BASE_BRANCH is set, resolves then rebases onto SHA."""
        worktree = tmp_path / "feat-11-branch"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.setenv("CHAIN_BASE_BRANCH", "feature/my-work")

        calls: list[list[str]] = []
        fake_sha = "abc1234def5678abc1234def5678abc1234def56"

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "rev-parse" in cmd:
                return _ok(stdout=fake_sha + "\n")
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        result = before_run_main()

        assert result == 0
        # Must have resolved the ref to a SHA via git rev-parse
        rev_parse_calls = [c for c in calls if "rev-parse" in c]
        assert len(rev_parse_calls) >= 1
        # Must have rebased onto the resolved SHA, not the string ref
        rebase_calls = [
            c for c in calls if "rebase" in c and "--abort" not in c
        ]
        assert any(fake_sha in c for c in rebase_calls), (
            "before_run must rebase onto the resolved SHA, not the string ref"
        )

    def test_resolves_ref_before_rebase(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rev-parse is called before rebase (resolve-before-rebase)."""
        worktree = tmp_path / "feat-12-order"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.setenv("CHAIN_BASE_BRANCH", "feature/order-test")

        call_order: list[str] = []
        fake_sha = "deadbeef" * 5

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "rev-parse" in cmd:
                call_order.append("rev-parse")
                return _ok(stdout=fake_sha + "\n")
            if "rebase" in cmd and "--abort" not in cmd:
                call_order.append("rebase")
            return _ok()

        monkeypatch.setattr(before_run_mod, "_run", fake_run)

        before_run_main()

        assert "rev-parse" in call_order, "rev-parse must be called"
        assert "rebase" in call_order, "rebase must be called"
        assert call_order.index("rev-parse") < call_order.index("rebase"), (
            "rev-parse (resolve SHA) must occur before rebase"
        )


# ---------------------------------------------------------------------------
# after_run: CHAIN_BASE_BRANCH env-awareness
# ---------------------------------------------------------------------------


class TestAfterRunChainBaseBranch:
    """after_run reads CHAIN_BASE_BRANCH and uses it as the cherry base."""

    def test_defaults_to_origin_main_when_env_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CHAIN_BASE_BRANCH is unset, cherry uses origin/main."""
        worktree = tmp_path / "feat-20-thing"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "cherry" in cmd:
                return _ok(stdout="")  # no commits ahead
            return _ok()

        with patch("baton_harness.after_run._run", side_effect=fake_run):
            _classify()

        cherry_calls = [c for c in calls if "cherry" in c]
        assert len(cherry_calls) == 1
        assert "origin/main" in cherry_calls[0]

    def test_uses_resolved_sha_as_cherry_base(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CHAIN_BASE_BRANCH is set, cherry uses the resolved SHA."""
        monkeypatch.setenv("CHAIN_BASE_BRANCH", "feature/my-work")

        fake_sha = "deadbeef12345678deadbeef12345678deadbeef"
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "rev-parse" in cmd:
                return _ok(stdout=fake_sha + "\n")
            if "cherry" in cmd:
                return _ok(stdout="")
            return _ok()

        with patch("baton_harness.after_run._run", side_effect=fake_run):
            _classify()

        cherry_calls = [c for c in calls if "cherry" in c]
        assert len(cherry_calls) == 1
        assert fake_sha in cherry_calls[0], (
            "after_run must pass the resolved SHA (not the ref string) "
            "as the cherry base when CHAIN_BASE_BRANCH is set"
        )


# ---------------------------------------------------------------------------
# Priority-3: no-longer-leaves-agent-ready regression
# ---------------------------------------------------------------------------

_LABEL_AGENT_READY = json.dumps({"labels": [{"name": "agent-ready"}]})
_LABEL_AGENT_READY_ONLY = json.dumps({"labels": [{"name": "agent-ready"}]})


class TestPriority3NoLongerLeavesAgentReady:
    """On COMMITTED_NO_PR/NO_COMMITS: remove agent-ready and set blocked.

    The old Priority-3 path left agent-ready in place for Baton retry.
    The new behaviour (P0 requirement, issue #42) removes agent-ready and
    sets blocked so the daemon can decide the retry strategy.
    """

    def _completed(
        self, stdout: str = "", returncode: int = 0
    ) -> subprocess.CompletedProcess[str]:
        """Build a fake CompletedProcess.

        Args:
            stdout: Simulated standard output string.
            returncode: Simulated process return code.

        Returns:
            A CompletedProcess with the given stdout and returncode.
        """
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_committed_no_pr_removes_agent_ready(self) -> None:
        """COMMITTED_NO_PR: agent-ready is removed (not left in place)."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=_LABEL_AGENT_READY),  # gh issue view
                self._completed(),  # remove agent-ready
                self._completed(),  # add blocked
            ]
            _reconcile_labels(7, RunOutcome.COMMITTED_NO_PR)

        all_args = [c[0][0] for c in mock_run.call_args_list]
        remove_call = next(
            (
                a
                for a in all_args
                if "--remove-label" in a and "agent-ready" in a
            ),
            None,
        )
        assert remove_call is not None, (
            "agent-ready must be removed on COMMITTED_NO_PR "
            "(no longer left for Baton retry)"
        )

    def test_committed_no_pr_sets_blocked(self) -> None:
        """COMMITTED_NO_PR: blocked label is added."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=_LABEL_AGENT_READY),
                self._completed(),  # remove agent-ready
                self._completed(),  # add blocked
            ]
            _reconcile_labels(7, RunOutcome.COMMITTED_NO_PR)

        all_args = [c[0][0] for c in mock_run.call_args_list]
        add_blocked = next(
            (a for a in all_args if "--add-label" in a and "blocked" in a),
            None,
        )
        assert add_blocked is not None, (
            "blocked label must be added on COMMITTED_NO_PR "
            "(Priority-3 carry-forward deleted)"
        )

    def test_no_commits_removes_agent_ready(self) -> None:
        """NO_COMMITS: agent-ready is removed (not left in place)."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=_LABEL_AGENT_READY),
                self._completed(),  # remove agent-ready
                self._completed(),  # add blocked
            ]
            _reconcile_labels(7, RunOutcome.NO_COMMITS)

        all_args = [c[0][0] for c in mock_run.call_args_list]
        remove_call = next(
            (
                a
                for a in all_args
                if "--remove-label" in a and "agent-ready" in a
            ),
            None,
        )
        assert remove_call is not None, (
            "agent-ready must be removed on NO_COMMITS"
        )

    def test_no_commits_sets_blocked(self) -> None:
        """NO_COMMITS: blocked label is added."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=_LABEL_AGENT_READY),
                self._completed(),  # remove agent-ready
                self._completed(),  # add blocked
            ]
            _reconcile_labels(7, RunOutcome.NO_COMMITS)

        all_args = [c[0][0] for c in mock_run.call_args_list]
        add_blocked = next(
            (a for a in all_args if "--add-label" in a and "blocked" in a),
            None,
        )
        assert add_blocked is not None, (
            "blocked label must be added on NO_COMMITS"
        )

    def test_uncommitted_changes_removes_agent_ready(self) -> None:
        """UNCOMMITTED_CHANGES: agent-ready is removed."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=_LABEL_AGENT_READY),
                self._completed(),  # remove agent-ready
                self._completed(),  # add blocked
            ]
            _reconcile_labels(7, RunOutcome.UNCOMMITTED_CHANGES)

        all_args = [c[0][0] for c in mock_run.call_args_list]
        remove_call = next(
            (
                a
                for a in all_args
                if "--remove-label" in a and "agent-ready" in a
            ),
            None,
        )
        assert remove_call is not None

    def test_uncommitted_changes_sets_blocked(self) -> None:
        """UNCOMMITTED_CHANGES: blocked label is added."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=_LABEL_AGENT_READY),
                self._completed(),  # remove agent-ready
                self._completed(),  # add blocked
            ]
            _reconcile_labels(7, RunOutcome.UNCOMMITTED_CHANGES)

        all_args = [c[0][0] for c in mock_run.call_args_list]
        add_blocked = next(
            (a for a in all_args if "--add-label" in a and "blocked" in a),
            None,
        )
        assert add_blocked is not None

    def test_label_edit_failure_propagates_nonzero(self) -> None:
        """Label-edit failure on Priority-3 path returns non-zero."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=_LABEL_AGENT_READY),
                self._completed(returncode=1),  # remove agent-ready fails
            ]
            exit_code = _reconcile_labels(7, RunOutcome.NO_COMMITS)

        assert exit_code != 0

    def test_agent_ready_absent_still_sets_blocked(self) -> None:
        """Even when agent-ready is absent, blocked is still added."""
        no_labels = json.dumps({"labels": []})
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                self._completed(stdout=no_labels),  # no agent-ready
                self._completed(),  # add blocked
            ]
            exit_code = _reconcile_labels(7, RunOutcome.NO_COMMITS)

        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        add_blocked = next(
            (a for a in all_args if "--add-label" in a and "blocked" in a),
            None,
        )
        assert add_blocked is not None, (
            "blocked must be added even when agent-ready is not present"
        )
