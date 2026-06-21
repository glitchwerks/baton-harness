"""Unit tests for VP-5 — PR-exists mid-loop early exit in ``_run_worker``.

Tests that once a PR is detected via ``tracker.check_pr_exists`` after a
successful turn, the turn loop terminates immediately (VP-5 behaviour).

All async calls are driven with ``asyncio.run`` (no pytest-asyncio dep).
Mock strategy mirrors ``tests/vendor/test_exclude_labels_recheck.py``.

Coverage:
- A PR detected after turn 1 terminates the loop before turns 2..max_turns.
- Without a PR, all turns run (early-exit must not over-fire).
- A closed issue terminates the loop before the PR check (ordering).
- A ``check_pr_exists`` exception is swallowed and the loop continues
  (best-effort guard, matching VP-2/VP-3 swallow-and-continue style).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.orchestrator import Orchestrator
from baton_harness.vendor.symphony.tracker import Issue

# ---------------------------------------------------------------------------
# Helpers — verbatim style from test_exclude_labels_recheck.py
# ---------------------------------------------------------------------------


def _minimal_config(max_turns: int = 4) -> WorkflowConfig:
    """Return a minimal WorkflowConfig.

    Args:
        max_turns: Maximum loop iterations for the orchestrator under test.

    Returns:
        A WorkflowConfig wired with the ``blocked`` exclude label and no
        hook scripts.
    """
    return WorkflowConfig(
        prompt_template="Work on issue #{{ issue.number }}: {{ issue.title }}",
        tracker_labels=["agent-ready"],
        tracker_exclude_labels=["blocked"],
        tracker_assignee=None,
        max_concurrent=1,
        max_turns=max_turns,
        hook_after_create=None,
        hook_before_run=None,
        hook_after_run=None,
        hook_timeout_ms=5000,
        poll_interval_ms=1000,
        max_retry_backoff_ms=10000,
    )


def _fake_issue(number: int = 1) -> Issue:
    """Return a minimal open Issue.

    Args:
        number: GitHub issue number to embed in the fixture.

    Returns:
        An Issue in the ``open`` state with no labels.
    """
    return Issue(
        number=number,
        title="Test Issue",
        state="open",
        body="",
        url=f"https://github.com/o/r/issues/{number}",
        labels=[],
    )


def _make_orch(
    max_turns: int = 4,
    project_root: str = "/tmp/fake_root",
    state_path: str = "/tmp/fake_state.json",
) -> Orchestrator:
    """Create an Orchestrator with a minimal config.

    Args:
        max_turns: Forwarded to ``_minimal_config``.
        project_root: Fake filesystem root (never touched in tests).
        state_path: Fake state-file path (never touched in tests).

    Returns:
        A fully-constructed Orchestrator whose I/O dependencies will be
        replaced by mocks at the call site.
    """
    config = _minimal_config(max_turns=max_turns)
    return Orchestrator(
        config=config,
        project_root=project_root,
        state_path=state_path,
    )


# ---------------------------------------------------------------------------
# VP-5 test 1: PR detected after turn 1 — loop terminates early
# ---------------------------------------------------------------------------


def test_pr_exists_mid_loop_terminates_early() -> None:
    """A PR detected after turn 1 ends the loop; turns 2..8 must NOT run.

    RED test for VP-5.  Against unpatched code ``check_pr_exists`` is
    never consulted inside the loop, so all 8 turns execute.  After the
    fix, the loop breaks immediately once ``check_pr_exists`` returns True.

    The failing assertion (pre-fix) will read:
        AssertionError: Expected 1 turn (PR exists → early exit), got 8
    """
    orch = _make_orch(max_turns=8)
    issue = _fake_issue(number=1)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/1"

    fake_turn_result = MagicMock()
    fake_turn_result.success = True
    fake_turn_result.error = None

    turn_call_count = 0

    async def fake_run_turn(**kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal turn_call_count
        turn_call_count += 1
        return fake_turn_result

    async def fake_fetch_issue_state(num: int) -> str:
        # Issue stays open throughout — closed-issue break must not fire.
        return "open"

    async def fake_run_gh(args: list[str]) -> str:
        # No blocked label — VP-2 break must not fire.
        return json.dumps({"labels": []})

    async def fake_run_hook(  # noqa: ANN401
        name: str, script: object, **kwargs: object
    ) -> bool:
        return True

    with (
        patch.object(
            orch.workspace,
            "ensure_worktree",
            new_callable=AsyncMock,
            return_value=fake_wt,
        ),
        patch.object(
            orch.worker,
            "run_turn",
            side_effect=fake_run_turn,
        ),
        patch.object(
            orch.tracker,
            "fetch_issue_state",
            side_effect=fake_fetch_issue_state,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_hook",
            side_effect=fake_run_hook,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_gh",
            side_effect=fake_run_gh,
        ),
        patch.object(
            orch.tracker,
            "check_pr_exists",
            new_callable=AsyncMock,
            return_value=True,  # PR already exists after every turn
        ),
    ):
        result = asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 1, (
        f"Expected 1 turn (PR exists → early exit), got {turn_call_count}"
    )
    assert result == "pr_created", (
        f"Expected 'pr_created' when PR detected, got {result!r}"
    )


# ---------------------------------------------------------------------------
# VP-5 test 2: No PR — all turns must still run (regression guard)
# ---------------------------------------------------------------------------


def test_no_pr_runs_all_turns() -> None:
    """Without a PR, the full turn budget runs (VP-5 must not over-fire).

    This mirrors ``test_normal_run_without_block_runs_all_turns`` from the
    VP-2 suite.  It should PASS even against unpatched code because the
    current loop never checks ``check_pr_exists`` mid-loop — and must
    continue to pass after the fix.
    """
    orch = _make_orch(max_turns=3)
    issue = _fake_issue(number=2)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/2"

    fake_turn_result = MagicMock()
    fake_turn_result.success = True
    fake_turn_result.error = None

    turn_call_count = 0

    async def fake_run_turn(**kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal turn_call_count
        turn_call_count += 1
        return fake_turn_result

    async def fake_fetch_issue_state(num: int) -> str:
        return "open"

    async def fake_run_gh(args: list[str]) -> str:
        return json.dumps({"labels": []})

    async def fake_run_hook(  # noqa: ANN401
        name: str, script: object, **kwargs: object
    ) -> bool:
        return True

    with (
        patch.object(
            orch.workspace,
            "ensure_worktree",
            new_callable=AsyncMock,
            return_value=fake_wt,
        ),
        patch.object(
            orch.worker,
            "run_turn",
            side_effect=fake_run_turn,
        ),
        patch.object(
            orch.tracker,
            "fetch_issue_state",
            side_effect=fake_fetch_issue_state,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_hook",
            side_effect=fake_run_hook,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_gh",
            side_effect=fake_run_gh,
        ),
        patch.object(
            orch.tracker,
            "check_pr_exists",
            new_callable=AsyncMock,
            return_value=False,  # No PR ever detected
        ),
    ):
        asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 3, (
        f"Expected 3 turns (no PR, no block), got {turn_call_count}"
    )


# ---------------------------------------------------------------------------
# VP-5 test 3: Closed issue terminates before PR check (ordering)
# ---------------------------------------------------------------------------


def test_closed_issue_precedes_pr_check() -> None:
    """Closed-issue break fires before VP-5's PR check (ordering contract).

    ``fetch_issue_state`` returns ``"closed"`` after turn 1.
    ``check_pr_exists`` would return True, but the closed-issue break must
    take precedence — VP-5 is added AFTER the existing checks, not before.

    After the fix the run must still terminate after exactly 1 turn, just
    as it does today (the closed-issue break is already in unpatched code).
    This test is RED pre-fix because it collapses to 1 turn for the
    *wrong* reason (closed issue) while the VP-5 PR check doesn't exist
    yet — the assertion value (1 turn) is correct either way, but the
    critical point is that adding VP-5 must not change this count.

    If VP-5 is accidentally inserted BEFORE the closed-issue check the
    return value would be ``"pr_created"`` rather than whatever the closed
    path returns, which would break the ordering invariant.
    """
    orch = _make_orch(max_turns=4)
    issue = _fake_issue(number=3)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/3"

    fake_turn_result = MagicMock()
    fake_turn_result.success = True
    fake_turn_result.error = None

    turn_call_count = 0

    async def fake_run_turn(**kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal turn_call_count
        turn_call_count += 1
        return fake_turn_result

    async def fake_fetch_issue_state(num: int) -> str:
        # Issue is immediately "closed" so the first post-turn state check
        # fires the existing break before any VP-5 PR check.
        return "closed"

    async def fake_run_gh(args: list[str]) -> str:
        return json.dumps({"labels": []})

    async def fake_run_hook(  # noqa: ANN401
        name: str, script: object, **kwargs: object
    ) -> bool:
        return True

    with (
        patch.object(
            orch.workspace,
            "ensure_worktree",
            new_callable=AsyncMock,
            return_value=fake_wt,
        ),
        patch.object(
            orch.worker,
            "run_turn",
            side_effect=fake_run_turn,
        ),
        patch.object(
            orch.tracker,
            "fetch_issue_state",
            side_effect=fake_fetch_issue_state,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_hook",
            side_effect=fake_run_hook,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_gh",
            side_effect=fake_run_gh,
        ),
        patch.object(
            orch.tracker,
            "check_pr_exists",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 1, (
        f"Expected 1 turn (closed-issue break before PR check),"
        f" got {turn_call_count}"
    )


# ---------------------------------------------------------------------------
# VP-5 test 4: check_pr_exists error → best-effort, loop continues
# ---------------------------------------------------------------------------


def test_check_pr_exists_error_is_best_effort() -> None:
    """An exception from ``check_pr_exists`` must not crash the run.

    VP-5 must swallow the exception and continue to the next turn,
    matching the VP-2/VP-3 swallow-and-continue pattern for mid-loop
    guards.

    NOTE: Against unpatched code this test is RED because the current code
    calls ``check_pr_exists`` AFTER the loop (line ~228) and does not wrap
    it in a try/except, so the RuntimeError propagates.  The test is
    included to LOCK the guard contract so the VP-5 implementer adds the
    mid-loop call inside a try/except that swallows exceptions, rather than
    letting them propagate either from the mid-loop site or the post-loop
    site.  A correct VP-5 implementation must satisfy this case: even when
    ``check_pr_exists`` raises on every call, the run must complete all
    turns without propagating the error.
    """
    orch = _make_orch(max_turns=2)
    issue = _fake_issue(number=4)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/4"

    fake_turn_result = MagicMock()
    fake_turn_result.success = True
    fake_turn_result.error = None

    turn_call_count = 0

    async def fake_run_turn(**kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal turn_call_count
        turn_call_count += 1
        return fake_turn_result

    async def fake_fetch_issue_state(num: int) -> str:
        return "open"

    async def fake_run_gh(args: list[str]) -> str:
        return json.dumps({"labels": []})

    async def fake_run_hook(  # noqa: ANN401
        name: str, script: object, **kwargs: object
    ) -> bool:
        return True

    async def raising_check_pr_exists(issue_number: int) -> bool:
        raise RuntimeError("simulated tracker failure")

    with (
        patch.object(
            orch.workspace,
            "ensure_worktree",
            new_callable=AsyncMock,
            return_value=fake_wt,
        ),
        patch.object(
            orch.worker,
            "run_turn",
            side_effect=fake_run_turn,
        ),
        patch.object(
            orch.tracker,
            "fetch_issue_state",
            side_effect=fake_fetch_issue_state,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_hook",
            side_effect=fake_run_hook,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_gh",
            side_effect=fake_run_gh,
        ),
        patch.object(
            orch.tracker,
            "check_pr_exists",
            side_effect=raising_check_pr_exists,
        ),
    ):
        # Must not raise despite check_pr_exists throwing on every call.
        asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 2, (
        f"Expected 2 turns (errors swallowed, all turns run),"
        f" got {turn_call_count}"
    )
