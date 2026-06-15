"""Unit tests for baton_harness.after_run.

Tests cover:

- F5 classification (``RunOutcome`` enum): all four outcome states.
- Label reconciliation: pr-opened → agent-done, blocked → remove
  agent-ready, retryable → leave agent-ready unchanged.
- Label-edit failures propagate as non-zero exit codes (H1 root cause).
- ``gh --json`` output is parsed via ``json.loads``, never grepped.

All git/gh subprocess calls are mocked via ``unittest.mock.patch`` on
the module-local ``_run`` helper, so no real git or gh is required.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from baton_harness import after_run
from baton_harness.after_run import RunOutcome, _classify, _reconcile_labels

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    stdout: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Build a fake CompletedProcess for use in mock return values.

    Args:
        stdout: Simulated standard output string.
        returncode: Simulated process return code.

    Returns:
        A ``subprocess.CompletedProcess`` with the given stdout and returncode.
    """
    result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )
    return result


# ---------------------------------------------------------------------------
# RunOutcome enum
# ---------------------------------------------------------------------------


class TestRunOutcome:
    """Tests for the RunOutcome enum values."""

    def test_has_uncommitted_changes_member(self) -> None:
        """RunOutcome has an UNCOMMITTED_CHANGES member."""
        assert RunOutcome.UNCOMMITTED_CHANGES

    def test_has_no_commits_member(self) -> None:
        """RunOutcome has a NO_COMMITS member."""
        assert RunOutcome.NO_COMMITS

    def test_has_committed_no_pr_member(self) -> None:
        """RunOutcome has a COMMITTED_NO_PR member."""
        assert RunOutcome.COMMITTED_NO_PR

    def test_has_pr_opened_member(self) -> None:
        """RunOutcome has a PR_OPENED member."""
        assert RunOutcome.PR_OPENED

    def test_members_are_distinct(self) -> None:
        """All four RunOutcome values are distinct."""
        members = list(RunOutcome)
        assert len(members) == len(set(members))


# ---------------------------------------------------------------------------
# F5 classification — _classify()
# ---------------------------------------------------------------------------


class TestClassifyUncommittedChanges:
    """_classify returns UNCOMMITTED_CHANGES when git status is non-empty."""

    def test_uncommitted_changes_detected(self) -> None:
        """Non-empty porcelain output → UNCOMMITTED_CHANGES."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(stdout=" M src/foo.py\n")
            result = _classify()
        assert result == RunOutcome.UNCOMMITTED_CHANGES

    def test_uncommitted_changes_calls_git_status(self) -> None:
        """Confirm _classify checks git status --porcelain first."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(stdout=" M file.py\n")
            _classify()
        first_call_args = mock_run.call_args_list[0][0][0]
        assert first_call_args[:3] == ["git", "status", "--porcelain"]


class TestClassifyNoCommits:
    """_classify returns NO_COMMITS when cherry shows no ahead commits."""

    def test_no_commits_when_cherry_empty(self) -> None:
        """Empty git cherry output → NO_COMMITS."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout=""),  # git cherry — no ahead commits
            ]
            result = _classify()
        assert result == RunOutcome.NO_COMMITS

    def test_no_commits_when_cherry_has_no_plus_lines(self) -> None:
        """Cherry output with only minus lines → NO_COMMITS."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout="- abc123\n"),  # cherry minus = not ahead
            ]
            result = _classify()
        assert result == RunOutcome.NO_COMMITS


class TestClassifyCommittedNoPr:
    """_classify returns COMMITTED_NO_PR when commits exist but no open PR."""

    def test_committed_no_pr_when_gh_returns_empty_array(self) -> None:
        """Commits ahead of main + empty gh pr list array → COMMITTED_NO_PR."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout="+ abc123\n"),  # git cherry — 1 ahead
                _completed(stdout="my-branch\n"),  # git rev-parse (branch)
                _completed(stdout="[]"),  # gh pr list — no open PR
            ]
            result = _classify()
        assert result == RunOutcome.COMMITTED_NO_PR

    def test_gh_json_parsed_with_json_loads(self) -> None:
        """PR list JSON is parsed via json.loads, not grepped."""
        with patch("baton_harness.after_run._run") as mock_run:
            # JSON array with 'number' field — valid json.loads input
            pr_json = json.dumps([{"number": 7}])
            mock_run.side_effect = [
                _completed(stdout=""),  # git status
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout="+ abc\n"),  # git cherry
                _completed(stdout="my-branch\n"),  # git rev-parse (branch)
                _completed(stdout=pr_json),  # gh pr list
            ]
            result = _classify()
        # The presence of a PR means PR_OPENED, not COMMITTED_NO_PR
        assert result == RunOutcome.PR_OPENED


class TestClassifyPrOpened:
    """_classify returns PR_OPENED when an open PR exists for the branch."""

    def test_pr_opened_when_gh_returns_nonempty_array(self) -> None:
        """Non-empty gh pr list array → PR_OPENED."""
        with patch("baton_harness.after_run._run") as mock_run:
            pr_json = json.dumps([{"number": 42}])
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout="+ abc123\n"),  # git cherry — ahead
                _completed(stdout="my-branch\n"),  # git rev-parse (branch)
                _completed(stdout=pr_json),  # gh pr list — PR exists
            ]
            result = _classify()
        assert result == RunOutcome.PR_OPENED

    def test_pr_opened_uses_head_branch_from_git(self) -> None:
        """_classify passes the current branch name to gh pr list --head."""
        with patch("baton_harness.after_run._run") as mock_run:
            pr_json = json.dumps([{"number": 5}])
            mock_run.side_effect = [
                _completed(stdout=""),
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout="+ deadbeef\n"),
                _completed(stdout="feat-5-thing\n"),  # git rev-parse (branch)
                _completed(stdout=pr_json),
            ]
            _classify()
        # The fifth call (index 4) should include --head <branch>
        gh_call_args = mock_run.call_args_list[4][0][0]
        assert "gh" in gh_call_args
        assert "--head" in gh_call_args


# ---------------------------------------------------------------------------
# Label reconciliation — _reconcile_labels()
# ---------------------------------------------------------------------------

# gh issue view returns JSON with a 'labels' list of dicts with 'name' keys.
_LABEL_AGENT_READY = json.dumps({"labels": [{"name": "agent-ready"}]})
_LABEL_BLOCKED = json.dumps(
    {"labels": [{"name": "agent-ready"}, {"name": "blocked"}]}
)
_LABEL_NONE = json.dumps({"labels": []})


class TestReconcilePrOpened:
    """PR_OPENED → add agent-done, remove agent-ready."""

    def test_adds_agent_done_label(self) -> None:
        """_reconcile_labels adds agent-done when outcome is PR_OPENED."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_AGENT_READY),  # gh issue view
                _completed(),  # add agent-done
                _completed(),  # remove agent-ready
            ]
            exit_code = _reconcile_labels(42, RunOutcome.PR_OPENED)
        assert exit_code == 0
        # Verify add agent-done was called
        all_args = [c[0][0] for c in mock_run.call_args_list]
        add_call = next(
            (a for a in all_args if "--add-label" in a and "agent-done" in a),
            None,
        )
        assert add_call is not None

    def test_removes_agent_ready_label(self) -> None:
        """_reconcile_labels removes agent-ready when outcome is PR_OPENED."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_AGENT_READY),
                _completed(),  # add agent-done
                _completed(),  # remove agent-ready
            ]
            _reconcile_labels(42, RunOutcome.PR_OPENED)
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

    def test_label_edit_failure_propagates_non_zero(self) -> None:
        """A gh label-edit failure returns non-zero (not swallowed)."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_AGENT_READY),
                _completed(returncode=1),  # add agent-done fails
            ]
            exit_code = _reconcile_labels(42, RunOutcome.PR_OPENED)
        assert exit_code != 0


class TestReconcileBlocked:
    """blocked label present → remove agent-ready, leave blocked."""

    def test_removes_agent_ready_when_blocked(self) -> None:
        """_reconcile_labels removes agent-ready when blocked label present."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_BLOCKED),  # issue has both labels
                _completed(),  # remove agent-ready
            ]
            exit_code = _reconcile_labels(7, RunOutcome.COMMITTED_NO_PR)
        assert exit_code == 0
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

    def test_blocked_label_edit_failure_propagates(self) -> None:
        """A gh remove-label failure on the block path returns non-zero."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_BLOCKED),
                _completed(returncode=1),  # remove agent-ready fails
            ]
            exit_code = _reconcile_labels(7, RunOutcome.NO_COMMITS)
        assert exit_code != 0

    def test_blocked_takes_precedence_over_outcome(self) -> None:
        """The blocked label wins regardless of the F5 classification."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_BLOCKED),
                _completed(),
            ]
            # Even PR_OPENED outcome: blocked label presence overrides
            exit_code = _reconcile_labels(7, RunOutcome.PR_OPENED)
        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        # Should NOT add agent-done
        add_done = next(
            (a for a in all_args if "--add-label" in a and "agent-done" in a),
            None,
        )
        assert add_done is None


class TestReconcileBlockedSingleStateInvariant:
    """Regression tests for the block-path single-state label invariant (#4).

    Validates that when ``blocked`` is present:

    - ``agent-ready`` is removed (single-state: only ``blocked`` remains).
    - ``agent-done`` is NEVER added (block overrides F5 classification).
    - Label-edit failures are surfaced via non-zero exit, not swallowed.
    - The block wins regardless of F5 outcome (even ``PR_OPENED``).

    These tests were validated against the dry-run in issue #6 (T2).
    The upstream-dependent terminal-block fix is tracked in #23.
    """

    def test_blocked_and_agent_ready_removes_agent_ready_not_agent_done(
        self,
    ) -> None:
        """blocked+agent-ready: removes agent-ready, does NOT add agent-done.

        Single-state invariant: after reconciliation only ``blocked``
        remains.  ``agent-done`` must never be added on the block path.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_BLOCKED),  # issue has both labels
                _completed(),  # --remove-label agent-ready
            ]
            exit_code = _reconcile_labels(7, RunOutcome.COMMITTED_NO_PR)
        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        # --remove-label agent-ready must have been called
        remove_call = next(
            (
                a
                for a in all_args
                if "--remove-label" in a and "agent-ready" in a
            ),
            None,
        )
        assert remove_call is not None, (
            "--remove-label agent-ready was not called"
        )
        # --add-label agent-done must NOT have been called
        add_done = next(
            (a for a in all_args if "--add-label" in a and "agent-done" in a),
            None,
        )
        assert add_done is None, (
            "--add-label agent-done must not be called on the block path"
        )

    def test_blocked_without_agent_ready_no_remove_attempted(self) -> None:
        """Blocked present, agent-ready absent: no remove call, returns 0.

        When ``agent-ready`` is already absent there is nothing to remove;
        the harness must not attempt a redundant label edit.
        """
        blocked_only = json.dumps({"labels": [{"name": "blocked"}]})
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(
                    stdout=blocked_only
                ),  # only blocked, no agent-ready
            ]
            exit_code = _reconcile_labels(7, RunOutcome.NO_COMMITS)
        assert exit_code == 0
        # Only the issue-view call — no label-edit calls
        assert mock_run.call_count == 1, (
            "No gh issue edit should be attempted when agent-ready is absent"
        )

    def test_blocked_remove_label_failure_returns_nonzero(self) -> None:
        """Block path: --remove-label failure surfaces as non-zero exit.

        Label-edit failures must never be swallowed (H1 root cause was
        ``|| true`` silencing).  A non-zero returncode from the remove
        call must propagate to the caller.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_BLOCKED),  # both labels present
                _completed(returncode=1),  # --remove-label fails
            ]
            exit_code = _reconcile_labels(7, RunOutcome.COMMITTED_NO_PR)
        assert exit_code == 1

    def test_blocked_wins_over_pr_opened_outcome(self) -> None:
        """Block path takes priority over PR_OPENED F5 classification.

        Even when the agent opened a PR (``PR_OPENED``), the presence of
        ``blocked`` must override the outcome: ``agent-ready`` is removed,
        ``agent-done`` is NOT added, and the function returns ``0``.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_BLOCKED),  # both labels present
                _completed(),  # --remove-label agent-ready
            ]
            exit_code = _reconcile_labels(7, RunOutcome.PR_OPENED)
        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        # --remove-label agent-ready must be called
        remove_call = next(
            (
                a
                for a in all_args
                if "--remove-label" in a and "agent-ready" in a
            ),
            None,
        )
        assert remove_call is not None, (
            "--remove-label agent-ready must be called on the block path"
        )
        # --add-label agent-done must NOT be called (block overrides PR_OPENED)
        add_done = next(
            (a for a in all_args if "--add-label" in a and "agent-done" in a),
            None,
        )
        assert add_done is None, (
            "--add-label agent-done must not be called even with PR_OPENED "
            "when blocked is present"
        )


class TestReconcilePrOpenedLoopResilience:
    """Regression tests for Finding B — loop-resilience on PR_OPENED path.

    Before the fix, ``_reconcile_labels`` added ``agent-done`` first, then
    removed ``agent-ready``.  If the add succeeded but the remove failed, the
    issue was left with both labels and remained eligible for re-dispatch,
    causing an unbounded agent-run loop.

    After the fix the order is reversed: remove ``agent-ready`` first (exit
    the eligible set), then add ``agent-done``.  A failure on the add step
    returns non-zero but the issue is no longer eligible.
    """

    def test_remove_agent_ready_called_before_add_agent_done(self) -> None:
        """remove-agent-ready must precede add-agent-done on PR_OPENED path.

        Asserts the operation order so that a partial failure never leaves
        the issue eligible for re-dispatch.
        """
        call_order: list[str] = []

        def _side_effect(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            if "--add-label" in cmd and "agent-done" in cmd:
                call_order.append("add-agent-done")
            elif "--remove-label" in cmd and "agent-ready" in cmd:
                call_order.append("remove-agent-ready")
            return _completed(
                stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                if "--json" in cmd
                else ""
            )

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(
                    stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                ),  # gh issue view
                _completed(),  # first label op
                _completed(),  # second label op
            ]
            # Intercept after the initial issue-view call
            mock_run.side_effect = None
            mock_run.return_value = _completed(
                stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
            )

            # Replace side_effect with our ordering tracker
            mock_run.side_effect = _side_effect
            _reconcile_labels(42, RunOutcome.PR_OPENED)

        assert call_order == [
            "remove-agent-ready",
            "add-agent-done",
        ], (
            f"Expected remove-agent-ready before add-agent-done, "
            f"got: {call_order}"
        )

    def test_add_agent_done_fails_agent_ready_already_removed(self) -> None:
        """When add-agent-done fails, agent-ready must already be removed.

        Verifies Finding B: even on failure, the issue must not remain
        eligible (agent-ready absent) so baton cannot re-dispatch.
        """
        remove_called = False

        def _side_effect(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal remove_called
            if "--json" in cmd:
                return _completed(
                    stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                )
            if "--remove-label" in cmd and "agent-ready" in cmd:
                remove_called = True
                return _completed()  # remove succeeds
            if "--add-label" in cmd and "agent-done" in cmd:
                # add fails — but remove already happened above
                return _completed(returncode=1, stdout="")
            return _completed()

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = _side_effect
            exit_code = _reconcile_labels(42, RunOutcome.PR_OPENED)

        # Non-zero because add-agent-done failed
        assert exit_code != 0
        # But remove-agent-ready was already called (issue not left eligible)
        assert remove_called, (
            "remove-agent-ready must be called even when add-agent-done fails"
        )

    def test_happy_path_both_ops_succeed_returns_zero(self) -> None:
        """Happy path: remove-agent-ready then add-agent-done, exit 0.

        Confirms the reordered implementation still completes successfully.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(
                    stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                ),  # gh issue view
                _completed(),  # remove-agent-ready
                _completed(),  # add-agent-done
            ]
            exit_code = _reconcile_labels(42, RunOutcome.PR_OPENED)
        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        # remove-agent-ready is the second call (index 1)
        assert "--remove-label" in all_args[1]
        assert "agent-ready" in all_args[1]
        # add-agent-done is the third call (index 2)
        assert "--add-label" in all_args[2]
        assert "agent-done" in all_args[2]


class TestReconcileRetryable:
    """Retryable outcomes → remove agent-ready and set blocked (P0 change).

    The old Priority-3 behaviour (leave agent-ready for Baton retry) has been
    deleted.  The always-on daemon is the new retry authority — it polls for
    blocked issues.  On retryable F5 outcomes, the hook now removes
    agent-ready and adds blocked so the daemon can decide retry timing.
    """

    def test_no_commits_removes_agent_ready_sets_blocked(self) -> None:
        """NO_COMMITS: removes agent-ready and adds blocked."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_AGENT_READY),  # gh issue view
                _completed(),  # remove agent-ready
                _completed(),  # add blocked
            ]
            exit_code = _reconcile_labels(3, RunOutcome.NO_COMMITS)
        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        assert any(
            "--remove-label" in a and "agent-ready" in a for a in all_args
        )
        assert any("--add-label" in a and "blocked" in a for a in all_args)

    def test_uncommitted_changes_removes_agent_ready_sets_blocked(
        self,
    ) -> None:
        """UNCOMMITTED_CHANGES: removes agent-ready and adds blocked."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_AGENT_READY),
                _completed(),  # remove agent-ready
                _completed(),  # add blocked
            ]
            exit_code = _reconcile_labels(3, RunOutcome.UNCOMMITTED_CHANGES)
        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        assert any(
            "--remove-label" in a and "agent-ready" in a for a in all_args
        )
        assert any("--add-label" in a and "blocked" in a for a in all_args)

    def test_committed_no_pr_removes_agent_ready_sets_blocked(self) -> None:
        """COMMITTED_NO_PR: removes agent-ready and adds blocked."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_AGENT_READY),
                _completed(),  # remove agent-ready
                _completed(),  # add blocked
            ]
            exit_code = _reconcile_labels(3, RunOutcome.COMMITTED_NO_PR)
        assert exit_code == 0
        all_args = [c[0][0] for c in mock_run.call_args_list]
        assert any(
            "--remove-label" in a and "agent-ready" in a for a in all_args
        )
        assert any("--add-label" in a and "blocked" in a for a in all_args)


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


class TestMain:
    """Integration tests for main() entry point."""

    def test_returns_1_when_issue_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() returns 1 if the cwd name fails worktree convention."""
        bad_dir = tmp_path / "not-a-worktree-name"
        bad_dir.mkdir()
        monkeypatch.chdir(bad_dir)
        assert after_run.main() == 1

    def test_returns_0_on_pr_opened_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() returns 0 for the happy-path PR_OPENED outcome."""
        worktree = tmp_path / "feat-99-my-feature"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)
        pr_json = json.dumps([{"number": 99}])
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout="+ abc\n"),  # git cherry — ahead
                _completed(stdout="feat-99\n"),  # git rev-parse (branch)
                _completed(stdout=pr_json),  # gh pr list — PR open
                _completed(
                    stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                ),
                _completed(),  # remove agent-ready (first — loop-resilience)
                _completed(),  # add agent-done
            ]
            result = after_run.main()
        assert result == 0

    def test_returns_nonzero_on_label_edit_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() propagates non-zero when a label edit fails."""
        worktree = tmp_path / "feat-12-fail-case"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)
        pr_json = json.dumps([{"number": 12}])
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse — base SHA
                _completed(stdout="+ abc\n"),  # git cherry — ahead
                _completed(stdout="feat-12\n"),  # git rev-parse (branch)
                _completed(stdout=pr_json),  # gh pr list — PR open
                _completed(
                    stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                ),
                _completed(
                    returncode=1
                ),  # remove agent-ready fails (first op)
            ]
            result = after_run.main()
        assert result != 0


# ---------------------------------------------------------------------------
# Issue #76: after_run label constants import-redirect
# ---------------------------------------------------------------------------


class TestLabelConstantsRedirect:
    """after_run label constants must be re-exported from chain.labels.

    After the import redirect (issue #76), after_run removes its own
    constant definitions and imports them from
    ``baton_harness.chain.labels``.  The three names must still be
    accessible on the ``after_run`` module, and they must resolve to the
    same objects as those in ``baton_harness.chain.labels`` (not a copy).
    """

    def test_after_run_label_agent_ready_is_chain_labels_object(
        self,
    ) -> None:
        """after_run.LABEL_AGENT_READY is baton_harness.chain.labels object.

        Importing the same name from both modules must yield the same
        interned string object, confirming the redirect is live and not a
        duplicated constant.
        """
        import baton_harness.chain.labels as labels_mod

        assert after_run.LABEL_AGENT_READY is labels_mod.LABEL_AGENT_READY, (
            "after_run.LABEL_AGENT_READY must resolve to the same object as "
            "baton_harness.chain.labels.LABEL_AGENT_READY (import redirect "
            "not in place or value diverged)"
        )

    def test_after_run_label_agent_done_is_chain_labels_object(
        self,
    ) -> None:
        """after_run.LABEL_AGENT_DONE is baton_harness.chain.labels object."""
        import baton_harness.chain.labels as labels_mod

        assert after_run.LABEL_AGENT_DONE is labels_mod.LABEL_AGENT_DONE, (
            "after_run.LABEL_AGENT_DONE must resolve to the same object as "
            "baton_harness.chain.labels.LABEL_AGENT_DONE"
        )

    def test_after_run_label_blocked_is_chain_labels_object(self) -> None:
        """after_run.LABEL_BLOCKED is baton_harness.chain.labels object."""
        import baton_harness.chain.labels as labels_mod

        assert after_run.LABEL_BLOCKED is labels_mod.LABEL_BLOCKED, (
            "after_run.LABEL_BLOCKED must resolve to the same object as "
            "baton_harness.chain.labels.LABEL_BLOCKED"
        )


# ---------------------------------------------------------------------------
# Issue #31 — Phase 1: PR_OPENED path idempotency (AC1)
# ---------------------------------------------------------------------------

# Label JSON fixtures for torn / post-success re-run scenarios.
# "Torn state": agent-ready was removed but agent-done not yet added;
# only agent-in-progress remains (a non-state label).
_LABEL_TORN = json.dumps({"labels": [{"name": "agent-in-progress"}]})

# "Post-success re-run": both state-machine ops completed; only agent-done
# is present (agent-ready already gone).
_LABEL_AGENT_DONE_ONLY = json.dumps({"labels": [{"name": "agent-done"}]})


class TestReconcilePrOpenedIdempotency:
    """PR_OPENED path is idempotent on torn or fully-done re-runs.

    Issue #31 AC1: a second call to ``_reconcile_labels`` against a torn
    (zero-state) or fully-succeeded label set must converge to exactly one
    state label (``agent-done``) and return ``0`` without attempting to
    remove a label that is no longer present.
    """

    def test_pr_opened_rerun_after_torn_state_converges(self) -> None:
        """Re-run against torn state converges without spurious remove.

        Torn state: agent-ready was removed (before the SIGKILL) but
        agent-done was never added.  The only label present is
        agent-in-progress (a non-state label).

        Expected behaviour after the AC1 guard is implemented:
        - No ``--remove-label agent-ready`` call is issued (label absent).
        - ``--add-label agent-done`` IS issued.
        - ``_reconcile_labels`` returns ``0``.

        The side-effect list is intentionally long enough to absorb the
        current unconditional remove call so that mock does not raise
        StopIteration; the test asserts the call pattern, not the mock
        exhaustion behaviour.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=_LABEL_TORN),  # gh issue view
                _completed(),  # unconditional remove (current code) OR add
                _completed(),  # add (if remove fired above)
            ]
            exit_code = _reconcile_labels(42, RunOutcome.PR_OPENED)

        assert exit_code == 0, (
            "Expected exit 0 on torn-state re-run; got non-zero"
        )

        all_args = [c[0][0] for c in mock_run.call_args_list]

        # The absent label must not be removed (guard missing in prod code).
        remove_call = next(
            (
                a
                for a in all_args
                if "--remove-label" in a and "agent-ready" in a
            ),
            None,
        )
        assert remove_call is None, (
            "--remove-label agent-ready must NOT be issued when agent-ready "
            "is absent from the fetched label set; current code removes "
            "unconditionally, meaning add-agent-done is the second call "
            "instead of the first (or remove triggers a non-zero exit)"
        )

        # agent-done must still be added.
        add_call = next(
            (a for a in all_args if "--add-label" in a and "agent-done" in a),
            None,
        )
        assert add_call is not None, (
            "--add-label agent-done must be issued even when agent-ready "
            "is already absent"
        )

    def test_pr_opened_remove_skipped_when_agent_ready_absent(self) -> None:
        """Re-run after full success is a no-op-ish convergence.

        Post-success state: agent-ready already removed, agent-done already
        added.  A second ``_reconcile_labels(n, PR_OPENED)`` call must not
        attempt to remove the absent agent-ready label, and must return ``0``
        (full idempotency — converges to the same single state).
        """
        with patch("baton_harness.after_run._run") as mock_run:
            # Only side-effects that may legitimately fire:
            # 1. gh issue view (label fetch)
            # 2. Optionally: add agent-done (tolerated if gh is idempotent)
            mock_run.side_effect = [
                _completed(stdout=_LABEL_AGENT_DONE_ONLY),  # gh issue view
                _completed(),  # possible add (tolerated)
                _completed(),  # safety extra
            ]
            exit_code = _reconcile_labels(42, RunOutcome.PR_OPENED)

        assert exit_code == 0, (
            "Expected exit 0 on post-success re-run (full idempotency); "
            "got non-zero — unconditional remove of absent agent-ready "
            "causes gh to return non-zero, aborting the function"
        )

        all_args = [c[0][0] for c in mock_run.call_args_list]

        # The absent agent-ready must never be targeted for removal.
        remove_call = next(
            (
                a
                for a in all_args
                if "--remove-label" in a and "agent-ready" in a
            ),
            None,
        )
        assert remove_call is None, (
            "--remove-label agent-ready must not be issued when agent-ready "
            "is absent; post-success re-run must be idempotent"
        )


# ---------------------------------------------------------------------------
# Issue #31 — Phase 2: Scenario F kill-simulation (AC3)
# ---------------------------------------------------------------------------


class TestReconcileCrashRecoveryScenarioF:
    """Scenario F crash-recovery: kill between remove and add (AC3).

    Simulates a kill between remove-agent-ready and add-agent-done
    (Scenario F per harness-design.md §10).  The two-run sequence proves
    that the idempotent hook (AC1) converges torn state to exactly one
    state label (``agent-done``) on re-dispatch.
    """

    def test_kill_between_remove_and_add_then_rerun_converges(self) -> None:
        """Simulate SIGKILL after remove-agent-ready; re-run converges.

        The test models the Scenario F two-run sequence:

        Run 1 (the kill): labels = {agent-ready, agent-in-progress}.
        ``_reconcile_labels`` removes agent-ready first (Finding B ordering).
        The process is killed between the remove and the add, leaving the
        issue in torn state ({agent-in-progress} only, no state label).
        The test confirms that remove fires and add fires AFTER it — the
        remove-first ordering is what creates the exploitable torn window.

        Run 2 (re-dispatch after orphan scan): fresh call to
        ``_reconcile_labels`` with labels = {agent-in-progress} (torn).
        After the AC1 guard is in place:
        - add-agent-done fires and returns 0.
        - No spurious remove of the absent agent-ready.
        - Final state satisfies the single-state invariant with agent-done.

        The red state is in Run 2: without the AC1 guard, the unconditional
        remove of absent agent-ready returns non-zero from gh and the
        function aborts before adding agent-done, so run2_exit != 0 and
        run2_add_fired is False.
        """
        from baton_harness.chain.labels import (
            LABEL_AGENT_DONE,
            LABEL_AGENT_READY,
            STATE_LABELS,
            assert_single_state,
        )

        # ------------------------------------------------------------------
        # Run 1: normal labels — confirm remove fires before add (ordering).
        # This documents the torn-window: if killed after remove, before add,
        # the surviving state is exactly _LABEL_TORN.
        # ------------------------------------------------------------------
        run1_ops: list[str] = []

        def _run1_side_effect(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            """Record operation order for Run 1."""
            if "--json" in cmd:
                return _completed(
                    stdout=json.dumps(
                        {
                            "labels": [
                                {"name": "agent-ready"},
                                {"name": "agent-in-progress"},
                            ]
                        }
                    )
                )
            if "--remove-label" in cmd and LABEL_AGENT_READY in cmd:
                run1_ops.append("remove-agent-ready")
                return _completed()
            if "--add-label" in cmd and LABEL_AGENT_DONE in cmd:
                run1_ops.append("add-agent-done")
                return _completed()
            return _completed()

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = _run1_side_effect
            _reconcile_labels(55, RunOutcome.PR_OPENED)

        # remove must precede add in run 1 (Finding B ordering).
        assert "remove-agent-ready" in run1_ops, (
            "remove-agent-ready must fire in run 1"
        )
        remove_idx = run1_ops.index("remove-agent-ready")
        if "add-agent-done" in run1_ops:
            add_idx = run1_ops.index("add-agent-done")
            assert remove_idx < add_idx, (
                "remove-agent-ready must precede add-agent-done (Finding B)"
            )

        # ------------------------------------------------------------------
        # Run 2: torn state — only agent-in-progress present.
        # AC1 guard required: must add agent-done without removing absent
        # agent-ready, and must return 0.
        # ------------------------------------------------------------------
        run2_remove_fired = False
        run2_add_fired = False

        def _run2_side_effect(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            """Track ops for Run 2 against torn state."""
            nonlocal run2_remove_fired, run2_add_fired
            if "--json" in cmd:
                return _completed(stdout=_LABEL_TORN)
            if "--remove-label" in cmd and LABEL_AGENT_READY in cmd:
                run2_remove_fired = True
                # Simulate gh's non-zero exit for removing an absent label.
                return _completed(
                    returncode=1,
                    stdout="Label 'agent-ready' is not on issue #55",
                )
            if "--add-label" in cmd and LABEL_AGENT_DONE in cmd:
                run2_add_fired = True
                return _completed()
            return _completed()

        with patch("baton_harness.after_run._run") as mock_run2:
            mock_run2.side_effect = _run2_side_effect
            run2_exit = _reconcile_labels(55, RunOutcome.PR_OPENED)

        assert run2_exit == 0, (
            f"Re-run against torn state must return 0; got {run2_exit}. "
            "Without the AC1 guard the unconditional remove of absent "
            "agent-ready returns non-zero from gh and the function aborts "
            "before adding agent-done."
        )

        assert run2_add_fired, (
            "add-agent-done must fire in run 2 (convergence of torn state)"
        )

        assert not run2_remove_fired, (
            "--remove-label agent-ready must NOT fire in run 2 when "
            "agent-ready is absent from the torn-state label set"
        )

        # Final state: exactly one state label (agent-done).
        final_labels = {"agent-in-progress", LABEL_AGENT_DONE}
        violation = assert_single_state(final_labels)
        assert violation is None, (
            f"Final state must satisfy the single-state invariant; "
            f"assert_single_state reported: {violation!r}"
        )
        state_in_final = final_labels & STATE_LABELS
        assert state_in_final == {LABEL_AGENT_DONE}, (
            f"Final state label must be exactly {{agent-done}}; "
            f"got {state_in_final!r}"
        )
