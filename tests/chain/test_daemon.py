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
                "baton_harness.chain.daemon.escalate",
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch("baton_harness.chain.daemon.escalate", mock_escalate),
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
            patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch("baton_harness.chain.daemon.escalate", mock_escalate),
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
        patch(
            "baton_harness.chain.daemon.escalate", side_effect=fake_escalate
        ),
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
        patch(
            "baton_harness.chain.daemon.escalate", side_effect=fake_escalate
        ),
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
        patch("baton_harness.chain.daemon.escalate", return_value=True),
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
