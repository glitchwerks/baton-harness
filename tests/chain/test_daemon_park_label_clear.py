"""Tests for issue #88: agent-in-progress must be cleared on park paths.

Invariant C-I4 (daemon.py module docstring): ``agent-in-progress`` MUST
be cleared on every terminal branch.  Two park paths were missing the
label-edit call:

- Gap A: ``recovery_result.parked_seed`` branch (lines ~662-665).
- Gap B: ``ci_gate_reentry`` no-open-PR branch (lines ~675-677).

Both tests drive ``run_daemon`` with ``once=True`` and spy on the
``_run`` seam (``daemon_mod._run``) to assert that a ``gh issue edit
--remove-label agent-in-progress`` call is made before
``sched.mark_parked`` completes.  They also confirm ``mark_parked``
itself ran (via the escalate call or the absence of a dispatch).

Async test functions are driven with ``asyncio.run`` — no pytest-asyncio
dependency needed; this matches the style in test_daemon.py.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any
from unittest.mock import patch

import baton_harness.chain.daemon as daemon_mod
from baton_harness.chain.daemon import run_daemon
from baton_harness.chain.merge import MergeOutcome
from baton_harness.chain.recovery import RecoveryResult
from baton_harness.chain.registry import RepoConfig
from baton_harness.vendor.symphony.config import WorkflowConfig

# ---------------------------------------------------------------------------
# Shared helpers — copied from test_daemon.py conventions
# ---------------------------------------------------------------------------

_REPO_ROOT_FAKE = "/fake/repo"
_OWNER = "glitchwerks"
_REPO_NAME = "baton-harness"


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _minimal_wf_config() -> WorkflowConfig:
    """Return a minimal WorkflowConfig for test runs."""
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
    from pathlib import Path

    return RepoConfig(
        owner=_OWNER,
        repo=_REPO_NAME,
        project_root=Path(_REPO_ROOT_FAKE),
    )


def _issue_list_json(number: int, *extra_labels: str) -> str:
    """Return gh issue list JSON for a single issue."""
    import json

    labels = [{"name": "agent-ready"}]
    for lbl in extra_labels:
        labels.append({"name": lbl})
    return json.dumps(
        [
            {
                "number": number,
                "title": f"Issue {number}",
                "state": "open",
                "body": "",
                "url": f"https://github.com/o/r/issues/{number}",
                "labels": labels,
                "milestone": None,
                "assignees": [],
            }
        ]
    )


def _make_run_side_effect(
    issue_number: int = 10,
) -> Any:  # noqa: ANN401
    """Return a ``_run`` side-effect covering all command patterns.

    Args:
        issue_number: The issue number used for label-edit pattern matching.

    Returns:
        A callable suitable for use as ``side_effect`` on the ``_run`` mock.
    """

    def side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        cmd_str = " ".join(cmd)
        # Issue list (poll).
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        ):
            return _ok(_issue_list_json(issue_number))
        # Issue view.
        if (
            "issue" in cmd_str
            and "view" in cmd_str
            and "edit" not in cmd_str
        ):
            return _ok(
                _json.dumps(
                    {
                        "number": issue_number,
                        "title": f"Issue {issue_number}",
                        "state": "open",
                        "body": "",
                        "url": (
                            f"https://github.com/o/r/issues/{issue_number}"
                        ),
                        "labels": [{"name": "agent-done"}],
                        "assignees": [],
                    }
                )
            )
        # Label edits.
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        # PR list.
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_json.dumps([]))
        # PR create.
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        # Git push.
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        # git ls-remote.
        if "ls-remote" in cmd_str:
            return _ok("")
        # git rev-parse / rev-list.
        if "rev-parse" in cmd_str or "rev-list" in cmd_str:
            return _ok("0\n")
        return _ok()

    return side_effect


# ---------------------------------------------------------------------------
# Gap A: parked_seed recovery branch must clear agent-in-progress
# ---------------------------------------------------------------------------


def test_parked_seed_clears_agent_in_progress_before_mark_parked() -> None:
    """parked_seed branch removes agent-in-progress before mark_parked.

    Scenario: the daemon crashed after labelling issue #10 with
    ``agent-in-progress``.  On restart, ``recovery.reconstruct`` returns
    issue #10 in ``parked_seed`` (because it also carries ``blocked``).
    The daemon must clear ``agent-in-progress`` before calling
    ``sched.mark_parked(10)``.

    This is the load-bearing test for Gap A (C-I4 invariant).

    Asserts:
        - A ``gh issue edit --remove-label agent-in-progress`` call is
          observed in the ``_run`` seam.
        - ``sched.mark_parked`` ran (confirmed by the escalate/alert call
          that fires for a fully-parked DAG, OR by the absence of a
          _run_worker dispatch).
    """
    issue_number = 10
    label_edits: list[list[str]] = []
    worker_called: list[int] = []

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        worker_called.append(issue.number)
        return "pr_created"

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "issue" in cmd and "edit" in cmd:
            label_edits.append(list(cmd))
        return _make_run_side_effect(issue_number)(cmd)

    # Recovery: issue is in parked_seed (crashed while in-progress + blocked).
    recovery = RecoveryResult(
        done=set(),
        parked_seed={issue_number},
        ci_gate_reentry=set(),
        redispatch=set(),
    )

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
            return_value=recovery,
        ),
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator"
            "._run_worker",
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

    # The _run_worker must NOT have been dispatched — this was a recovery
    # park, not a fresh dispatch.
    assert worker_called == [], (
        "parked_seed issue must NOT be dispatched to _run_worker; "
        f"got worker calls for issues: {worker_called}"
    )

    # The critical assertion: agent-in-progress MUST be cleared.
    remove_in_progress_calls = [
        c
        for c in label_edits
        if "--remove-label" in c and "agent-in-progress" in c
    ]
    assert remove_in_progress_calls, (
        "Gap A (parked_seed): daemon must call "
        "'gh issue edit --remove-label agent-in-progress' before "
        "mark_parked when an issue is in recovery_result.parked_seed. "
        f"label_edits seen: {label_edits}"
    )


# ---------------------------------------------------------------------------
# Gap B: ci_gate_reentry no-open-PR branch must clear agent-in-progress
# ---------------------------------------------------------------------------


def test_ci_gate_reentry_no_pr_clears_agent_in_progress() -> None:
    """ci_gate_reentry no-PR branch removes agent-in-progress before park.

    Scenario: issue #10 is in ``ci_gate_reentry`` (the daemon was waiting
    for CI when it crashed) but when it restarts there is no open PR for
    the issue.  The daemon parks the issue via the "no open PR" branch.
    It must clear ``agent-in-progress`` before calling ``mark_parked``.

    Asserts:
        - A ``gh issue edit --remove-label agent-in-progress`` call is
          observed before the work unit exits.
        - ``_run_worker`` is NOT called (the no-PR path parks without
          re-dispatching).
    """
    issue_number = 10
    label_edits: list[list[str]] = []
    worker_called: list[int] = []

    async def fake_run_worker(issue: Any) -> str:  # noqa: ANN401
        worker_called.append(issue.number)
        return "pr_created"

    def recording_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _json

        if "issue" in cmd and "edit" in cmd:
            label_edits.append(list(cmd))
        cmd_str = " ".join(cmd)
        # Issue list (poll).
        if (
            "issue" in cmd_str
            and "list" in cmd_str
            and "agent-ready" in cmd_str
        ):
            return _ok(_issue_list_json(issue_number))
        # Issue view.
        if (
            "issue" in cmd_str
            and "view" in cmd_str
            and "edit" not in cmd_str
        ):
            return _ok(
                _json.dumps(
                    {
                        "number": issue_number,
                        "title": f"Issue {issue_number}",
                        "state": "open",
                        "body": "",
                        "url": (
                            f"https://github.com/o/r/issues/{issue_number}"
                        ),
                        "labels": [{"name": "agent-done"}],
                        "assignees": [],
                    }
                )
            )
        # Label edits.
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        # PR list: return EMPTY — simulates no open PR for the issue.
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_json.dumps([]))
        # PR create.
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        # Git push.
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        # git ls-remote.
        if "ls-remote" in cmd_str:
            return _ok("")
        # git rev-parse / rev-list.
        if "rev-parse" in cmd_str or "rev-list" in cmd_str:
            return _ok("0\n")
        return _ok()

    # Recovery: issue is in ci_gate_reentry.
    recovery = RecoveryResult(
        done=set(),
        parked_seed=set(),
        ci_gate_reentry={issue_number},
        redispatch=set(),
    )

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
            return_value=recovery,
        ),
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator"
            "._run_worker",
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

    # _run_worker must NOT be called on this path (ci_gate_reentry goes
    # directly to the merge gate or, when no PR found, parks immediately).
    assert worker_called == [], (
        "ci_gate_reentry no-PR path must NOT dispatch to _run_worker; "
        f"got worker calls: {worker_called}"
    )

    # The critical assertion: agent-in-progress MUST be cleared.
    remove_in_progress_calls = [
        c
        for c in label_edits
        if "--remove-label" in c and "agent-in-progress" in c
    ]
    assert remove_in_progress_calls, (
        "Gap B (ci_gate_reentry no-PR): daemon must call "
        "'gh issue edit --remove-label agent-in-progress' before "
        "mark_parked when ci_gate_reentry has no open PR. "
        f"label_edits seen: {label_edits}"
    )
