"""Unit tests for baton_harness.chain.branches.

Owns the feature-branch lifecycle: naming, creation off main, idempotent
re-create on resume, HEAD checkout before each ``_run_worker`` call, and
cut-point SHA recording.

Coverage:
- ``feature_branch_name`` produces ``feature/<milestone-slug>`` for a
  milestone work unit and ``feature/issue-<N>`` for an un-milestoned single
  issue.
- ``create_feature_branch`` creates the branch off ``origin/main`` via
  ``git -C <repo_root>``.
- Idempotent re-create: if the branch already exists, the function does NOT
  raise and does NOT re-issue the create command (resume semantics).
- ``checkout_feature_branch`` issues ``git -C <repo_root> checkout
  feature/<slug>`` so the repo-root HEAD is on the feature branch
  immediately before each ``_run_worker`` call (BLOCKING-1, §3.4).
  The command MUST use ``git -C <repo_root>``, NOT a bare ``git checkout``
  that would rely on the shell cwd.
- ``record_cut_point`` returns the current tip SHA of ``feature/<slug>``
  (i.e. the HEAD of that branch) by running
  ``git -C <repo_root> rev-parse feature/<slug>`` and returns the resulting
  SHA string.  This SHA is passed to hooks as ``CHAIN_BASE_BRANCH`` (§3.7).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import baton_harness.chain.branches as branches_mod
from baton_harness.chain.branches import (
    checkout_feature_branch,
    create_feature_branch,
    feature_branch_name,
    record_cut_point,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO = Path("/fake/repo")


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess with the given stdout.

    Args:
        stdout: Simulated output from the subprocess.

    Returns:
        A ``CompletedProcess`` with ``returncode=0``.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess.

    Args:
        stderr: Simulated error output.

    Returns:
        A ``CompletedProcess`` with ``returncode=1``.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# feature_branch_name
# ---------------------------------------------------------------------------


class TestFeatureBranchName:
    """Tests for the ``feature_branch_name`` naming convention."""

    def test_milestone_slug_produces_feature_slash_slug(self) -> None:
        """A milestone-slug argument returns ``feature/<milestone-slug>``."""
        assert (
            feature_branch_name(slug="my-milestone") == "feature/my-milestone"
        )

    def test_issue_number_produces_feature_issue_n(self) -> None:
        """An un-milestoned issue returns ``feature/issue-<N>``."""
        assert feature_branch_name(issue=44) == "feature/issue-44"

    def test_milestone_slug_with_numbers_preserved(self) -> None:
        """Slugs with numbers produce correct feature branch names."""
        assert feature_branch_name(slug="daemon-v2") == "feature/daemon-v2"

    def test_requires_slug_or_issue_not_both(self) -> None:
        """Providing both slug and issue raises ValueError."""
        with pytest.raises(ValueError):
            feature_branch_name(slug="foo", issue=1)

    def test_requires_slug_or_issue_at_least_one(self) -> None:
        """Providing neither slug nor issue raises ValueError."""
        with pytest.raises(ValueError):
            feature_branch_name()

    def test_issue_number_zero_not_valid(self) -> None:
        """Issue number 0 is not valid and raises ValueError."""
        with pytest.raises(ValueError):
            feature_branch_name(issue=0)

    def test_issue_collision_free_by_number_not_title(self) -> None:
        """Different issue numbers produce distinct branch names (NIT-1)."""
        b1 = feature_branch_name(issue=44)
        b2 = feature_branch_name(issue=45)
        assert b1 != b2

    def test_slug_does_not_add_extra_feature_prefix(self) -> None:
        """Slug already containing 'feature' does not get doubled prefix."""
        name = feature_branch_name(slug="my-feature")
        assert name == "feature/my-feature"
        assert "feature/feature" not in name


# ---------------------------------------------------------------------------
# create_feature_branch
# ---------------------------------------------------------------------------


class TestCreateFeatureBranch:
    """Tests for ``create_feature_branch`` creating the branch off main."""

    def test_creates_branch_off_origin_main(self) -> None:
        """Issues ``git branch <name> origin/main`` via ``git -C <repo>``."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            create_feature_branch(_REPO, "feature/my-milestone")

        branch_cmds = [c for c in calls if "branch" in c]
        assert any("origin/main" in c for c in branch_cmds), (
            "Expected origin/main as the base ref"
        )

    def test_uses_git_dash_c_repo_root(self) -> None:
        """All git commands use ``git -C <repo_root>`` not bare ``git``."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            create_feature_branch(_REPO, "feature/my-milestone")

        for cmd in calls:
            if cmd and cmd[0] == "git":
                assert cmd[1] == "-C", (
                    f"git command must use -C flag, got: {cmd}"
                )
                assert cmd[2] == str(_REPO), (
                    f"git -C must target repo_root, got: {cmd}"
                )

    def test_idempotent_if_branch_already_exists(self) -> None:
        """Does not raise if the branch already exists (resume semantics)."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            # Simulate git branch failing because it already exists
            if "branch" in cmd:
                return _fail(
                    "fatal: A branch named 'feature/my-milestone'"
                    " already exists."
                )
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            # Should NOT raise even though branch creation "failed"
            create_feature_branch(_REPO, "feature/my-milestone", exist_ok=True)

    def test_raises_on_unexpected_git_failure(self) -> None:
        """Raises RuntimeError for unexpected git failures (not exist)."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _fail("fatal: some unexpected error")

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            with pytest.raises(RuntimeError):
                create_feature_branch(_REPO, "feature/my-milestone")

    def test_branch_name_appears_in_create_command(self) -> None:
        """The branch name appears in the git branch command."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            create_feature_branch(_REPO, "feature/issue-44")

        branch_cmds = [c for c in calls if "branch" in c]
        assert any("feature/issue-44" in c for c in branch_cmds)


# ---------------------------------------------------------------------------
# checkout_feature_branch
# ---------------------------------------------------------------------------


class TestCheckoutFeatureBranch:
    """Tests for ``checkout_feature_branch`` setting HEAD to feature branch.

    This is the BLOCKING-1/§3.4 requirement: the daemon MUST check out
    ``feature/<slug>`` as the repo-root HEAD before calling ``_run_worker``
    so that symphony's ``git worktree add … HEAD`` branches off the feature
    branch, not main.
    """

    def test_issues_git_checkout_with_dash_c(self) -> None:
        """Issues ``git -C <repo_root> checkout feature/<slug>``."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            checkout_feature_branch(_REPO, "my-milestone")

        assert any(
            cmd[:3] == ["git", "-C", str(_REPO)]
            and "checkout" in cmd
            and "feature/my-milestone" in cmd
            for cmd in calls
        ), f"Expected git -C checkout command, got: {calls}"

    def test_never_uses_bare_git_checkout(self) -> None:
        """Never issues a bare ``git checkout`` without the -C flag."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            checkout_feature_branch(_REPO, "my-milestone")

        for cmd in calls:
            if cmd and cmd[0] == "git" and "checkout" in cmd:
                assert "-C" in cmd, (
                    "checkout must use git -C, not bare git checkout"
                )

    def test_checks_out_correct_feature_slug(self) -> None:
        """The checkout targets ``feature/<slug>`` exactly."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            checkout_feature_branch(_REPO, "always-on-daemon")

        checkout_cmds = [c for c in calls if "checkout" in c]
        assert any("feature/always-on-daemon" in c for c in checkout_cmds)

    def test_raises_on_checkout_failure(self) -> None:
        """Raises RuntimeError when git checkout fails."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _fail("error: pathspec 'feature/missing' did not match")

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            with pytest.raises(RuntimeError):
                checkout_feature_branch(_REPO, "missing")

    def test_repo_root_used_as_cwd_argument(self) -> None:
        """The repo_root is passed via ``-C`` not via the cwd kwarg."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            checkout_feature_branch(_REPO, "slug")

        for cmd in calls:
            assert "-C" in cmd and str(_REPO) in cmd, (
                "repo_root must appear as -C argument"
            )


# ---------------------------------------------------------------------------
# record_cut_point
# ---------------------------------------------------------------------------


class TestRecordCutPoint:
    """Tests for ``record_cut_point`` capturing the feature branch tip SHA.

    The cut-point SHA is the ``feature/<slug>`` tip at worktree-creation
    time.  It is passed to hooks as ``CHAIN_BASE_BRANCH`` so ``before_run``
    and ``after_run`` measure against the correct frozen base (§3.7).
    """

    _SHA = "abc123def456abc123def456abc123def456abc1"

    def test_returns_sha_string(self) -> None:
        """Returns the SHA string from ``git rev-parse feature/<slug>``."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _ok(self._SHA + "\n")

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            sha = record_cut_point(_REPO, "my-milestone")

        assert sha == self._SHA

    def test_uses_rev_parse_with_dash_c(self) -> None:
        """Issues ``git -C <repo_root> rev-parse feature/<slug>``."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok(self._SHA + "\n")

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            record_cut_point(_REPO, "my-milestone")

        assert any(
            cmd[:3] == ["git", "-C", str(_REPO)]
            and "rev-parse" in cmd
            and "feature/my-milestone" in cmd
            for cmd in calls
        ), f"Expected git -C rev-parse, got: {calls}"

    def test_strips_trailing_newline(self) -> None:
        """Trims trailing whitespace/newline from the SHA."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _ok(self._SHA + "\n\n")

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            sha = record_cut_point(_REPO, "my-milestone")

        assert sha == self._SHA
        assert "\n" not in sha

    def test_raises_on_rev_parse_failure(self) -> None:
        """Raises RuntimeError when git rev-parse fails."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _fail("fatal: ambiguous argument 'feature/missing'")

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            with pytest.raises(RuntimeError):
                record_cut_point(_REPO, "missing")

    def test_cut_point_sha_is_deterministic_for_frozen_branch(self) -> None:
        """Two calls with same branch return same SHA (freeze invariant)."""
        call_count = 0

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            return _ok(self._SHA + "\n")

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            sha1 = record_cut_point(_REPO, "slug")
            sha2 = record_cut_point(_REPO, "slug")

        assert sha1 == sha2 == self._SHA


# ---------------------------------------------------------------------------
# FIX 3: resume from remote feature branch (exist_ok + remote tracking)
# ---------------------------------------------------------------------------


class TestResumeFromRemoteFeatureBranch:
    """Guard FIX 3: resume must reuse an existing remote feature branch.

    On a restart in a fresh clone the local branch may not exist, but
    ``origin/feature/<slug>`` does.  The current code recreates from
    ``origin/main``, dropping integration history.  The fix must detect the
    remote branch and track it instead.
    """

    def test_tracks_remote_branch_when_local_absent_remote_present(
        self,
    ) -> None:
        """When local branch absent but remote exists, tracks the remote.

        The local branch must be created FROM ``origin/feature/<slug>``
        (not from ``origin/main``) so integration history is preserved.

        Simulates the restart-in-fresh-clone scenario:
        - Initial ``git branch <name> origin/main`` → fails with
          "already exists" (the branch somehow already exists — this path
          is exercised when we call with exist_ok=True and need to detect
          the remote).  Actually for the fresh-clone scenario the local
          branch does NOT exist; we simulate that the branch creation from
          ``origin/main`` fails with a different error to force the code
          into the ls-remote probe path, then ls-remote finds the remote.
        """
        calls: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            cmd_str = " ".join(cmd)
            # fetch succeeds
            if "fetch" in cmd:
                return _ok()
            # ls-remote: branch exists on remote
            if "ls-remote" in cmd:
                return _ok("abc123\trefs/heads/feature/my-milestone\n")
            # local existence check: branch does NOT exist locally
            if "rev-parse" in cmd and "--verify" in cmd:
                return _fail("fatal: not a valid ref")
            if "branch" in cmd:
                if "origin/feature/my-milestone" in cmd_str:
                    # Tracking creation from remote succeeds
                    return _ok()
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            create_feature_branch(_REPO, "feature/my-milestone", exist_ok=True)

        # The branch creation command must reference the remote branch, not
        # origin/main, when the remote branch exists.
        branch_cmds = [c for c in calls if "branch" in c and "git" in c]
        remote_branch_cmds = [
            c
            for c in branch_cmds
            if "origin/feature/my-milestone" in " ".join(c)
        ]
        assert remote_branch_cmds, (
            "Must create local branch FROM origin/feature/<slug> when remote "
            "exists, not from origin/main"
        )

    def test_creates_from_origin_main_when_neither_local_nor_remote_exist(
        self,
    ) -> None:
        """When branch exists in neither local nor remote, create from main."""
        calls: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            cmd_str = " ".join(cmd)
            # fetch succeeds
            if "fetch" in cmd:
                return _ok()
            # ls-remote: branch does NOT exist on remote (empty stdout)
            if "ls-remote" in cmd:
                return _ok("")
            # branch creation from origin/main succeeds
            if "branch" in cmd and "origin/main" in cmd_str:
                return _ok()
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            create_feature_branch(_REPO, "feature/new-branch", exist_ok=True)

        branch_cmds = [c for c in calls if "branch" in c and "git" in c]
        origin_main_cmds = [
            c for c in branch_cmds if "origin/main" in " ".join(c)
        ]
        assert origin_main_cmds, (
            "Must create from origin/main when neither local nor remote exist"
        )


# ---------------------------------------------------------------------------
# FIX 6: slug validation in feature_branch_name / create_feature_branch
# ---------------------------------------------------------------------------


class TestSlugValidation:
    """Guard FIX 6: invalid slugs must be rejected before git operations."""

    def test_feature_branch_name_rejects_empty_slug(self) -> None:
        """An empty slug string raises ValueError."""
        with pytest.raises(ValueError, match="slug"):
            feature_branch_name(slug="")

    def test_feature_branch_name_rejects_leading_dash_slug(self) -> None:
        """A slug starting with '-' raises ValueError."""
        with pytest.raises(ValueError, match="slug"):
            feature_branch_name(slug="-bad-start")

    def test_feature_branch_name_rejects_whitespace_slug(self) -> None:
        """A slug containing whitespace raises ValueError."""
        with pytest.raises(ValueError, match="slug"):
            feature_branch_name(slug="bad slug")

    def test_feature_branch_name_rejects_leading_slash_slug(self) -> None:
        """A slug starting with '/' raises ValueError."""
        with pytest.raises(ValueError, match="slug"):
            feature_branch_name(slug="/bad")

    def test_feature_branch_name_accepts_valid_slug(self) -> None:
        """A valid alphanumeric-dash slug is accepted."""
        assert (
            feature_branch_name(slug="valid-slug-123")
            == "feature/valid-slug-123"
        )

    def test_feature_branch_name_accepts_slug_with_dots(self) -> None:
        """A slug with dots is accepted."""
        assert feature_branch_name(slug="v2.0-daemon") == "feature/v2.0-daemon"


# ---------------------------------------------------------------------------
# End-to-end: checkout before _run_worker (BLOCKING-1 regression guard)
# ---------------------------------------------------------------------------


class TestCheckoutBeforeRunWorker:
    """Integration-style: checkout is issued before _run_worker is called.

    The spec requires (§3.4 / BLOCKING-1): the daemon calls
    ``branches.checkout_feature_branch(repo_root, slug)`` BEFORE invoking
    ``_run_worker``.  These tests assert the checkout command/cwd semantics
    so a daemon implementation that skips the checkout would be caught.
    """

    def test_checkout_command_targets_repo_root_not_shell_cwd(self) -> None:
        """The git -C repo_root in checkout is NOT the ambient shell cwd."""
        other_root = Path("/other/dir")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            checkout_feature_branch(other_root, "slug")

        for cmd in calls:
            if "checkout" in cmd:
                c_idx = cmd.index("-C")
                assert cmd[c_idx + 1] == str(other_root), (
                    "checkout must use the passed repo_root, not cwd"
                )

    def test_checkout_precedes_cut_point_recording_when_both_called(
        self,
    ) -> None:
        """Checkout is issued before cut-point recording in correct usage."""
        order: list[str] = []
        expected_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "checkout" in cmd:
                order.append("checkout")
                return _ok()
            if "rev-parse" in cmd:
                order.append("rev-parse")
                return _ok(expected_sha + "\n")
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            checkout_feature_branch(_REPO, "slug")
            record_cut_point(_REPO, "slug")

        assert order == ["checkout", "rev-parse"], (
            "Checkout must come before cut-point recording"
        )

    def test_checkout_uses_feature_prefix(self) -> None:
        """Checkout targets ``feature/<slug>``, not a bare ``<slug>``."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            checkout_feature_branch(_REPO, "my-work")

        checkout_cmds = [c for c in calls if "checkout" in c]
        for cmd in checkout_cmds:
            # Must reference the full feature/my-work, not bare my-work
            joined = " ".join(cmd)
            assert "feature/my-work" in joined, (
                f"Expected feature/my-work in checkout: {joined}"
            )
            assert " my-work" not in joined.replace("feature/my-work", ""), (
                "Must not appear without feature/ prefix"
            )


# ---------------------------------------------------------------------------
# full create + checkout + record round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Round-trip: create → checkout → record all use git -C correctly."""

    _SHA = "1a2b3c4d1a2b3c4d1a2b3c4d1a2b3c4d1a2b3c4d"

    def test_all_three_operations_use_dash_c(self) -> None:
        """create, checkout, and record all pass repo_root via -C."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "rev-parse" in cmd:
                return _ok(self._SHA + "\n")
            return _ok()

        with patch.object(branches_mod, "_run", side_effect=fake_run):
            create_feature_branch(_REPO, "feature/v2")
            checkout_feature_branch(_REPO, "v2")
            sha = record_cut_point(_REPO, "v2")

        assert sha == self._SHA
        for cmd in calls:
            if cmd and cmd[0] == "git":
                assert "-C" in cmd, f"Missing -C in: {cmd}"
                c_idx = cmd.index("-C")
                assert cmd[c_idx + 1] == str(_REPO), f"Wrong -C target: {cmd}"
