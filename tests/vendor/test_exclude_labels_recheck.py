"""Unit tests for VP-2 — exclude_labels re-check in ``_run_worker``.

Tests that a mid-turn ``blocked`` label terminates the turn loop (VP-2
behaviour) and that the ``running[N]`` guard does not raise when the
state dict is missing the issue entry.

All async calls are driven with ``asyncio.run`` (no pytest-asyncio dep).
Mock strategy follows ``tests/vendor/test_run_hook_env.py``.

Coverage:
- A ``blocked`` label appearing after a successful turn terminates the
  loop before the next turn.
- Confirm that the existing ``if issue.number in self.state.running``
  guard (present in the vendored source) does NOT raise a KeyError when
  the entry is absent — VP-2's guard requirement is satisfied.
- Normal run (no block label) runs all turns.
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
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config(max_turns: int = 4) -> WorkflowConfig:
    """Return a minimal WorkflowConfig."""
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
    """Return a minimal Issue dataclass."""
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
    """Create an Orchestrator with a minimal config and mocked workspace."""
    config = _minimal_config(max_turns=max_turns)
    orch = Orchestrator(
        config=config,
        project_root=project_root,
        state_path=state_path,
    )
    return orch


# ---------------------------------------------------------------------------
# VP-2: blocked label terminates the turn loop early
# ---------------------------------------------------------------------------


def test_blocked_label_terminates_turn_loop_early() -> None:
    """A ``blocked`` label after turn 1 terminates before turn 2."""
    orch = _make_orch(max_turns=4)
    issue = _fake_issue(number=7)

    # Worktree stub.
    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/7"

    # Worker.run_turn returns success.
    fake_turn_result = MagicMock()
    fake_turn_result.success = True
    fake_turn_result.error = None

    turn_call_count = 0

    async def fake_run_turn(**kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal turn_call_count
        turn_call_count += 1
        return fake_turn_result

    state_call_count = 0

    async def fake_fetch_issue_state(num: int) -> str:
        nonlocal state_call_count
        state_call_count += 1
        return "open"

    # run_gh mock for the VP-2 exclude_labels re-check.
    # Returns JSON with "blocked" label after the first state check.
    async def fake_run_gh(args: list[str]) -> str:
        # The exclude_labels re-check fetches labels for the current issue.
        return json.dumps({"labels": [{"name": "blocked"}]})

    # run_hook always succeeds.
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
            return_value=False,
        ),
    ):
        asyncio.run(orch._run_worker(issue))

    # The loop should have stopped after turn 1 due to the blocked label.
    # With max_turns=4 but blocked after turn 1, only 1 turn runs.
    assert turn_call_count == 1, (
        "Expected 1 turn (blocked label terminates loop),"
        f" got {turn_call_count}"
    )


def test_running_guard_does_not_raise_when_state_missing() -> None:
    """running[N] guard does not raise KeyError when state.running lacks N."""
    orch = _make_orch(max_turns=1)
    issue = _fake_issue(number=99)

    # Deliberately do NOT add issue 99 to state.running — the guard
    # must handle this gracefully (VP-2's guard requirement).
    assert 99 not in orch.state.running

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/99"

    fake_turn_result = MagicMock()
    fake_turn_result.success = True
    fake_turn_result.error = None

    async def fake_run_turn(**kwargs: Any) -> Any:  # noqa: ANN401
        return fake_turn_result

    async def fake_fetch_issue_state(num: int) -> str:
        return "open"

    async def fake_run_gh(args: list[str]) -> str:
        # No blocked label — loop completes normally.
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
            return_value=False,
        ),
    ):
        # This must not raise KeyError.
        result = asyncio.run(orch._run_worker(issue))

    # Just ensuring it completes without error.
    assert result in ("pr_created", "no_pr")


def test_normal_run_without_block_runs_all_turns() -> None:
    """Without a block label, the loop runs all max_turns turns."""
    orch = _make_orch(max_turns=3)
    issue = _fake_issue(number=5)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/5"

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
        # Never block.
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
            return_value=False,
        ),
    ):
        asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 3, (
        f"Expected 3 turns (no block), got {turn_call_count}"
    )
