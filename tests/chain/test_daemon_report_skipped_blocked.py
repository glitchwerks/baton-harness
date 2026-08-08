"""Unit tests for issue #343 — dispatch-excluded issues report gap.

Excluded (blocked) ``agent-ready`` issues currently vanish from the daemon's
session report instead of being recorded with
``outcome="skipped_blocked"``.

**Frozen consuming contract** (do not touch):
``src/baton_harness/scenario/expectations.py``'s ``"terminal-block"`` entry
requires ``issues_len == 1`` and ``issue.outcome == "skipped_blocked"``. Per
the design doc (``docs/superpowers/plans/
2026-07-09-daemon-report-scenario-harness-243.md`` §4, the "empty-``issues[]``
trap", B1), an issue seeded with **both** ``agent-ready`` and ``blocked``
labels is fetched by the daemon's ``agent-ready`` scan (so it *would* be
visible to the report) and then excluded from dispatch by the ``blocked``
filter (``_DISPATCH_EXCLUDE_LABELS``, ``daemon/__init__.py``) — today, that
exclusion happens **before** any ``IssueRecord`` is ever created for the
issue (``SessionReport._get_issue`` is lazily created only on
``record_pickup``, which the exclude filter never reaches), so the issue
disappears from ``report["issues"]`` entirely instead of leaving a
``skipped_blocked`` trace.

These tests drive the real ``run_daemon`` with the same ``_run``-seam-
stubbing convention established in ``tests/chain/test_daemon_report_wiring.py``
(helpers duplicated here rather than imported, per that file's own stated
convention and mirrored by ``tests/chain/test_daemon_orphan_scan.py``).
Report assertions read the **written JSON artifact** from an explicit
``report_path`` so these tests pin the observable contract (the file on
disk), not any particular internal call path — the implementer is free to
satisfy this from the snapshot filter in ``poll.py``, from the tracker fetch
path, or by any other means that lands the same JSON shape.

Coverage:
- A solo ``agent-ready``+``blocked`` issue (no other ready issues) produces
  exactly one ``issues[]`` entry with ``outcome="skipped_blocked"`` — the
  literal ``terminal-block`` scenario shape.
- The same issue, seeded alongside a second, independent clean
  ``agent-ready`` issue that dispatches and merges normally in the same
  tick, still produces two distinct entries: the clean issue outcome
  ``"merged"``, and the blocked issue outcome ``"skipped_blocked"`` — proving
  the fix records the skip without disturbing normal dispatch, and without
  the blocked issue being folded into or misclassified as the other
  issue's record.
- The written report, fed through the frozen
  ``baton_harness.scenario.verify.verify_report("terminal-block", ...)``
  matcher, actually PASSES — closing the loop against the exact consumer
  named in the issue.
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
from baton_harness.chain.merge import MergeOutcome
from baton_harness.chain.recovery import RecoveryResult
from baton_harness.chain.registry import RepoConfig
from baton_harness.scenario.verify import verify_report
from baton_harness.vendor.symphony.config import WorkflowConfig

_OWNER = "glitchwerks"
_REPO_NAME = "baton-harness"
_FEED_SHA = "feedface" * 5


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
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


def _repo_cfg(project_root: Path) -> RepoConfig:
    """Return a minimal RepoConfig rooted at ``project_root``.

    A ``.git`` marker directory is created (if absent) so
    ``_launch_one_issue``'s ``has_git_dir`` check does not fail closed.
    Mirrors ``tests/chain/test_daemon_report_wiring.py::_repo_cfg``.
    """
    (project_root / ".git").mkdir(exist_ok=True)
    return RepoConfig(
        owner=_OWNER,
        repo=_REPO_NAME,
        project_root=project_root,
    )


def _isolate_work_unit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against ambient ``os.environ`` leaks from the work-unit path.

    Mirrors ``tests/chain/test_daemon_report_wiring.py::
    _isolate_work_unit_env`` — ``_run_work_unit`` writes ``BH_VENV``,
    ``CHAIN_BASE_BRANCH``, and ``BH_FEATURE_BRANCH`` directly to the real
    ``os.environ`` as a side effect of dispatching a work unit.

    Args:
        monkeypatch: The test's ``monkeypatch`` fixture.
    """
    for key in ("BH_VENV", "CHAIN_BASE_BRANCH", "BH_FEATURE_BRANCH"):
        monkeypatch.delenv(key, raising=False)


def _make_issue(number: int, labels: list[str]) -> dict[str, Any]:
    """Build a minimal un-milestoned gh issue dict."""
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "body": "",
        "url": f"https://github.com/o/r/issues/{number}",
        "labels": [{"name": lbl} for lbl in labels],
        "milestone": None,
        "assignees": [],
    }


def _gh_noun_verb(cmd: list[str]) -> tuple[str | None, str | None]:
    """Extract the ``gh <noun> <verb>`` tokens from a ``_run`` argv."""
    is_gh = bool(cmd) and cmd[0] == "gh"
    noun = cmd[1] if is_gh and len(cmd) > 1 else None
    verb = cmd[2] if is_gh and len(cmd) > 2 else None
    return noun, verb


def _make_run_side_effect(
    *,
    ready_issues: list[dict[str, Any]],
    issue_branch_by_number: dict[int, str],
    post_worker_labels: str = "agent-done",
) -> Any:  # noqa: ANN401
    """Build a ``_run`` side-effect handling common gh/git commands.

    Mirrors ``tests/chain/test_daemon_report_wiring.py::
    _make_run_side_effect``.

    Args:
        ready_issues: Issues returned for ``gh issue list --label
            agent-ready``.
        issue_branch_by_number: Maps issue number to its issue-branch name
            for the ``gh pr list`` stub.
        post_worker_labels: The single label name returned by ``gh issue
            view`` for issues not otherwise excluded pre-dispatch.

    Returns:
        A callable matching ``daemon_mod._run``'s signature.
    """

    def side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        noun, verb = _gh_noun_verb(cmd)

        if noun == "issue" and verb == "list" and "agent-ready" in cmd:
            return _ok(json.dumps(ready_issues))
        if noun == "issue" and verb == "list" and "agent-in-progress" in cmd:
            return _ok(json.dumps([]))
        if noun == "issue" and verb == "view":
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
                        "labels": [{"name": post_worker_labels}],
                        "assignees": [],
                    }
                )
            )
        if noun == "issue" and verb == "edit":
            return _ok()
        if noun == "pr" and verb == "list":
            prs = [
                {
                    "number": n,
                    "headRefName": branch,
                    "headRefOid": _FEED_SHA,
                }
                for n, branch in issue_branch_by_number.items()
            ]
            return _ok(json.dumps(prs))
        if noun == "pr" and verb == "create":
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd and "push" in cmd:
            return _ok()
        if "ls-remote" in cmd:
            return _ok("")
        if "rev-parse" in cmd:
            return _ok(f"{_FEED_SHA}\n")
        return _ok()

    return side_effect


def _patch_run_worker(return_value: str = "pr_created") -> Any:  # noqa: ANN401
    """Patch Orchestrator._run_worker with an AsyncMock."""
    return patch(
        "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
        new_callable=AsyncMock,
        return_value=return_value,
    )


def _common_success_patches() -> Any:  # noqa: ANN401
    """Return the standard non-``_run`` patch stack for a clean work unit.

    Mirrors ``tests/chain/test_daemon_report_wiring.py::
    _common_success_patches``.
    """
    import contextlib

    @contextlib.contextmanager
    def ctx() -> Any:  # noqa: ANN401
        with (
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
        ):
            yield

    return ctx


# ---------------------------------------------------------------------------
# Solo terminal-block shape: exactly one issues[] entry, skipped_blocked.
# ---------------------------------------------------------------------------


def test_solo_agent_ready_and_blocked_issue_records_skipped_blocked(
    tmp_path: Path,
) -> None:
    """A lone agent-ready+blocked issue leaves one skipped_blocked record.

    This is the literal ``terminal-block`` scenario shape (§4, plan #243):
    the issue is fetched by the ``agent-ready`` scan (so it is visible to
    the daemon) then excluded from dispatch by the ``blocked`` filter. It
    must not vanish from the report — it must leave exactly one ``issues[]``
    entry with ``outcome="skipped_blocked"``.
    """
    report_path = tmp_path / "session-report.json"
    ready_issues = [_make_issue(77, ["agent-ready", "blocked"])]

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={},
            ),
        ),
        _common_success_patches()(),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    assert report_path.exists(), (
        "run_daemon must write a session report to report_path"
    )
    data = json.loads(report_path.read_text(encoding="utf-8"))
    issues = data["issues"]

    assert data["totals"]["issues_picked_up"] == 0, (
        "a dispatch-excluded issue must not be counted as picked up; "
        f"got totals={data['totals']!r}"
    )
    assert len(issues) == 1, (
        "a dispatch-excluded agent-ready+blocked issue must leave exactly "
        f"one issues[] entry, not vanish from the report; got {issues!r}"
    )
    assert issues[0]["number"] == 77
    assert issues[0]["outcome"] == "skipped_blocked", (
        "the excluded issue's outcome must be recorded as "
        f"'skipped_blocked'; got {issues[0]['outcome']!r}"
    )


# ---------------------------------------------------------------------------
# Combined: a normally-dispatched issue and a blocked issue coexist in the
# same tick without cross-contamination.
# ---------------------------------------------------------------------------


def test_skipped_blocked_issue_coexists_with_normally_merged_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked issue and a clean issue in the same tick are both recorded.

    Issue #10 carries only ``agent-ready`` and dispatches/merges normally.
    Issue #20 carries ``agent-ready`` and ``blocked`` and is dispatch-
    excluded. The final report must contain both, correctly attributed:
    #10 outcome="merged", #20 outcome="skipped_blocked" — the fix must not
    fold the skip into #10's record, drop #10, or misattribute the
    ``skipped_blocked`` outcome to the wrong issue number.
    """
    _isolate_work_unit_env(monkeypatch)
    report_path = tmp_path / "session-report.json"
    ready_issues = [
        _make_issue(10, ["agent-ready"]),
        _make_issue(20, ["agent-ready", "blocked"]),
    ]

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={10: "baton/issue-10-10"},
            ),
        ),
        _common_success_patches()(),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    data = json.loads(report_path.read_text(encoding="utf-8"))
    by_number = {i["number"]: i for i in data["issues"]}

    assert data["totals"]["issues_picked_up"] == 1, (
        "only the clean issue should be counted as picked up; "
        f"got totals={data['totals']!r}"
    )
    assert set(by_number) == {10, 20}, (
        "expected exactly issues #10 and #20 in the report; got "
        f"{sorted(by_number)!r}"
    )
    assert by_number[10]["outcome"] == "merged", (
        "the clean, non-excluded issue must still dispatch and merge "
        f"normally; got {by_number[10]!r}"
    )
    assert by_number[20]["outcome"] == "skipped_blocked", (
        "the dispatch-excluded issue must be recorded as "
        f"'skipped_blocked', not folded into or confused with #10's "
        f"record; got {by_number[20]!r}"
    )


# ---------------------------------------------------------------------------
# Live re-check: an initially ready issue becomes blocked before dispatch.
# ---------------------------------------------------------------------------


def test_live_recheck_excluded_issue_records_skipped_blocked(
    tmp_path: Path,
) -> None:
    """An issue blocked after the snapshot leaves a skipped record."""
    report_path = tmp_path / "session-report.json"
    ready_issues = [_make_issue(88, ["agent-ready"])]

    def fake_fetch_labels(
        owner: str,  # noqa: ARG001
        repo: str,  # noqa: ARG001
        issue: int,  # noqa: ARG001
        installation_token: str = "",  # noqa: ARG001
    ) -> set[str]:
        """Return the torn live state reached after the initial snapshot."""
        return {"agent-ready", "blocked"}

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={},
            ),
        ),
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=fake_fetch_labels,
        ),
        _common_success_patches()(),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    data = json.loads(report_path.read_text(encoding="utf-8"))
    issues = data["issues"]

    assert data["totals"]["issues_picked_up"] == 0, (
        "an issue excluded by the live re-check must not be counted as "
        f"picked up; got totals={data['totals']!r}"
    )
    assert len(issues) == 1, (
        "an issue excluded by the live re-check must leave exactly one "
        f"issues[] entry; got {issues!r}"
    )
    assert issues[0]["number"] == 88
    assert issues[0]["outcome"] == "skipped_blocked", (
        "the live-excluded issue must be recorded as 'skipped_blocked'; "
        f"got {issues[0]!r}"
    )
    assert issues[0]["skipped_at"] is not None, (
        "the live-excluded issue must record when it was skipped; "
        f"got {issues[0]!r}"
    )
    assert issues[0]["picked_up_at"] is None, (
        "the live-excluded issue was never dispatched and must not have a "
        f"pickup timestamp; got {issues[0]!r}"
    )


# ---------------------------------------------------------------------------
# Closing the loop: the written report actually satisfies the frozen
# terminal-block scenario expectation.
# ---------------------------------------------------------------------------


def test_written_report_satisfies_terminal_block_scenario_expectation(
    tmp_path: Path,
) -> None:
    """The real daemon's report passes the frozen terminal-block matcher.

    Drives ``run_daemon`` for the literal terminal-block seed (one issue
    carrying both ``agent-ready`` and ``blocked``), then feeds the written
    report through the untouched
    ``baton_harness.scenario.verify.verify_report("terminal-block", ...)``
    matcher — the actual consumer named in issue #343 — and asserts it
    passes.
    """
    report_path = tmp_path / "session-report.json"
    ready_issues = [_make_issue(5, ["agent-ready", "blocked"])]

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={},
            ),
        ),
        _common_success_patches()(),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    data = json.loads(report_path.read_text(encoding="utf-8"))
    result = verify_report("terminal-block", data)

    assert result.passed is True, (
        "the written report must satisfy the frozen terminal-block "
        f"expectation; assertions={result.assertions!r}"
    )
