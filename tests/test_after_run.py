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
                _completed(stdout=""),  # git cherry — no ahead commits
            ]
            result = _classify()
        assert result == RunOutcome.NO_COMMITS

    def test_no_commits_when_cherry_has_no_plus_lines(self) -> None:
        """Cherry output with only minus lines → NO_COMMITS."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
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
                _completed(stdout="+ deadbeef\n"),
                _completed(stdout="feat-5-thing\n"),  # git rev-parse
                _completed(stdout=pr_json),
            ]
            _classify()
        # The fourth call (index 3) should include --head <branch>
        gh_call_args = mock_run.call_args_list[3][0][0]
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


class TestReconcileRetryable:
    """Retryable outcomes → leave agent-ready; no label changes."""

    def test_no_commits_leaves_agent_ready(self) -> None:
        """NO_COMMITS leaves agent-ready in place (no gh edits)."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(stdout=_LABEL_AGENT_READY)
            exit_code = _reconcile_labels(3, RunOutcome.NO_COMMITS)
        assert exit_code == 0
        # Only the issue-view call; no label-edit calls
        assert mock_run.call_count == 1

    def test_uncommitted_changes_leaves_agent_ready(self) -> None:
        """UNCOMMITTED_CHANGES leaves agent-ready in place."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(stdout=_LABEL_AGENT_READY)
            exit_code = _reconcile_labels(3, RunOutcome.UNCOMMITTED_CHANGES)
        assert exit_code == 0
        assert mock_run.call_count == 1

    def test_committed_no_pr_leaves_agent_ready(self) -> None:
        """COMMITTED_NO_PR leaves agent-ready in place."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(stdout=_LABEL_AGENT_READY)
            exit_code = _reconcile_labels(3, RunOutcome.COMMITTED_NO_PR)
        assert exit_code == 0
        assert mock_run.call_count == 1


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
        pr_json = json.dumps([{"number": 99}])
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="+ abc\n"),  # git cherry — ahead
                _completed(stdout="feat-99\n"),  # git rev-parse (branch)
                _completed(stdout=pr_json),  # gh pr list — PR open
                _completed(
                    stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                ),
                _completed(),  # add agent-done
                _completed(),  # remove agent-ready
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
        pr_json = json.dumps([{"number": 12}])
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="+ abc\n"),  # git cherry — ahead
                _completed(stdout="feat-12\n"),  # git rev-parse (branch)
                _completed(stdout=pr_json),  # gh pr list — PR open
                _completed(
                    stdout=json.dumps({"labels": [{"name": "agent-ready"}]})
                ),
                _completed(returncode=1),  # add agent-done fails
            ]
            result = after_run.main()
        assert result != 0
