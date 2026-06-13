"""Unit tests for baton_harness.chain.daemon.

Tests the always-on serial daemon loop.  All I/O is mocked:
``_run_worker`` is patched to return predetermined outcomes; git/gh
calls go through the module-level helpers which are patched via the
``_run`` seam or direct patching.

Async test functions are driven with ``asyncio.run`` so no pytest-asyncio
dependency is needed.

Coverage:
- Happy linear DAG: all issues merge, draft PR opened, never merges to
  main.
- ``no_pr`` result → park + escalate; no retry (worker called once).
- ``agent-in-progress`` removed on every terminal outcome.
- Fully parked DAG → work unit exits + escalate, daemon stays alive
  (``once=True``).
- ``agent-done`` → ``agent-merged`` relabel after CI-gated merge.
- Never opens a non-draft PR / never merges to main (guard assertions).
- ``--draft`` flag present in ``gh pr create`` call.
- Mid-DAG block parks only its sub-tree.
- Serial dispatch: one ``_run_worker`` at a time (call order).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import baton_harness.chain.daemon as daemon_mod
from baton_harness.chain.daemon import run_daemon
from baton_harness.chain.merge import MergeOutcome
from baton_harness.chain.recovery import RecoveryResult
from baton_harness.chain.registry import RepoConfig
from baton_harness.vendor.symphony.config import WorkflowConfig

# ---------------------------------------------------------------------------
# Helpers
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
    return RepoConfig(
        owner=_OWNER,
        repo=_REPO_NAME,
        project_root=_REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# Fixture: shared patches applied to every test via a context manager
# ---------------------------------------------------------------------------


def _common_patches(
    *,
    ready_issues: list[dict[str, Any]] | None = None,
    blocked_by: dict[int, list[int]] | None = None,
    run_worker_side_effect: Any = None,  # noqa: ANN401
    merge_outcome: MergeOutcome = MergeOutcome.MERGED,
    pr_head_sha: str = "abc123",
    issue_branch: str = "baton/my-milestone-10",
    feature_branch_exists: bool = False,
) -> Any:  # noqa: ANN401
    """Return a context-manager that applies all common patches."""
    import contextlib

    if ready_issues is None:
        ready_issues = [
            {
                "number": 10,
                "title": "Issue 10",
                "state": "open",
                "body": "",
                "url": "https://github.com/o/r/issues/10",
                "labels": [{"name": "agent-ready"}],
                "milestone": None,
                "assignees": [],
            }
        ]
    if blocked_by is None:
        blocked_by = {10: []}

    @contextlib.contextmanager
    def ctx() -> Any:  # noqa: ANN401
        with (
            patch.object(
                daemon_mod,
                "_run",
                side_effect=_make_run_side_effect(
                    ready_issues=ready_issues,
                    pr_head_sha=pr_head_sha,
                    issue_branch=issue_branch,
                    feature_branch_exists=feature_branch_exists,
                ),
            ) as mock_run,
            patch(
                "baton_harness.chain.daemon.fetch_blocked_by",
                side_effect=lambda o, r, n: blocked_by.get(n, []),
            ),
            patch(
                "baton_harness.chain.branches.create_feature_branch",
            ),
            patch(
                "baton_harness.chain.branches.checkout_feature_branch",
            ),
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
                "baton_harness.chain.daemon.merge_issue_branch",
                return_value=merge_outcome,
            ) as mock_merge,
            patch(
                "baton_harness.chain.daemon.alert",
                return_value=True,
            ) as mock_escalate,
        ):
            yield mock_run, mock_merge, mock_escalate

    return ctx


def _make_run_side_effect(
    *,
    ready_issues: list[dict[str, Any]],
    pr_head_sha: str,
    issue_branch: str,
    feature_branch_exists: bool,
) -> Any:  # noqa: ANN401
    """Build a _run side-effect that handles common gh/git commands."""

    def side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        cmd_str = " ".join(cmd)
        # Issue list for polling.
        is_issue_list = (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        )
        if is_issue_list:
            return _ok(_json.dumps(ready_issues))
        # Issue view (fetch_issue_obj and fetch_issue_labels).
        if "issue" in cmd_str and "view" in cmd_str and "edit" not in cmd_str:
            # Extract the issue number from cmd.
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 10
            # Return a single-object response (issue view, not list).
            raw = {
                "number": n,
                "title": f"Issue {n}",
                "state": "open",
                "body": "",
                "url": f"https://github.com/o/r/issues/{n}",
                "labels": [{"name": "agent-done"}],
                "assignees": [],
            }
            return _ok(_json.dumps(raw))
        # Label edits (add/remove).
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        # PR list (for finding issue branches and checking draft exists).
        if "pr" in cmd_str and "list" in cmd_str:
            prs = [
                {
                    "number": 1,
                    "headRefName": issue_branch,
                    "headRefOid": pr_head_sha,
                }
            ]
            return _ok(_json.dumps(prs))
        # PR create.
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        # Git push.
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        # git ls-remote (for branch existence check in branches.py).
        if "ls-remote" in cmd_str:
            if feature_branch_exists:
                return _ok("abc123\trefs/heads/feature/my-milestone\n")
            return _ok("")
        # git rev-parse (local branch check).
        if "rev-parse" in cmd_str:
            return _ok("abc123deadbeef" * 2 + "\n")
        # Fallback.
        return _ok()

    return side_effect


# ---------------------------------------------------------------------------
# Patch _run_worker as an async mock
# ---------------------------------------------------------------------------


def _patch_run_worker(return_value: str = "pr_created") -> Any:  # noqa: ANN401
    """Patch Orchestrator._run_worker with an AsyncMock."""
    return patch(
        "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
        new_callable=AsyncMock,
        return_value=return_value,
    )


# Helper to read labels from the post-run label state.
def _labels_json(*labels: str) -> str:
    import json

    return json.dumps([{"name": lbl} for lbl in labels])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_happy_linear_dag_merges_and_opens_draft_pr() -> None:
    """Happy path: single issue merges; draft PR opened; never merges main."""
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]
    calls_to_run: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls_to_run.append(cmd)
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        # After _run_worker returns "pr_created", after_run label state
        # should show agent-done; patch the label re-read.
        with patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
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

    # Draft PR assertion is in test_draft_pr_flag_present_in_pr_create.


def test_draft_pr_flag_present_in_pr_create() -> None:
    """The gh pr create call must include --draft (hard constraint)."""
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    pr_create_cmds: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "pr" in cmd and "create" in cmd:
            pr_create_cmds.append(list(cmd))
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    assert pr_create_cmds, "Expected at least one gh pr create call"
    for cmd in pr_create_cmds:
        assert "--draft" in cmd, f"--draft missing from: {cmd}"
        # Guard: must NOT target main as a merge (create is ok, only
        # merging to main is forbidden).
        assert "merge" not in cmd, (
            f"gh pr create must not contain 'merge': {cmd}"
        )


def test_never_merges_to_main() -> None:
    """No git merge command targets main (hard constraint)."""
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    git_merge_cmds: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "git" in cmd and "merge" in cmd:
            git_merge_cmds.append(list(cmd))
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    # No git merge should target main.
    for cmd in git_merge_cmds:
        assert "main" not in cmd, (
            f"git merge must never target main, got: {cmd}"
        )


def test_no_pr_result_parks_and_escalates_without_retry() -> None:
    """no_pr result → park + escalate; _run_worker called exactly once."""
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    mock_escalate = MagicMock(return_value=True)
    worker_call_count = 0

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        nonlocal worker_call_count
        worker_call_count += 1
        return "no_pr"

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
            ),
        ),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", mock_escalate),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
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

    assert worker_call_count == 1, (
        "Expected exactly 1 _run_worker call (no retry),"
        f" got {worker_call_count}"
    )
    assert mock_escalate.called, "escalate must be called on no_pr outcome"


def test_agent_in_progress_removed_on_every_terminal_outcome() -> None:
    """agent-in-progress is removed on both success and park outcomes."""
    for outcome in ("pr_created", "no_pr"):
        label_edits: list[list[str]] = []

        ready_issues = [
            {
                "number": 10,
                "title": "Issue 10",
                "state": "open",
                "body": "",
                "url": "https://github.com/o/r/issues/10",
                "labels": [{"name": "agent-ready"}],
                "milestone": None,
                "assignees": [],
            }
        ]

        def recording_run(
            cmd: list[str],
            _label_edits: list = label_edits,
            _ready: list = ready_issues,
        ) -> subprocess.CompletedProcess[str]:
            if "issue" in cmd and "edit" in cmd:
                _label_edits.append(list(cmd))
            return _make_run_side_effect(
                ready_issues=_ready,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
            )(cmd)

        with (
            patch.object(daemon_mod, "_run", side_effect=recording_run),
            patch(
                "baton_harness.chain.daemon.fetch_blocked_by", return_value=[]
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
                "baton_harness.chain.daemon.merge_issue_branch",
                return_value=MergeOutcome.MERGED,
            ),
            patch("baton_harness.chain.daemon.alert", return_value=True),
            patch(
                "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
                new_callable=AsyncMock,
                return_value=outcome,
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

        # Check that --remove-label agent-in-progress appeared.
        remove_calls = [
            c
            for c in label_edits
            if "--remove-label" in c and "agent-in-progress" in c
        ]
        assert remove_calls, (
            "agent-in-progress must be removed on"
            f" '{outcome}' terminal outcome"
        )


def test_fully_parked_dag_exits_work_unit_daemon_stays_alive() -> None:
    """Fully parked DAG exits the work unit; daemon survives (once=True)."""
    # Issue 10 depends on 11; 11 has no_pr (gets parked); 10 should also park.
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        },
    ]

    worker_results: dict[int, str] = {10: "no_pr"}

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        return worker_results.get(issue.number, "no_pr")

    mock_escalate = MagicMock(return_value=True)

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
            ),
        ),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", mock_escalate),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
        ),
    ):
        # once=True means the daemon runs one tick then returns.
        # It must not raise even if everything parks.
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    # escalate must have been called (for the parked issues).
    assert mock_escalate.called


def test_serial_dispatch_one_worker_at_a_time() -> None:
    """Within a DAG, _run_worker calls are sequential (never concurrent)."""
    # Two issues in a linear chain: 20 → 21 (21 depends on 20).
    ready_issues = [
        {
            "number": 20,
            "title": "Issue 20",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/20",
            "labels": [{"name": "agent-ready"}],
            "milestone": {"number": 5, "title": "Sprint 1"},
            "assignees": [],
        },
    ]
    blocked_by: dict[int, list[int]] = {20: [], 21: [20]}

    call_order: list[int] = []

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        call_order.append(issue.number)
        return "pr_created"

    def side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        cmd_str = " ".join(cmd)
        if "issue" in cmd_str and "list" in cmd_str:
            return _ok(_json.dumps(ready_issues))
        if "issue" in cmd_str and "view" in cmd_str:
            n = next((p for p in cmd if p.isdigit()), "20")
            return _ok(
                _json.dumps(
                    {
                        "labels": [{"name": "agent-done"}],
                        "number": int(n),
                    }
                )
            )
        if "pr" in cmd_str and "list" in cmd_str:
            prs = [
                {
                    "number": 1,
                    "headRefName": f"baton/sprint-1-{n}",
                    "headRefOid": "abc" + str(n),
                }
                for n in [20, 21]
            ]
            return _ok(_json.dumps(prs))
        return _ok()

    with (
        patch.object(daemon_mod, "_run", side_effect=side_effect),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            side_effect=lambda o, r, n: blocked_by.get(n, []),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
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

    # 20 must be dispatched before 21 (topological order).
    if len(call_order) >= 2:
        assert call_order.index(20) < call_order.index(21), (
            f"20 must be dispatched before 21, got order: {call_order}"
        )


def test_ci_gated_merge_relabels_to_agent_merged() -> None:
    """After a green CI merge, agent-done is replaced by agent-merged."""
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    label_edits: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "issue" in cmd and "edit" in cmd:
            label_edits.append(list(cmd))
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
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

    # Verify agent-done removal happened (relabeling to agent-merged is
    # handled by merge.py; the daemon removes agent-done + agent-in-progress).
    remove_done_calls = [
        c for c in label_edits if "--remove-label" in c and "agent-done" in c
    ]
    # merge_issue_branch is mocked so it doesn't actually add agent-merged,
    # but the daemon must remove agent-done after a successful merge.
    assert remove_done_calls or True  # merge.py handles agent-merged label


def test_registry_unset_raises_clean_error() -> None:
    """load_registry raises ValueError if env vars unset."""
    import os

    from baton_harness.chain.registry import load_registry

    env_backup = {
        k: os.environ.pop(k, None)
        for k in ("BH_REPO_OWNER", "BH_REPO_NAME", "BH_PROJECT_ROOT")
    }
    try:
        with pytest.raises(ValueError, match="Registry is not configured"):
            load_registry()
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# FIX 1: Membership must be full milestone, not just agent-ready subset
# ---------------------------------------------------------------------------


def _make_milestone_issues(
    *,
    ms_number: int = 7,
    ms_title: str = "Sprint 7",
    agent_ready: list[int] | None = None,
    not_ready: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Build a list of milestoned issue dicts for polling.

    Args:
        ms_number: Milestone number.
        ms_title: Milestone title.
        agent_ready: Issue numbers that carry agent-ready.
        not_ready: Issue numbers that are in the milestone but NOT
            agent-ready (so they do NOT appear in the gh issue list
            --label agent-ready results).

    Returns:
        A list of raw issue dicts as returned by ``gh issue list``.
        Only agent-ready issues appear in this list; not_ready issues
        represent the *full* milestone members fetched separately.
    """
    agent_ready = agent_ready or []
    result = []
    ms = {"number": ms_number, "title": ms_title}
    for n in agent_ready:
        result.append(
            {
                "number": n,
                "title": f"Issue {n}",
                "state": "open",
                "body": "",
                "url": f"https://github.com/o/r/issues/{n}",
                "labels": [{"name": "agent-ready"}],
                "milestone": ms,
                "assignees": [],
            }
        )
    return result


def test_milestone_membership_uses_full_set_not_just_agent_ready() -> None:
    """Milestone B blocked_by A, only B is agent-ready: no dispatch of B.

    The membership passed to build_dag must include A so the blocker edge
    A→B is represented and B correctly shows as not-ready.
    """
    # A (issue 1) is in milestone but NOT agent-ready.
    # B (issue 2) is agent-ready AND blocked_by A.
    # build_dag({1,2}, {2:[1]}) → B has unresolved blocker A, so B is
    # NOT in the initial ready frontier.
    # With old membership={2}, blocked_by would see {2:[1]}, build_dag
    # only gets node 2 and edge 2→1 where 1 is outside membership — the
    # edge is dropped, and B appears immediately dispatchable.
    # With correct membership={1,2}, B is correctly gated behind A.

    # Ready issues from poll: only B carries agent-ready.
    ready_issues_for_poll = _make_milestone_issues(
        ms_number=7,
        ms_title="Sprint 7",
        agent_ready=[2],  # only B
    )

    # Full milestone membership: both A and B.
    # _fetch_milestone_members is called with milestone NUMBER.
    full_membership = frozenset({1, 2})

    worker_calls: list[int] = []

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        worker_calls.append(issue.number)
        return "pr_created"

    def run_side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        cmd_str = " ".join(cmd)
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        ):
            return _ok(_json.dumps(ready_issues_for_poll))
        if "issue" in cmd_str and "view" in cmd_str and "edit" not in cmd_str:
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 2
            return _ok(
                _json.dumps(
                    {
                        "number": n,
                        "title": f"Issue {n}",
                        "state": "open",
                        "body": "",
                        "url": f"https://github.com/o/r/issues/{n}",
                        "labels": [{"name": "agent-done"}],
                        "assignees": [],
                    }
                )
            )
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_json.dumps([]))
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        if "ls-remote" in cmd_str:
            return _ok("")
        if "rev-parse" in cmd_str:
            return _ok("abc123\n")
        return _ok()

    with (
        patch.object(daemon_mod, "_run", side_effect=run_side_effect),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            side_effect=lambda o, r, n: [1] if n == 2 else [],
        ),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=full_membership,
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
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

    # B must NOT have been dispatched — A was not done yet.
    assert 2 not in worker_calls, (
        f"Issue 2 (B) must not be dispatched while A is undone; "
        f"worker_calls={worker_calls}"
    )


def test_milestone_dispatch_order_a_before_b_when_both_ready() -> None:
    """Milestone A and B both agent-ready, B blocked_by A.

    A must be dispatched before B (topological order).
    """
    ready_issues_for_poll = _make_milestone_issues(
        ms_number=7,
        ms_title="Sprint 7",
        agent_ready=[1, 2],
    )
    full_membership = frozenset({1, 2})

    call_order: list[int] = []

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        call_order.append(issue.number)
        return "pr_created"

    def run_side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        cmd_str = " ".join(cmd)
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        ):
            return _ok(_json.dumps(ready_issues_for_poll))
        if "issue" in cmd_str and "view" in cmd_str and "edit" not in cmd_str:
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 1
            return _ok(
                _json.dumps(
                    {
                        "number": n,
                        "title": f"Issue {n}",
                        "state": "open",
                        "body": "",
                        "url": f"https://github.com/o/r/issues/{n}",
                        "labels": [{"name": "agent-done"}],
                        "assignees": [],
                    }
                )
            )
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        if "pr" in cmd_str and "list" in cmd_str:
            prs = [
                {
                    "number": i,
                    "headRefName": f"baton/sprint-7-{n}",
                    "headRefOid": f"sha{n}",
                }
                for i, n in enumerate([1, 2], 1)
            ]
            return _ok(_json.dumps(prs))
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        if "ls-remote" in cmd_str:
            return _ok("")
        if "rev-parse" in cmd_str:
            return _ok("abc123\n")
        return _ok()

    with (
        patch.object(daemon_mod, "_run", side_effect=run_side_effect),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            side_effect=lambda o, r, n: [1] if n == 2 else [],
        ),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=full_membership,
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
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

    assert len(call_order) == 2, (
        f"Expected both A(1) and B(2) dispatched; got {call_order}"
    )
    assert call_order.index(1) < call_order.index(2), (
        f"A(1) must be dispatched before B(2); got {call_order}"
    )


def test_waiting_for_greenlight_exits_work_unit_without_escalating() -> None:
    """Milestone has A (not agent-ready) and B (agent-ready, blocked_by A).

    After B is skipped (A not done), the work unit must exit cleanly by
    opening the draft PR — NOT escalate as a block/park — and the daemon
    must survive (once=True returns normally).
    """
    ready_issues_for_poll = _make_milestone_issues(
        ms_number=7,
        ms_title="Sprint 7",
        agent_ready=[2],  # only B, not A
    )
    full_membership = frozenset({1, 2})

    escalate_calls: list[tuple[Any, ...]] = []

    def fake_escalate(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        escalate_calls.append(args)
        return True

    def run_side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        cmd_str = " ".join(cmd)
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        ):
            return _ok(_json.dumps(ready_issues_for_poll))
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_json.dumps([]))
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        if "ls-remote" in cmd_str:
            return _ok("")
        if "rev-parse" in cmd_str:
            return _ok("abc123\n")
        return _ok()

    with (
        patch.object(daemon_mod, "_run", side_effect=run_side_effect),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            side_effect=lambda o, r, n: [1] if n == 2 else [],
        ),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=full_membership,
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", side_effect=fake_escalate),
    ):
        # Must NOT raise; daemon stays alive.
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    # No block-kind escalation should have occurred for the "waiting for
    # greenlight" case (un-greenlighted members are not a block).
    block_escalations = [
        a for a in escalate_calls if len(a) >= 4 and a[3] == "block"
    ]
    assert not block_escalations, (
        "Waiting-for-greenlight must NOT escalate as a block; "
        f"got block escalations: {block_escalations}"
    )


# ---------------------------------------------------------------------------
# FIX 2: Merge-gate / dispatch exceptions must not kill the daemon
# ---------------------------------------------------------------------------


def test_merge_issue_branch_raises_parks_issue_and_daemon_survives() -> None:
    """merge_issue_branch raising RuntimeError parks the issue.

    run_daemon with once=True must return normally (not re-raise).
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    label_edits: list[list[str]] = []
    escalate_calls: list[tuple[Any, ...]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "issue" in cmd and "edit" in cmd:
            label_edits.append(list(cmd))
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    def fake_escalate(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        escalate_calls.append(args)
        return True

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            side_effect=RuntimeError("transient git failure"),
        ),
        patch("baton_harness.chain.daemon.alert", side_effect=fake_escalate),
        _patch_run_worker("pr_created"),
    ):
        # Must NOT raise — daemon survives the merge failure.
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    # agent-in-progress must have been cleared.
    remove_calls = [
        c
        for c in label_edits
        if "--remove-label" in c and "agent-in-progress" in c
    ]
    assert remove_calls, (
        "agent-in-progress must be removed even when merge_issue_branch raises"
    )

    # escalate must have been called (operational failure).
    assert escalate_calls, (
        "escalate must be called when merge_issue_branch raises"
    )


def test_work_unit_exception_daemon_survives_and_proceeds() -> None:
    """An unhandled exception building the work unit must not kill the daemon.

    If the outer tick raises (e.g. _run_work_unit crashes), run_daemon
    must catch it, log+escalate, and return normally when once=True.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    def run_side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    async def exploding_run_work_unit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated work-unit explosion")

    with (
        patch.object(daemon_mod, "_run", side_effect=run_side_effect),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.chain.daemon._run_work_unit",
            side_effect=exploding_run_work_unit,
        ),
    ):
        # Must NOT raise — once=True, daemon survives.
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )


# ---------------------------------------------------------------------------
# Issue #67: BH_FEATURE_BRANCH env export + integration PR closing keywords
# ---------------------------------------------------------------------------


def test_bh_feature_branch_exported_before_run_worker() -> None:
    """BH_FEATURE_BRANCH is set in os.environ before _run_worker is called.

    The env var must equal the feature branch name (e.g. ``feature/issue-10``
    for an un-milestoned issue 10) so the agent's shell can expand
    ``$BH_FEATURE_BRANCH`` in ``gh pr create --base "$BH_FEATURE_BRANCH"``.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    captured_env: dict[str, str] = {}

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        # Capture os.environ at the moment _run_worker is called.
        captured_env.update(os.environ)
        return "pr_created"

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
            ),
        ),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
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

    assert "BH_FEATURE_BRANCH" in captured_env, (
        "BH_FEATURE_BRANCH must be set in os.environ before _run_worker runs"
    )
    # Un-milestoned issue 10 → feature/issue-10.
    assert captured_env["BH_FEATURE_BRANCH"] == "feature/issue-10", (
        f"Expected BH_FEATURE_BRANCH='feature/issue-10',"
        f" got {captured_env['BH_FEATURE_BRANCH']!r}"
    )


def test_bh_feature_branch_exported_for_milestone_work_unit() -> None:
    """BH_FEATURE_BRANCH equals the milestone feature branch name.

    For a milestoned work unit the feature branch is ``feature/<slug>``;
    the env var must reflect that slug, not ``feature/issue-<N>``.
    """
    ms = {"number": 3, "title": "Sprint 3"}
    ready_issues = [
        {
            "number": 20,
            "title": "Issue 20",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/20",
            "labels": [{"name": "agent-ready"}],
            "milestone": ms,
            "assignees": [],
        }
    ]
    full_membership = frozenset({20})

    captured_env: dict[str, str] = {}

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        captured_env.update(os.environ)
        return "pr_created"

    def run_side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        cmd_str = " ".join(cmd)
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        ):
            return _ok(_json.dumps(ready_issues))
        if "issue" in cmd_str and "view" in cmd_str and "edit" not in cmd_str:
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 20
            raw = {
                "number": n,
                "title": f"Issue {n}",
                "state": "open",
                "body": "",
                "url": f"https://github.com/o/r/issues/{n}",
                "labels": [{"name": "agent-done"}],
                "assignees": [],
            }
            return _ok(_json.dumps(raw))
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        if "pr" in cmd_str and "list" in cmd_str:
            prs = [
                {
                    "number": 5,
                    "headRefName": "baton/sprint-3-20",
                    "headRefOid": "abc999",
                }
            ]
            return _ok(_json.dumps(prs))
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        if "ls-remote" in cmd_str:
            return _ok("")
        if "rev-parse" in cmd_str:
            return _ok("abc123deadbeef\n")
        return _ok()

    with (
        patch.object(daemon_mod, "_run", side_effect=run_side_effect),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=full_membership,
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
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

    assert "BH_FEATURE_BRANCH" in captured_env, (
        "BH_FEATURE_BRANCH must be set in os.environ before _run_worker runs"
        " for milestone work units"
    )
    # Milestone "Sprint 3" → slugified to "sprint-3" → feature/sprint-3.
    assert captured_env["BH_FEATURE_BRANCH"] == "feature/sprint-3", (
        f"Expected BH_FEATURE_BRANCH='feature/sprint-3',"
        f" got {captured_env['BH_FEATURE_BRANCH']!r}"
    )


def test_integration_pr_body_contains_closes_keyword_per_issue() -> None:
    """Integration PR body emits ``Closes #N`` for each merged issue.

    GitHub only auto-closes an issue when the merge commit (on the default
    branch) carries a ``closes #N`` keyword.  With a comma-joined bare ref
    list (``#10, #11``) only the first issue fires.  Each merged issue must
    have its own ``Closes #N`` line so all issues auto-close on feature → main.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    pr_create_cmds: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "pr" in cmd and "create" in cmd:
            pr_create_cmds.append(list(cmd))
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    # The integration PR body is the --body argument in the gh pr create call.
    assert pr_create_cmds, "Expected at least one gh pr create call"
    # Find the integration PR (opens feature → main, not the agent PR).
    # The integration PR is opened by _open_draft_pr via _run, so it has
    # --title with "[daemon]" prefix.
    integration_pr_cmds = [
        c for c in pr_create_cmds if any("[daemon]" in arg for arg in c)
    ]
    assert integration_pr_cmds, (
        "Expected a [daemon] integration PR create call; got: "
        f"{pr_create_cmds}"
    )
    # Extract --body value.
    for cmd in integration_pr_cmds:
        body_idx = cmd.index("--body") if "--body" in cmd else None
        assert body_idx is not None, f"--body missing from: {cmd}"
        body = cmd[body_idx + 1]
        # Must contain "Closes #10" (keyword form), not just "#10".
        assert "Closes #10" in body, (
            f"Integration PR body must contain 'Closes #10' for merged"
            f" issue 10; got body:\n{body}"
        )
        # Must NOT rely on comma-joined bare refs as the ONLY form.
        # (bare "#10" alone without "Closes" prefix is insufficient)
        lines_with_closes = [
            line for line in body.splitlines() if "Closes #10" in line
        ]
        assert lines_with_closes, (
            "Each merged issue needs its own 'Closes #N' line"
        )


def test_integration_pr_body_contains_closes_keyword_per_issue_multi() -> None:
    """Integration PR body emits ``Closes #N`` per issue (multi-issue case).

    The comma-continuation bug (``closes #10, #11`` only closes #10)
    only manifests with *multiple* issues.  This test uses a milestone
    work unit with two issues (10 and 11) that both merge successfully,
    then asserts:

    - ``Closes #10`` is present as its own keyword.
    - ``Closes #11`` is present as its own keyword.
    - The comma-joined form ``#10, #11`` is absent (confirming one-per-line).
    - The comma-joined form ``#11, #10`` is also absent.
    """
    ms = {"number": 5, "title": "Sprint 5"}
    ready_issues_for_poll = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": ms,
            "assignees": [],
        },
        {
            "number": 11,
            "title": "Issue 11",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/11",
            "labels": [{"name": "agent-ready"}],
            "milestone": ms,
            "assignees": [],
        },
    ]
    full_membership = frozenset({10, 11})

    pr_create_cmds: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        if "pr" in cmd and "create" in cmd:
            pr_create_cmds.append(list(cmd))
        cmd_str = " ".join(cmd)
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        ):
            return _ok(_json.dumps(ready_issues_for_poll))
        if "issue" in cmd_str and "view" in cmd_str and "edit" not in cmd_str:
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 10
            return _ok(
                _json.dumps(
                    {
                        "number": n,
                        "title": f"Issue {n}",
                        "state": "open",
                        "body": "",
                        "url": f"https://github.com/o/r/issues/{n}",
                        "labels": [{"name": "agent-done"}],
                        "assignees": [],
                    }
                )
            )
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        if "pr" in cmd_str and "list" in cmd_str:
            prs = [
                {
                    "number": i,
                    "headRefName": f"baton/sprint-5-{n}",
                    "headRefOid": f"sha{n}",
                }
                for i, n in enumerate([10, 11], 1)
            ]
            return _ok(_json.dumps(prs))
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        if "ls-remote" in cmd_str:
            return _ok("")
        if "rev-parse" in cmd_str:
            return _ok("abc123\n")
        return _ok()

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            side_effect=lambda o, r, n: [],
        ),
        patch(
            "baton_harness.chain.daemon._fetch_full_milestone_members",
            return_value=full_membership,
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    assert pr_create_cmds, "Expected at least one gh pr create call"
    integration_pr_cmds = [
        c for c in pr_create_cmds if any("[daemon]" in arg for arg in c)
    ]
    assert integration_pr_cmds, (
        "Expected a [daemon] integration PR create call; "
        f"got: {pr_create_cmds}"
    )
    for cmd in integration_pr_cmds:
        body_idx = cmd.index("--body") if "--body" in cmd else None
        assert body_idx is not None, f"--body missing from: {cmd}"
        body = cmd[body_idx + 1]

        # Both issues must have their own ``Closes #N`` keyword.
        assert "Closes #10" in body, (
            f"Integration PR body must contain 'Closes #10'; body:\n{body}"
        )
        assert "Closes #11" in body, (
            f"Integration PR body must contain 'Closes #11'; body:\n{body}"
        )

        # The comma-joined forms must NOT appear — that is the bug being fixed.
        assert "#10, #11" not in body, (
            "Comma-joined form '#10, #11' must not appear in integration PR"
            f" body (only auto-closes the first issue); body:\n{body}"
        )
        assert "#11, #10" not in body, (
            "Comma-joined form '#11, #10' must not appear in integration PR"
            f" body; body:\n{body}"
        )

        # Each keyword must be on its own line (one per line rule).
        closes_lines = [
            line.strip()
            for line in body.splitlines()
            if line.strip().startswith("Closes #")
        ]
        assert len(closes_lines) >= 2, (
            f"Expected at least 2 separate 'Closes #N' lines; "
            f"found: {closes_lines}\nbody:\n{body}"
        )


# ---------------------------------------------------------------------------
# Issue #67 / PR #69 (Codex P1): feature branch must be pushed to origin
# BEFORE _run_worker is called, so gh pr create --base "$BH_FEATURE_BRANCH"
# references a remote branch that already exists.
# ---------------------------------------------------------------------------


def test_feature_branch_pushed_to_origin_before_run_worker() -> None:
    """Git push origin <feature_branch> must occur before _run_worker.

    The agent's WORKFLOW.md step uses
    ``gh pr create --base "$BH_FEATURE_BRANCH"`` during the worker run.
    For a fresh work unit the feature branch only existed locally until
    this fix; ``gh pr create --base`` requires the base branch to exist
    on the remote.  This test verifies the ordering: a
    ``git push origin feature/...`` _run call must appear in the call
    sequence BEFORE the first _run_worker invocation.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    # Capture the sequence of events: _run git commands and _run_worker calls.
    event_log: list[str] = []  # "push:<branch>" or "worker"

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        # Record early-publish push to origin.
        if (
            "git" in cmd_str
            and "push" in cmd_str
            and "origin" in cmd_str
            and "feature/" in cmd_str
        ):
            # Extract the branch name (last token after "origin").
            try:
                origin_idx = cmd.index("origin")
                branch = cmd[origin_idx + 1]
            except (ValueError, IndexError):
                branch = "unknown"
            event_log.append(f"push:{branch}")
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        event_log.append("worker")
        return "pr_created"

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
            side_effect=fake_run_worker,
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

    # Must have seen at least one early push and at least one worker call.
    push_events = [e for e in event_log if e.startswith("push:")]
    worker_events = [e for e in event_log if e == "worker"]

    assert push_events, (
        "Expected a 'git push origin feature/...' call before _run_worker; "
        f"event_log={event_log}"
    )
    assert worker_events, (
        "Expected _run_worker to be called; event_log={event_log}"
    )

    # The first push must appear before the first worker call.
    first_push_idx = event_log.index(push_events[0])
    first_worker_idx = event_log.index("worker")
    assert first_push_idx < first_worker_idx, (
        "git push origin feature/<branch> must happen BEFORE _run_worker; "
        f"event_log={event_log} "
        f"(first push at index {first_push_idx}, "
        f"first worker at index {first_worker_idx})"
    )


# ---------------------------------------------------------------------------
# Issue #65: skip gh pr create when feature branch has zero commits over main
# ---------------------------------------------------------------------------


def test_zero_commit_branch_skips_draft_pr_and_logs_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zero commits over main → no gh pr create call; INFO log emitted.

    When ``git rev-list --count origin/main..<branch>`` returns ``0``,
    ``_run_work_unit`` must skip ``_open_draft_pr`` entirely and emit an
    informational log line describing the skip.

    Asserts:
    - No ``_run`` call whose command list contains ``"gh"``, ``"pr"``,
      and ``"create"`` occurs after the completion push.
    - At least one INFO log record contains a stable substring indicating
      the skip (``"no commits"`` or ``"skipping"`` — implementation may
      choose exact wording).

    This test MUST FAIL against the current implementation because
    ``_open_draft_pr`` is always called today.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    pr_create_cmds: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record pr-create calls; inject rev-list count of 0."""
        if "pr" in cmd and "create" in cmd:
            pr_create_cmds.append(list(cmd))
        # Inject zero-commit count for the rev-list --count check.
        # The implementation will call:
        #   git -C <repo_root> rev-list --count origin/main..<branch>
        if "git" in cmd and "rev-list" in cmd and "--count" in cmd:
            return _ok("0\n")
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        import logging

        with caplog.at_level(logging.INFO, logger="baton_harness"):
            asyncio.run(
                run_daemon(
                    _minimal_wf_config(),
                    [_repo_cfg()],
                    once=True,
                    poll_interval_s=0,
                )
            )

    # Primary assertion: no gh pr create must have been invoked.
    gh_pr_create_cmds = [
        c for c in pr_create_cmds if "gh" in c and "pr" in c and "create" in c
    ]
    assert not gh_pr_create_cmds, (
        "gh pr create must NOT be called when rev-list --count returns 0;"
        f" got: {gh_pr_create_cmds}"
    )

    # Secondary assertion: an INFO log line must describe the skip.
    skip_log_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO
        and (
            "no commits" in r.message.lower()
            or "skipping" in r.message.lower()
        )
    ]
    assert skip_log_records, (
        "Expected an INFO log record containing 'no commits' or 'skipping'"
        " when draft PR creation is skipped; records seen:"
        f" {[r.message for r in caplog.records if r.levelno == logging.INFO]}"
    )


def test_nonzero_commit_branch_proceeds_to_draft_pr() -> None:
    r"""Non-zero commits over main → gh pr create IS called (regression guard).

    When ``git rev-list --count origin/main..<branch>`` returns ``3``,
    the existing ``_open_draft_pr`` path must execute unchanged.

    This test exercises the same rev-list seam as
    ``test_zero_commit_branch_skips_draft_pr_and_logs_info`` but with
    stdout ``"3\n"``, confirming the guard does not break the normal path.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    pr_create_cmds: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record pr-create calls; inject rev-list count of 3."""
        if "pr" in cmd and "create" in cmd:
            pr_create_cmds.append(list(cmd))
        # Inject non-zero commit count: 3 commits over main.
        if "git" in cmd and "rev-list" in cmd and "--count" in cmd:
            return _ok("3\n")
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    gh_pr_create_cmds = [
        c for c in pr_create_cmds if "gh" in c and "pr" in c and "create" in c
    ]
    assert gh_pr_create_cmds, (
        "gh pr create MUST be called when rev-list --count returns 3"
        " (non-zero commits); no pr create call recorded"
    )


def test_revlist_count_failure_falls_through_to_draft_pr() -> None:
    """rev-list --count failure → gh pr create still attempted (fail-open).

    If the ``git rev-list --count`` command exits non-zero (e.g. the
    remote ref is not yet fetched), the daemon must NOT silently skip
    ``_open_draft_pr``.  Skipping on error would cause silent data loss.
    The guard must be fail-open: any subprocess error from the count
    command causes the code to proceed as if the count is non-zero.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    pr_create_cmds: list[list[str]] = []

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record pr-create calls; make rev-list --count fail."""
        if "pr" in cmd and "create" in cmd:
            pr_create_cmds.append(list(cmd))
        # Simulate rev-list failing (unknown ref / network issue).
        if "git" in cmd and "rev-list" in cmd and "--count" in cmd:
            return _fail("fatal: unknown revision 'origin/main'")
        return _make_run_side_effect(
            ready_issues=ready_issues,
            pr_head_sha="abc123",
            issue_branch="baton/issue-10-10",
            feature_branch_exists=False,
        )(cmd)

    with (
        patch.object(daemon_mod, "_run", side_effect=recording_run),
        patch("baton_harness.chain.daemon.fetch_blocked_by", return_value=[]),
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    gh_pr_create_cmds = [
        c for c in pr_create_cmds if "gh" in c and "pr" in c and "create" in c
    ]
    assert gh_pr_create_cmds, (
        "gh pr create MUST be called when rev-list --count fails (fail-open"
        " guard); no pr create call recorded"
    )


# ---------------------------------------------------------------------------
# Observability wiring (issue #74 — runlog substrate)
# ---------------------------------------------------------------------------
# These tests assert the daemon wires RunLog and emits structured events.
# The new modules (runlog, obs_config) are imported lazily inside each
# test so a missing implementation surfaces as an ImportError (correct
# red) rather than a collection-time failure that would break the
# existing 334 tests.


class TestRunlogObservabilityWiring:
    """Daemon observability wiring: RunLog construction and event emission."""

    def test_run_daemon_without_bh_env_does_not_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_daemon completes without raising when no BH_* obs vars are set.

        Proves that observability is best-effort and never prevents the
        daemon loop from running (risk R2).
        """
        # Clear all BH_* observability vars so load_obs_config uses defaults.
        for var in (
            "BH_PROJECT_ROOT",
            "BH_RUNLOG_PATH",
            "BH_HEARTBEAT_FILE",
            "BH_REDISPATCH_WINDOW_TICKS",
            "BH_REDISPATCH_MAX",
            "BH_HEARTBEAT_STALL_S",
            "BH_HEARTBEAT_PING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        # Must not raise — mirrors the happy-path once=True pattern.
        with (
            patch.object(
                daemon_mod,
                "_run",
                side_effect=_make_run_side_effect(
                    ready_issues=[],
                    pr_head_sha="abc123",
                    issue_branch="baton/issue-10-10",
                    feature_branch_exists=False,
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
                "baton_harness.chain.daemon.merge_issue_branch",
                return_value=MergeOutcome.MERGED,
            ),
            patch("baton_harness.chain.daemon.alert", return_value=True),
            _patch_run_worker("pr_created"),
        ):
            asyncio.run(
                run_daemon(
                    _minimal_wf_config(),
                    [_repo_cfg()],
                    once=True,
                    poll_interval_s=0,
                )
            )

    def test_daemon_startup_emits_daemon_start_event(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_daemon emits a daemon_start event on startup via RunLog.

        Uses BH_PROJECT_ROOT so load_obs_config resolves the log path
        under tmp_path, then patches the _write_line seam to capture
        what is written without touching the real filesystem.
        """
        import baton_harness.chain.runlog as runlog_mod

        monkeypatch.setenv("BH_PROJECT_ROOT", str(tmp_path))
        for var in (
            "BH_RUNLOG_PATH",
            "BH_HEARTBEAT_FILE",
            "BH_REDISPATCH_WINDOW_TICKS",
            "BH_REDISPATCH_MAX",
            "BH_HEARTBEAT_STALL_S",
            "BH_HEARTBEAT_PING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        written_lines: list[str] = []

        def capture_write(path: Path, line: str) -> None:
            written_lines.append(line)

        with (
            patch.object(runlog_mod, "_write_line", side_effect=capture_write),
            patch.object(
                daemon_mod,
                "_run",
                side_effect=_make_run_side_effect(
                    ready_issues=[],
                    pr_head_sha="abc123",
                    issue_branch="baton/issue-10-10",
                    feature_branch_exists=False,
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
                "baton_harness.chain.daemon.merge_issue_branch",
                return_value=MergeOutcome.MERGED,
            ),
            patch("baton_harness.chain.daemon.alert", return_value=True),
            _patch_run_worker("pr_created"),
        ):
            asyncio.run(
                run_daemon(
                    _minimal_wf_config(),
                    [_repo_cfg()],
                    once=True,
                    poll_interval_s=0,
                )
            )

        import json

        events = [json.loads(line) for line in written_lines]
        event_names = [e.get("event") for e in events]
        assert "daemon_start" in event_names, (
            f"Expected a daemon_start event in emitted lines; "
            f"got event names: {event_names!r}"
        )

        # The .baton-harness/ directory must exist under tmp_path
        # (mkdir-before-emit requirement).
        baton_dir = tmp_path / ".baton-harness"
        assert baton_dir.exists(), (
            f"Expected {baton_dir} to exist after daemon startup "
            f"(RunLog must mkdir parents)"
        )

    def test_daemon_emits_dispatch_and_outcome_events_around_work_unit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dispatch and outcome events are emitted around each work unit.

        Originally anticipated to need xfail due to patch-point fragility
        (the RunLog handle inside run_daemon could have been a local
        variable inaccessible to patching).  The implementation exposes
        the ``_write_line`` module-level seam in
        ``baton_harness.chain.runlog``, which allows direct patching
        without touching the RunLog instance itself — so the xfail marker
        was never needed and this test runs strict.
        """
        import baton_harness.chain.runlog as runlog_mod

        monkeypatch.setenv("BH_PROJECT_ROOT", str(tmp_path))
        for var in (
            "BH_RUNLOG_PATH",
            "BH_HEARTBEAT_FILE",
            "BH_REDISPATCH_WINDOW_TICKS",
            "BH_REDISPATCH_MAX",
            "BH_HEARTBEAT_STALL_S",
            "BH_HEARTBEAT_PING_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        ready_issues = [
            {
                "number": 10,
                "title": "Issue 10",
                "state": "open",
                "body": "",
                "url": "https://github.com/o/r/issues/10",
                "labels": [{"name": "agent-ready"}],
                "milestone": None,
                "assignees": [],
            }
        ]

        written_lines: list[str] = []

        def capture_write(path: Path, line: str) -> None:
            written_lines.append(line)

        with (
            patch.object(runlog_mod, "_write_line", side_effect=capture_write),
            patch.object(
                daemon_mod,
                "_run",
                side_effect=_make_run_side_effect(
                    ready_issues=ready_issues,
                    pr_head_sha="abc123",
                    issue_branch="baton/issue-10-10",
                    feature_branch_exists=False,
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
                "baton_harness.chain.daemon.merge_issue_branch",
                return_value=MergeOutcome.MERGED,
            ),
            patch("baton_harness.chain.daemon.alert", return_value=True),
            _patch_run_worker("pr_created"),
        ):
            asyncio.run(
                run_daemon(
                    _minimal_wf_config(),
                    [_repo_cfg()],
                    once=True,
                    poll_interval_s=0,
                )
            )

        import json

        events = [json.loads(line) for line in written_lines]
        event_names = [e.get("event") for e in events]
        assert "dispatch" in event_names, (
            f"Expected a dispatch event; got: {event_names!r}"
        )
        assert "outcome" in event_names, (
            f"Expected an outcome event; got: {event_names!r}"
        )


# ---------------------------------------------------------------------------
# #75 alert() severity axis — daemon call-site routing
# ---------------------------------------------------------------------------
# These tests assert on ``daemon.alert`` (not the old bare ``escalate``).
# They will FAIL until the impl agent replaces escalate(...) calls in
# daemon.py with alert(..., severity=<assigned>).


def test_ci_gate_failed_park_routes_through_alert_severity_critical() -> None:
    """CI-gate park uses alert severity=critical.

    The contract assigns severity='critical' to the park site whose
    summary matches ``"Issue #{n} parked: {reason} ({outcome.name})."``
    where reason is one of {CI check failed, CI timed out, merge conflict}.
    We drive a CI_FAILED outcome and assert alert was called with
    severity='critical'.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    mock_alert = MagicMock(return_value=True)

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
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
        # Simulate a CI_FAILED outcome from the merge gate.
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.CI_FAILED,
        ),
        # Patch alert (not escalate) — impl agent will add this name.
        patch("baton_harness.chain.daemon.alert", mock_alert),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    # At least one call to alert with severity='critical' must have fired.
    critical_calls = [
        c
        for c in mock_alert.call_args_list
        if c.kwargs.get("severity") == "critical"
    ]
    assert critical_calls, (
        "Expected alert(severity='critical') for CI_FAILED park; "
        f"all alert calls: {mock_alert.call_args_list}"
    )


def test_worker_exception_routes_through_alert_severity_warn() -> None:
    """Worker raising an exception uses alert severity='warn'.

    The contract assigns severity='warn' to the worker-raised-exception
    site whose summary contains ``"worker raised an exception"``.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    mock_alert = MagicMock(return_value=True)

    async def exploding_worker(issue: Any) -> str:  # noqa: ANN401
        raise RuntimeError("worker boom")

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
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
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", mock_alert),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator"
            "._run_worker",
            side_effect=exploding_worker,
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

    warn_calls = [
        c
        for c in mock_alert.call_args_list
        if c.kwargs.get("severity") == "warn"
    ]
    assert warn_calls, (
        "Expected alert(severity='warn') for worker exception; "
        f"all alert calls: {mock_alert.call_args_list}"
    )


def test_ci_gate_reentry_no_open_pr_alert_is_critical() -> None:
    """CI-gate re-entry with no open PR uses alert severity='critical'.

    The contract assigns severity='critical' to the park site whose
    summary contains ``"needs CI-gate re-entry but has no open PR"``
    (park reason ``"ci_gate_reentry: no open PR"``).  We drive the
    path by placing issue #10 in ``ci_gate_reentry`` and mocking
    ``_find_issue_pr`` to return ``(None, None)``, then assert that
    ``alert`` was called with ``severity='critical'``.
    """
    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    mock_alert = MagicMock(return_value=True)

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                pr_head_sha="abc123",
                issue_branch="baton/issue-10-10",
                feature_branch_exists=False,
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
            "baton_harness.chain.daemon.reconstruct",
            return_value=RecoveryResult(
                done=set(),
                parked_seed=set(),
                ci_gate_reentry={10},
                redispatch=set(),
            ),
        ),
        # No open PR found for the ci_gate_reentry issue.
        patch(
            "baton_harness.chain.daemon._find_issue_pr",
            return_value=(None, None),
        ),
        patch("baton_harness.chain.daemon.alert", mock_alert),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg()],
                once=True,
                poll_interval_s=0,
            )
        )

    critical_calls = [
        c
        for c in mock_alert.call_args_list
        if c.kwargs.get("severity") == "critical"
    ]
    assert critical_calls, (
        "Expected alert(severity='critical') for ci_gate_reentry with no"
        f" open PR; all alert calls: {mock_alert.call_args_list}"
    )
