r"""Unit tests for VP-5 — PR-exists mid-loop early exit in ``_run_worker``.

Tests that once a PR is detected via ``tracker.check_pr_exists`` after a
successful turn AND the worktree is clean, the turn loop terminates
immediately (revised VP-5 behaviour).

Revised contract (Codex P2): the mid-loop early-exit fires only when BOTH
(a) ``check_pr_exists(issue.number)`` is True AND (b) the worktree is clean
— i.e. ``await run_cmd(["git", "status", "--porcelain",
"--untracked-files=no"], cwd=wt.path)`` returns empty after ``.strip()``.
If a PR exists but the worktree is dirty, the loop does NOT break; it
continues so a later turn can commit.  The whole check remains best-effort
(any exception → swallow → continue, no early-exit).

``run_cmd`` is patched at
``baton_harness.vendor.symphony.orchestrator.run_cmd`` (the name the
implementation imports into that namespace from ``.workspace``).
``return_value=""`` → clean worktree; ``return_value=" M src/foo.py\\n"``
→ dirty worktree.

All async calls are driven with ``asyncio.run`` (no pytest-asyncio dep).
Mock strategy mirrors ``tests/vendor/test_exclude_labels_recheck.py``.

Coverage:
- A PR detected after turn 1 with a clean worktree terminates the loop.
- Without a PR, all turns run (early-exit must not over-fire).
- A closed issue terminates the loop before the PR check (ordering).
- A ``check_pr_exists`` exception is swallowed and the loop continues
  (best-effort guard, matching VP-2/VP-3 swallow-and-continue style).
- Mid-loop True + clean + post-loop raise → ``"pr_created"`` (latch).
- Mid-loop True + clean + post-loop False → ``"pr_created"`` (latch).
- PR appearing on a later turn stops exactly at that turn (clean wt).
- A dirty worktree prevents early-exit even when ``check_pr_exists`` is
  True (revised contract — RED until implementation lands).
- A clean worktree allows early-exit when ``check_pr_exists`` is True
  (positive-path pin for the clean branch).
- A clean worktree with unpushed commits must NOT early-exit (Codex P2 —
  RED until implementation adds rev-list ahead check).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.orchestrator import Orchestrator
from baton_harness.vendor.symphony.tracker import Issue
from baton_harness.vendor.symphony.workspace import WorkspaceError

# ---------------------------------------------------------------------------
# Helpers — verbatim style from test_exclude_labels_recheck.py
# ---------------------------------------------------------------------------

_ORCHESTRATOR_RUN_CMD = "baton_harness.vendor.symphony.orchestrator.run_cmd"


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
# VP-5 test 1: PR detected after turn 1 with clean worktree → early exit
# ---------------------------------------------------------------------------


def test_pr_exists_mid_loop_terminates_early() -> None:
    """A PR detected after turn 1 with a clean worktree ends the loop early.

    Revised VP-5 contract: BOTH check_pr_exists True AND clean worktree
    (run_cmd → "") must hold for the early-exit to fire.

    ``run_cmd`` is mocked to return ``""`` (clean) so the worktree gate
    does not block the exit once the implementation lands.  Against current
    code (no worktree gate), the mock is inert and the test still passes
    (current code breaks on check_pr_exists True alone, satisfying the same
    turn-count assertion).

    Failing assertion (pre-VP-5-fix) will read:
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
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value="",  # clean worktree → early-exit allowed
            create=True,  # attribute absent until impl imports run_cmd
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
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value="",  # harmless — run_cmd not reached when PR absent
            create=True,  # attribute absent until impl imports run_cmd
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
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value="",  # harmless — closed-issue break fires first
            create=True,  # attribute absent until impl imports run_cmd
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

    ``run_cmd`` is not reached when ``check_pr_exists`` raises (exception
    swallowed before the worktree check), so the mock is harmless.
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
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            # harmless — not reached when check_pr_exists raises
            return_value="",
            create=True,  # attribute absent until impl imports run_cmd
        ),
    ):
        # Must not raise despite check_pr_exists throwing on every call.
        asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 2, (
        f"Expected 2 turns (errors swallowed, all turns run),"
        f" got {turn_call_count}"
    )


# ---------------------------------------------------------------------------
# VP-5 double-probe race: mid-loop True + post-loop raise → "pr_created"
# ---------------------------------------------------------------------------


def test_mid_loop_true_then_post_loop_raise_returns_pr_created() -> None:
    """Mid-loop True + clean worktree must latch; post-loop raise keeps it.

    Sequence of ``check_pr_exists`` calls:
      call 1 (mid-loop, turn 1): returns True  → worktree checked (clean)
                                                 → loop breaks
      call 2 (post-loop re-probe): raises RuntimeError

    Against current code the post-loop exception is swallowed and
    ``pr_exists`` stays False, so ``_run_worker`` returns ``"no_pr"``.
    The forthcoming fix latches ``pr_detected = True`` when the mid-loop
    call returns True (and the worktree is clean) and short-circuits the
    post-loop re-probe, returning ``"pr_created"`` regardless.

    ``run_cmd`` is mocked to ``""`` (clean) so the worktree gate passes.

    Failing assertion (pre-fix):
        AssertionError: assert result == "pr_created" (got "no_pr")
    """
    orch = _make_orch(max_turns=8)
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
            # call 1: mid-loop returns True (break); call 2: post-loop raises
            side_effect=[True, RuntimeError("post-loop flake")],
        ),
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value="",  # clean worktree → early-exit allowed
            create=True,  # attribute absent until impl imports run_cmd
        ),
    ):
        result = asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 1, (
        f"Expected 1 turn (mid-loop True → break), got {turn_call_count}"
    )
    assert result == "pr_created", (
        f"Expected 'pr_created' (mid-loop latch), got {result!r}"
    )


# ---------------------------------------------------------------------------
# VP-5 double-probe race: mid-loop True + post-loop False → "pr_created"
# ---------------------------------------------------------------------------


def test_mid_loop_true_then_post_loop_false_returns_pr_created() -> None:
    """Mid-loop True + clean worktree must latch; post-loop False keeps it.

    Sequence of ``check_pr_exists`` calls:
      call 1 (mid-loop, turn 1): returns True  → worktree checked (clean)
                                                 → loop breaks
      call 2 (post-loop re-probe): returns False

    Against current code the post-loop False sets ``pr_exists = False`` and
    ``_run_worker`` returns ``"no_pr"``.  The forthcoming fix latches
    ``pr_detected = True`` and skips the post-loop re-probe (or ignores its
    result), returning ``"pr_created"``.

    ``run_cmd`` is mocked to ``""`` (clean) so the worktree gate passes.

    Failing assertion (pre-fix):
        AssertionError: assert result == "pr_created" (got "no_pr")
    """
    orch = _make_orch(max_turns=8)
    issue = _fake_issue(number=6)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/6"

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
            # call 1: mid-loop returns True (break); call 2: post-loop False
            side_effect=[True, False],
        ),
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value="",  # clean worktree → early-exit allowed
            create=True,  # attribute absent until impl imports run_cmd
        ),
    ):
        result = asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 1, (
        f"Expected 1 turn (mid-loop True → break), got {turn_call_count}"
    )
    assert result == "pr_created", (
        f"Expected 'pr_created' (mid-loop latch), got {result!r}"
    )


# ---------------------------------------------------------------------------
# VP-5 guard: PR appearing on a later turn stops exactly at that turn
# ---------------------------------------------------------------------------


def test_pr_appears_on_later_turn_stops_there() -> None:
    """PR detected on turn 3 with a clean worktree stops after exactly 3 turns.

    Sequence of ``check_pr_exists`` calls (one per turn, plus any post-loop):
      call 1 (mid-loop, turn 1): False  → no run_cmd call
      call 2 (mid-loop, turn 2): False  → no run_cmd call
      call 3 (mid-loop, turn 3): True   → run_cmd called (clean) → break
      call 4 (post-loop re-probe, if present): True (harmless)

    ``run_cmd`` mocked to ``""`` (clean) — the worktree gate passes when
    the PR is finally detected on turn 3.

    This pins later-turn detection — not just the turn-1 case covered by
    test_pr_exists_mid_loop_terminates_early.  It may pass against current
    code if the post-loop probe returns True (no net state change) but is
    included to guard the turn-count contract after the fix.

    Failing assertion if VP-5 loop-break is missing (all 8 turns run):
        AssertionError: assert turn_call_count == 3 (got 8)
    """
    orch = _make_orch(max_turns=8)
    issue = _fake_issue(number=7)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/7"

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
            # turns 1-2: no PR; turn 3: PR found; 4th value covers post-loop
            side_effect=[False, False, True, True],
        ),
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value="",  # clean worktree → early-exit allowed on turn 3
            create=True,  # attribute absent until impl imports run_cmd
        ),
    ):
        result = asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 3, (
        f"Expected 3 turns (PR appears on turn 3), got {turn_call_count}"
    )
    assert result == "pr_created", (
        f"Expected 'pr_created' (PR detected on turn 3), got {result!r}"
    )


# ---------------------------------------------------------------------------
# VP-5 revised contract: dirty worktree prevents early exit (RED pre-impl)
# ---------------------------------------------------------------------------


def test_dirty_worktree_does_not_early_exit() -> None:
    r"""A dirty worktree must prevent the early-exit even when PR exists.

    Revised VP-5 contract: the mid-loop break fires only when BOTH
    ``check_pr_exists`` is True AND the worktree is clean.  When
    ``run_cmd`` returns a non-empty status string (dirty), the loop must
    continue so a later turn can commit the pending changes.

    Setup:
      - max_turns=4
      - check_pr_exists → True on every call
      - run_cmd → " M src/foo.py\\n" (dirty) on every call
      - fetch_issue_state → "open"
      - no blocked label

    Expected: run_turn called 4 times (all turns run; no early-exit).

    RED pre-implementation: current code (no worktree check) breaks as soon
    as check_pr_exists is True → run_turn called once → assertion fails.

    Failing assertion (pre-impl):
        AssertionError: Expected 4 turns (dirty worktree, no early-exit),
        got 1

    Also asserts run_cmd was awaited with git status args (pins that the
    worktree check actually runs).
    """
    orch = _make_orch(max_turns=4)
    issue = _fake_issue(number=8)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/8"

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

    dirty_status = " M src/foo.py\n"

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
            return_value=True,  # PR exists after every turn
        ),
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value=dirty_status,  # dirty → early-exit must NOT fire
            create=True,  # attribute absent until impl imports run_cmd
        ) as mock_run_cmd,
    ):
        result = asyncio.run(orch._run_worker(issue))

    # Primary RED assertion: all 4 turns must run despite PR existing.
    assert turn_call_count == 4, (
        f"Expected 4 turns (dirty worktree, no early-exit),"
        f" got {turn_call_count}"
    )

    # Secondary: confirm run_cmd was called with git-status args at least once.
    assert mock_run_cmd.called, (
        "Expected run_cmd to be called for worktree status check"
    )
    called_args = [
        call.args[0] if call.args else call.kwargs.get("args", [])
        for call in mock_run_cmd.call_args_list
    ]
    assert any(
        "status" in args and "--porcelain" in args for args in called_args
    ), f"Expected a git-status --porcelain call; got: {called_args!r}"

    # Tertiary: dirty path must still report "pr_created" via post-loop probe
    # (tree stayed dirty so no early-exit, but post-loop check_pr_exists
    # is True → "pr_created").  Pins that all-dirty does NOT regress to
    # "no_pr".
    assert result == "pr_created", (
        f"Expected 'pr_created' (dirty path, post-loop probe finds PR),"
        f" got {result!r}"
    )


# ---------------------------------------------------------------------------
# VP-5 revised contract: clean worktree allows early exit (positive pin)
# ---------------------------------------------------------------------------


def test_clean_worktree_early_exits() -> None:
    """A clean worktree combined with PR-exists fires the early exit.

    Positive-path pin for the revised VP-5 contract.  Locks that a clean
    worktree does NOT block the exit — only a dirty one does.

    Setup:
      - max_turns=8
      - check_pr_exists → True on every call
      - run_cmd → "" (clean) on every call
      - fetch_issue_state → "open"
      - no blocked label

    Expected: run_turn called exactly once, result "pr_created".

    Against current code (no worktree gate) this passes because the early-
    exit fires on check_pr_exists True alone — the mock is inert.  Once
    the implementation adds the worktree gate, run_cmd returns "" → clean
    → early-exit still fires.  This test therefore passes both before and
    after the implementation, pinning the clean-path contract throughout.

    Also asserts run_cmd was awaited with git-status args (pins that the
    worktree check actually runs in the post-implementation path).
    """
    orch = _make_orch(max_turns=8)
    issue = _fake_issue(number=9)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/9"

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
            return_value=True,  # PR exists after every turn
        ),
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            return_value="",  # clean → early-exit must fire
            create=True,  # attribute absent until impl imports run_cmd
        ) as mock_run_cmd,
    ):
        result = asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 1, (
        f"Expected 1 turn (clean worktree + PR exists → early exit),"
        f" got {turn_call_count}"
    )
    assert result == "pr_created", (
        f"Expected 'pr_created' (clean worktree exit), got {result!r}"
    )

    # Pin that the worktree check runs (post-implementation guard).
    # Pre-implementation: mock not called (run_cmd absent from code) — OK,
    # the call-args assertion is inside an ``if mock_run_cmd.called`` guard
    # so it does not force a RED on this test before the impl lands.
    if mock_run_cmd.called:
        called_args = [
            call.args[0] if call.args else call.kwargs.get("args", [])
            for call in mock_run_cmd.call_args_list
        ]
        assert any(
            "status" in args and "--porcelain" in args for args in called_args
        ), f"Expected a git-status --porcelain call; got: {called_args!r}"


# ---------------------------------------------------------------------------
# VP-5 best-effort: run_cmd failure swallowed, run continues conservatively
# ---------------------------------------------------------------------------


def test_run_cmd_failure_continues_conservatively() -> None:
    """A ``run_cmd`` error inside the mid-loop VP-5 block must be swallowed.

    The mid-loop PR check is wrapped in a best-effort ``try/except``; any
    exception — including ``WorkspaceError`` from ``run_cmd`` — must be
    swallowed and the turn loop must continue rather than crashing.

    Setup:
      - max_turns=4
      - check_pr_exists → True on every call (PR found in principle)
      - run_cmd → raises ``WorkspaceError("git_failed", "boom")`` on every
        call (git status fails on every attempt)
      - fetch_issue_state → "open" (no closed-issue break)
      - no blocked label

    Expected (characterising existing conservative contract):
      - run_turn called all 4 times (swallowed run_cmd failure must NOT
        cause an early-exit and must NOT abort the run).
      - ``_run_worker`` completes without propagating the exception.
      - result is ``"pr_created"`` (post-loop ``check_pr_exists`` probe
        still returns True → ``"pr_created"``).

    This pins the conservative "git status failure → keep looping, never
    crash" invariant.  A regression that lets the exception propagate would
    surface as an unhandled ``WorkspaceError`` raised by ``asyncio.run``.
    """
    orch = _make_orch(max_turns=4)
    issue = _fake_issue(number=10)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/10"

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
            return_value=True,  # PR found on every call (per-turn + post-loop)
        ),
        patch(
            _ORCHESTRATOR_RUN_CMD,
            new_callable=AsyncMock,
            side_effect=WorkspaceError("git_failed", "boom"),
            create=True,  # attribute absent until impl imports run_cmd
        ),
    ):
        # Must not raise even though run_cmd errors on every turn.
        result = asyncio.run(orch._run_worker(issue))

    assert turn_call_count == 4, (
        f"Expected 4 turns (run_cmd failure swallowed, all turns run),"
        f" got {turn_call_count}"
    )
    assert result == "pr_created", (
        f"Expected 'pr_created' (post-loop probe succeeds despite run_cmd"
        f" failures), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Codex P2: clean tree but unpushed commits → must NOT early-exit (RED)
# ---------------------------------------------------------------------------


def test_clean_but_unpushed_commits_does_not_early_exit() -> None:
    r"""Clean worktree ahead of remote must NOT trigger early-exit.

    Codex P2 gate condition: the mid-loop break requires THREE conditions:
      1. ``check_pr_exists`` returns True.
      2. ``git status --porcelain --untracked-files=no`` output is empty
         (worktree is clean — no uncommitted changes).
      3. ``git rev-list --count @{upstream}..HEAD`` returns ``"0"`` (branch
         is NOT ahead of remote — no unpushed commits).

    When condition 3 fails (the branch is ahead), the early-exit must NOT
    fire even though conditions 1 and 2 are met.  A later turn must be
    allowed to push the commit before the daemon can safely exit.

    The ``run_cmd`` mock is argument-aware (side_effect callable) so that:
      - args containing ``"rev-list"`` → returns ``"1\\n"`` (1 unpushed
        commit; branch is ahead of remote).
      - args containing ``"status"`` → returns ``""`` (clean worktree).
      - any other args → returns ``""`` (safe default).

    This shape is safe for the 10 existing tests: they each install their
    own ``patch(_ORCHESTRATOR_RUN_CMD, ..., return_value=...)`` context,
    which uses a constant ``return_value``, not this callable.  They are
    unaffected by this test's mock.

    Setup:
      - max_turns=4
      - check_pr_exists → True (PR already open)
      - fetch_issue_state → "open" (issue stays open, no closed-issue break)
      - no blocked label (VP-2 break must not fire)
      - run_cmd side_effect: "1\\n" for rev-list, "" for status

    Expected (correct implementation):
      - run_turn called all 4 times (clean-but-ahead must NOT early-exit).
      - run_cmd called at least once with a ``rev-list`` command (pins that
        the ahead check is actually performed by the implementation).

    RED pre-implementation: current code checks only ``status`` (condition
    2) and ignores condition 3 (no rev-list call).  Because status is
    clean and check_pr_exists is True, the early-exit fires after turn 1 →
    run_turn called once → assertion fails:

        AssertionError: assert 1 == 4 — Expected 4 turns
        (clean-but-unpushed, no early-exit), got 1
    """
    orch = _make_orch(max_turns=4)
    issue = _fake_issue(number=11)

    fake_wt = MagicMock()
    fake_wt.created_now = False
    fake_wt.path = "/fake/wt/11"

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

    def _arg_aware_run_cmd(args: list[str], **kwargs: object) -> str:
        r"""Return git output keyed on which subcommand is being called.

        Args:
            args: The git argument list passed to run_cmd.
            **kwargs: Forwarded keyword args (e.g. ``cwd``); unused here.

        Returns:
            ``"1\\n"`` for a rev-list call (1 unpushed commit), ``""``
            for a status call or any other git subcommand.
        """
        if "rev-list" in args:
            return "1\n"  # branch is ahead — 1 unpushed commit
        if "status" in args:
            return ""  # clean worktree
        return ""

    mock_run_cmd_async = AsyncMock(side_effect=_arg_aware_run_cmd)

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
            return_value=True,  # PR open on every turn
        ),
        patch(
            _ORCHESTRATOR_RUN_CMD,
            mock_run_cmd_async,
            create=True,  # attribute absent until impl imports run_cmd
        ),
    ):
        asyncio.run(orch._run_worker(issue))

    # Primary RED assertion: all 4 turns must run despite PR existing
    # and worktree being clean — the unpushed commit blocks early-exit.
    assert turn_call_count == 4, (
        f"Expected 4 turns (clean-but-unpushed, no early-exit),"
        f" got {turn_call_count}"
    )

    # Pin that the impl actually calls run_cmd with a rev-list command.
    all_call_args = [
        call.args[0] if call.args else call.kwargs.get("args", [])
        for call in mock_run_cmd_async.call_args_list
    ]
    assert any(
        "rev-list" in args for args in all_call_args
    ), (
        "Expected run_cmd to be called with a rev-list command"
        f" (ahead-of-remote check); got: {all_call_args!r}"
    )
