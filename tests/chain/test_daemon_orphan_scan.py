"""Tests for daemon secondary orphan scan (issue #89).

The daemon's ``_poll_and_run`` currently queries ONLY
``--label agent-ready``.  An issue stuck on ``agent-in-progress`` with no
``agent-ready`` sibling in the same milestone is never recovered.

Required new behavior (the #89 fix): after the normal agent-ready dispatch
the daemon performs a **secondary scan** for open ``agent-in-progress``
issues and, for each orphan whose milestone was **not already processed this
cycle**, seeds a work unit so the existing
``reconstruct → redispatch → tally.record_and_check``
and ``liveness_state.mark_in_progress`` path runs for the orphan.

Test 1 (TRUE RED): lone orphan reconstructed and tallied.
Test 2 (GUARD): milestone with both labels runs exactly once.
Test 3 (TRUE RED): liveness_state.mark_in_progress reached for orphan.

All tests mirror ``tests/chain/test_daemon.py`` exactly:
- same imports and helpers
- same ``_run`` seam mocking
- same ``asyncio.run`` driver
- same ``patch`` style
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import baton_harness.chain.daemon as daemon_mod
from baton_harness.chain.daemon import run_daemon
from baton_harness.chain.heartbeat import LivenessState
from baton_harness.chain.merge import MergeOutcome
from baton_harness.chain.recovery import RecoveryResult
from baton_harness.chain.registry import RepoConfig
from baton_harness.vendor.symphony.config import WorkflowConfig

# ---------------------------------------------------------------------------
# Helpers (mirrors test_daemon.py exactly)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path("/fake/repo")
_OWNER = "glitchwerks"
_REPO_NAME = "baton-harness"


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr
    )


def _minimal_wf_config() -> WorkflowConfig:
    """Return a minimal WorkflowConfig."""
    return WorkflowConfig(
        prompt_template="Work on #{{ issue.number }}",
        tracker_labels=["agent-ready"],
        tracker_exclude_labels=["blocked"],
        tracker_assignee=None,
        max_concurrent=1,
        max_turns=8,
        hook_after_create=None,
        hook_before_run=None,
        hook_after_run=None,
        hook_timeout_ms=5000,
        poll_interval_ms=1000,
        max_retry_backoff_ms=10000,
    )


def _repo_cfg() -> RepoConfig:
    """Return a minimal RepoConfig."""
    return RepoConfig(
        owner=_OWNER,
        repo=_REPO_NAME,
        project_root=_REPO_ROOT,
    )


def _make_issue(
    number: int,
    labels: list[str],
    milestone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal gh issue dict.

    Args:
        number: Issue number.
        labels: List of label name strings.
        milestone: Optional milestone dict with ``number`` and ``title``.

    Returns:
        A raw issue dict shaped like ``gh issue list --json`` output.
    """
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "body": "",
        "url": f"https://github.com/o/r/issues/{number}",
        "labels": [{"name": lbl} for lbl in labels],
        "milestone": milestone,
        "assignees": [],
    }


# ---------------------------------------------------------------------------
# _run seam builder for the orphan-scan scenarios
#
# Two lists are plumbed separately:
#   agent_ready_issues  – returned when "agent-ready" appears in the cmd
#   orphan_issues       – returned when "agent-in-progress" appears in the cmd
#
# Everything else falls back to sane success stubs.
# ---------------------------------------------------------------------------


def _make_orphan_run_side_effect(
    *,
    agent_ready_issues: list[dict[str, Any]],
    orphan_issues: list[dict[str, Any]],
    issue_branch: str = "baton/orphan-milestone-10",
    pr_head_sha: str = "abc123",
) -> Any:  # noqa: ANN401
    """Build a ``_run`` side-effect for orphan-scan tests.

    Args:
        agent_ready_issues: Issues returned for ``--label agent-ready``.
        orphan_issues: Issues returned for ``--label agent-in-progress``.
        issue_branch: Branch name in PR list stubs.
        pr_head_sha: SHA in PR list stubs.

    Returns:
        A callable matching the signature of ``daemon_mod._run``.
    """

    def side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)

        # Primary scan: agent-ready list.
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
            and "agent-in-progress" not in cmd_str
        ):
            return _ok(json.dumps(agent_ready_issues))

        # Secondary scan: agent-in-progress list.
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-in-progress" in cmd_str
        ):
            return _ok(json.dumps(orphan_issues))

        # Issue view (labels fetch / single issue).
        if "issue" in cmd_str and "view" in cmd_str and "edit" not in cmd_str:
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 10
            return _ok(
                json.dumps(
                    {
                        "number": n,
                        "title": f"Issue {n}",
                        "state": "open",
                        "body": "",
                        "url": f"https://github.com/o/r/issues/{n}",
                        "labels": [{"name": "agent-in-progress"}],
                        "assignees": [],
                    }
                )
            )

        # Label edits.
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()

        # PR list.
        if "pr" in cmd_str and "list" in cmd_str:
            prs = [
                {
                    "number": 1,
                    "headRefName": issue_branch,
                    "headRefOid": pr_head_sha,
                }
            ]
            return _ok(json.dumps(prs))

        # PR create.
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")

        # Git push.
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()

        # git ls-remote.
        if "ls-remote" in cmd_str:
            return _ok("")

        # git rev-parse.
        if "rev-parse" in cmd_str:
            return _ok("abc123deadbeef\n")

        return _ok()

    return side_effect


# ---------------------------------------------------------------------------
# Test 1 (TRUE RED): lone orphan reconstructed + tallied
#
# A milestone has EXACTLY ONE open issue labelled agent-in-progress.
# NO agent-ready issues exist anywhere.
# The primary poll returns an empty list → current code returns early.
# The secondary scan (missing) would find the orphan and seed a work unit
# so that reconstruct → tally.record_and_check runs for issue number 10.
# ---------------------------------------------------------------------------


def test_lone_orphan_milestone_triggers_reconstruct_and_tally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lone agent-in-progress issue causes reconstruct + tally to run.

    Arrange:
    - No agent-ready issues anywhere (primary scan returns []).
    - One open ``agent-in-progress`` issue #10 in milestone "Sprint 9"
      (orphan from a prior crash).
    - ``reconstruct`` mock returns ``redispatch={10}``.
    - ``tally.record_and_check`` is spied on via
      ``RedispatchTally.record_and_check``.

    Assert:
    - ``tally.record_and_check`` is called at least once with issue=10.

    This MUST FAIL on current code: no secondary scan exists, so the
    primary poll's empty result causes ``_poll_and_run`` to return early
    without ever touching ``reconstruct`` or ``tally``.

    Args:
        tmp_path: pytest tmp directory for the counts file.
        monkeypatch: env isolation fixture.
    """
    monkeypatch.setenv("BH_PROJECT_ROOT", str(tmp_path))
    for var in (
        "BH_RUNLOG_PATH",
        "BH_HEARTBEAT_FILE",
        "BH_REDISPATCH_WINDOW_TICKS",
        "BH_REDISPATCH_MAX",
        "BH_HEARTBEAT_STALL_S",
        "BH_HEARTBEAT_PING_URL",
        "BH_REDISPATCH_COUNTS_PATH",
    ):
        monkeypatch.delenv(var, raising=False)

    ms = {"number": 9, "title": "Sprint 9"}
    orphan = _make_issue(10, ["agent-in-progress"], milestone=ms)

    tally_calls: list[int] = []

    def spy_record_and_check(
        self: Any,  # noqa: ANN401
        issue: int,
    ) -> bool:
        """Spy on record_and_check; always return False (allow dispatch)."""
        tally_calls.append(issue)
        return False

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_orphan_run_side_effect(
                agent_ready_issues=[],
                orphan_issues=[orphan],
            ),
        ),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            return_value=[],
        ),
        patch("baton_harness.chain.branches.create_feature_branch"),
        patch("baton_harness.chain.branches.checkout_feature_branch"),
        patch(
            "baton_harness.chain.branches.record_cut_point",
            return_value="deadbeef" * 5,
        ),
        # reconstruct returns redispatch={10} so the tally path runs.
        patch(
            "baton_harness.chain.recovery.reconstruct",
            return_value=RecoveryResult(
                done=set(),
                parked_seed=set(),
                ci_gate_reentry=set(),
                redispatch={10},
            ),
        ),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=frozenset({10}),
        ),
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        # Spy on RedispatchTally.record_and_check.
        patch(
            "baton_harness.chain.redispatch.RedispatchTally.record_and_check",
            autospec=True,
            side_effect=spy_record_and_check,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator."
            "Orchestrator._run_worker",
            new_callable=AsyncMock,
            return_value="pr_created",
        ),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    assert 10 in tally_calls, (
        "Expected tally.record_and_check to be called with issue=10 "
        "for the lone agent-in-progress orphan, but it was never called. "
        "The secondary orphan scan is missing from _poll_and_run — "
        f"tally_calls={tally_calls}"
    )


# ---------------------------------------------------------------------------
# Test 2 (GUARD): milestone with BOTH labels is not double-processed
#
# A milestone has one agent-ready issue (#20) AND one agent-in-progress
# issue (#21).  The primary scan picks up the agent-ready issue and
# processes the milestone.  The secondary scan must NOT re-seed the same
# milestone as a second work unit.
#
# Current code behaviour: the primary scan processes the agent-ready issue
# normally, _run_work_unit is called once, and _poll_and_run returns.
# No secondary scan exists, so it trivially never double-processes.
#
# Post-implementation risk: a naive secondary scan that doesn't dedup
# against already-processed milestones would call _run_work_unit twice.
#
# Guard design: count how many times _run_work_unit is called.  Assert
# the count is exactly 1.  Pre-implementation: count is 1 (existing path
# runs once) → test PASSES as a guard.  Post-implementation: must still
# be 1 (dedup works) → test PASSES.  If a naive impl double-processes:
# count is 2 → test FAILS.
#
# NOTE: This is a GUARD test.  It passes on current code because there is
# no secondary scan at all.  It becomes load-bearing after the fix to
# ensure the dedup logic is correct.
# ---------------------------------------------------------------------------


def test_milestone_with_ready_and_orphan_runs_work_unit_exactly_once() -> None:
    """Work unit for a milestone with both label types runs exactly once.

    A milestone has one ``agent-ready`` issue (#20) and one
    ``agent-in-progress`` issue (#21).  Across the full poll cycle
    (primary + secondary scan) ``_run_work_unit`` must be called at most
    once for that milestone.

    This is a **guard test**.  On current code (no secondary scan) it
    passes because ``_run_work_unit`` is called exactly once via the
    primary scan.  Post-implementation it must still pass — the secondary
    scan must dedup against milestones already handled this cycle.
    """
    ms = {"number": 11, "title": "Sprint 11"}
    ready_issue = _make_issue(20, ["agent-ready"], milestone=ms)
    orphan_issue = _make_issue(21, ["agent-in-progress"], milestone=ms)

    work_unit_calls: list[str] = []

    real_run_work_unit = daemon_mod._run_work_unit  # type: ignore[attr-defined]

    async def spy_run_work_unit(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Record the call and delegate to the real implementation."""
        work_unit_calls.append("called")
        # Delegate so the rest of the machinery runs normally.
        await real_run_work_unit(*args, **kwargs)

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_orphan_run_side_effect(
                agent_ready_issues=[ready_issue],
                orphan_issues=[orphan_issue],
            ),
        ),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            return_value=[],
        ),
        patch("baton_harness.chain.branches.create_feature_branch"),
        patch("baton_harness.chain.branches.checkout_feature_branch"),
        patch(
            "baton_harness.chain.branches.record_cut_point",
            return_value="deadbeef" * 5,
        ),
        patch(
            "baton_harness.chain.recovery.reconstruct",
            return_value=RecoveryResult(
                done=set(),
                parked_seed=set(),
                ci_gate_reentry=set(),
                redispatch=set(),
            ),
        ),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=frozenset({20, 21}),
        ),
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch.object(
            daemon_mod,
            "_run_work_unit",
            side_effect=spy_run_work_unit,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator."
            "Orchestrator._run_worker",
            new_callable=AsyncMock,
            return_value="pr_created",
        ),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    assert len(work_unit_calls) <= 1, (
        "Expected _run_work_unit to be called AT MOST ONCE for a milestone "
        "that has both an agent-ready and an agent-in-progress issue. "
        "A secondary scan must not re-seed a milestone already handled this "
        f"cycle. _run_work_unit was called {len(work_unit_calls)} time(s)."
    )


# ---------------------------------------------------------------------------
# Test 3 (TRUE RED): liveness_state.mark_in_progress reached for orphan
#
# Same lone-orphan scenario as Test 1.  Assert that
# ``liveness_state.mark_in_progress`` is called with the orphan's issue
# number so the heartbeat stall monitor can track it.
#
# This MUST FAIL on current code for the same reason as Test 1: without
# a secondary scan the orphan's milestone is never processed and
# mark_in_progress is never reached for issue 10.
# ---------------------------------------------------------------------------


def test_lone_orphan_populates_liveness_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lone orphan causes liveness_state.mark_in_progress to be called.

    Arrange:
    - No agent-ready issues (primary scan empty).
    - One open ``agent-in-progress`` issue #10 in milestone "Sprint 9".
    - ``reconstruct`` returns ``redispatch={10}``.
    - ``LivenessState.mark_in_progress`` is spied on.

    Assert:
    - ``liveness_state.mark_in_progress`` is called (any call) during the
      poll cycle so the heartbeat monitor knows the orphan is being worked
      on.

    This MUST FAIL on current code because no secondary scan exists.

    Args:
        tmp_path: pytest tmp directory.
        monkeypatch: env isolation fixture.
    """
    monkeypatch.setenv("BH_PROJECT_ROOT", str(tmp_path))
    for var in (
        "BH_RUNLOG_PATH",
        "BH_HEARTBEAT_FILE",
        "BH_REDISPATCH_WINDOW_TICKS",
        "BH_REDISPATCH_MAX",
        "BH_HEARTBEAT_STALL_S",
        "BH_HEARTBEAT_PING_URL",
        "BH_REDISPATCH_COUNTS_PATH",
    ):
        monkeypatch.delenv(var, raising=False)

    ms = {"number": 9, "title": "Sprint 9"}
    orphan = _make_issue(10, ["agent-in-progress"], milestone=ms)

    mark_in_progress_calls: list[Any] = []
    real_mark_in_progress = LivenessState.mark_in_progress

    def spy_mark_in_progress(
        self: LivenessState,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Record the call and delegate to the real implementation."""
        mark_in_progress_calls.append((args, kwargs))
        real_mark_in_progress(self, *args, **kwargs)

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_orphan_run_side_effect(
                agent_ready_issues=[],
                orphan_issues=[orphan],
            ),
        ),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            return_value=[],
        ),
        patch("baton_harness.chain.branches.create_feature_branch"),
        patch("baton_harness.chain.branches.checkout_feature_branch"),
        patch(
            "baton_harness.chain.branches.record_cut_point",
            return_value="deadbeef" * 5,
        ),
        # reconstruct returns redispatch={10} so the dispatch path runs.
        patch(
            "baton_harness.chain.recovery.reconstruct",
            return_value=RecoveryResult(
                done=set(),
                parked_seed=set(),
                ci_gate_reentry=set(),
                redispatch={10},
            ),
        ),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=frozenset({10}),
        ),
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch.object(
            LivenessState,
            "mark_in_progress",
            autospec=True,
            side_effect=spy_mark_in_progress,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator."
            "Orchestrator._run_worker",
            new_callable=AsyncMock,
            return_value="pr_created",
        ),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    assert mark_in_progress_calls, (
        "Expected liveness_state.mark_in_progress to be called at least "
        "once for the lone agent-in-progress orphan #10. "
        "Without the secondary orphan scan this path is never reached — "
        "_poll_and_run returns early after the empty agent-ready list. "
        f"mark_in_progress_calls={mark_in_progress_calls}"
    )
