"""Tests for the shared issue park transition (#351)."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import baton_harness.chain.daemon as daemon_mod
from baton_harness.chain.daemon.park import (
    ParkClass,
    ParkContext,
    park_issue,
)
from baton_harness.chain.failure_tally import FailureTally
from baton_harness.chain.merge import MergeOutcome


def _context(tmp_path: Path, *, count: int = 0) -> ParkContext:
    tally = FailureTally(tmp_path / "failures.json", max_count=2)
    for _ in range(count):
        tally.record_and_check(7)
    return ParkContext(
        owner="o",
        repo="r",
        installation_token="",
        report=None,
        runlog=None,
        sched=MagicMock(),
        liveness_state=MagicMock(),
        parked_reasons={},
        failure_tally=tally,
    )


@pytest.mark.parametrize(
    ("prior_count", "expected_add", "expected_count"),
    [(0, ["agent-ready"], 1), (1, ["agent-failed"], 0)],
)
def test_charged_park_restores_or_terminalises_at_budget(
    tmp_path: Path,
    prior_count: int,
    expected_add: list[str],
    expected_count: int,
) -> None:
    """Charged failures retry once, then terminalise and reset."""
    context = _context(tmp_path, count=prior_count)
    with (
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=[
                {"agent-done", "agent-in-progress"},
                set(expected_add),
            ],
        ),
        patch("baton_harness.chain.daemon._label_edit") as edit,
        patch("baton_harness.chain.daemon.alert", return_value=True),
    ):
        park_issue(
            context,
            7,
            ParkClass.CHARGED,
            reason="worker exception",
            detail="worker failed",
            severity="warn",
            kind="debug",
        )

    edit.assert_called_once()
    kwargs = edit.call_args.kwargs
    assert kwargs["add"] == expected_add
    assert set(kwargs["remove"]) == {"agent-done", "agent-in-progress"}
    assert context.failure_tally is not None
    assert context.failure_tally.peek(7) == expected_count


def test_uncharged_park_restores_without_charging(tmp_path: Path) -> None:
    """An infrastructure failure restores readiness without a charge."""
    context = _context(tmp_path, count=1)
    with (
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=[{"agent-in-progress"}, {"agent-ready"}],
        ),
        patch("baton_harness.chain.daemon._label_edit") as edit,
        patch("baton_harness.chain.daemon.alert", return_value=True),
    ):
        park_issue(
            context,
            7,
            ParkClass.UNCHARGED,
            reason="issue fetch failed",
            detail="fetch failed",
            severity="warn",
            kind="debug",
        )

    assert edit.call_args.kwargs["add"] == ["agent-ready"]
    assert context.failure_tally is not None
    assert context.failure_tally.peek(7) == 1


@pytest.mark.parametrize(
    "park_class", [ParkClass.STATE_INTACT, ParkClass.UNKNOWN_STATE]
)
def test_non_restoring_classes_add_no_state_label(
    tmp_path: Path,
    park_class: ParkClass,
) -> None:
    """State-intact and unknown-state parks never invent a state."""
    context = _context(tmp_path)
    with (
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=[{"blocked"}, {"blocked"}],
        ),
        patch("baton_harness.chain.daemon._label_edit") as edit,
        patch("baton_harness.chain.daemon.alert", return_value=True),
    ):
        park_issue(
            context,
            7,
            park_class,
            reason="parked",
            detail="parked",
            severity="critical",
            kind="block",
        )

    assert "add" not in edit.call_args.kwargs


def test_postcondition_violation_emits_critical_alert(tmp_path: Path) -> None:
    """A bad post-park state is surfaced immediately."""
    context = _context(tmp_path)
    with (
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=[{"agent-in-progress"}, set()],
        ),
        patch("baton_harness.chain.daemon._label_edit"),
        patch("baton_harness.chain.daemon.alert", return_value=True) as alert,
    ):
        park_issue(
            context,
            7,
            ParkClass.UNCHARGED,
            reason="fetch failed",
            detail="fetch failed",
            severity="warn",
            kind="debug",
        )

    assert alert.call_count == 2
    assert alert.call_args.kwargs["severity"] == "critical"
    assert "UNCHARGED" in alert.call_args.args[3]


def test_unreadable_charged_park_retains_exhausted_count(
    tmp_path: Path,
) -> None:
    """Unreadable labels do not erase state or grant a fresh budget."""
    context = _context(tmp_path, count=1)
    with (
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=[None, None],
        ),
        patch("baton_harness.chain.daemon._label_edit") as edit,
        patch("baton_harness.chain.daemon.alert", return_value=True),
    ):
        park_issue(
            context,
            7,
            ParkClass.CHARGED,
            reason="worker exception",
            detail="worker failed",
            severity="warn",
            kind="debug",
        )

    edit.assert_called_once()
    assert edit.call_args.kwargs["remove"] == ["agent-in-progress"]
    assert "add" not in edit.call_args.kwargs
    assert context.failure_tally is not None
    assert context.failure_tally.peek(7) == 2


def test_failed_terminal_label_edit_retains_exhausted_count(
    tmp_path: Path,
) -> None:
    """The failure budget resets only after terminal state is confirmed."""
    context = _context(tmp_path, count=1)
    with (
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=[{"agent-done"}, {"agent-done"}],
        ),
        patch(
            "baton_harness.chain.daemon._label_edit", return_value=False
        ) as edit,
        patch("baton_harness.chain.daemon.alert", return_value=True),
    ):
        park_issue(
            context,
            7,
            ParkClass.CHARGED,
            reason="worker exception",
            detail="worker failed",
            severity="warn",
            kind="debug",
        )

    assert edit.call_args.kwargs["add"] == ["agent-failed"]
    assert context.failure_tally is not None
    assert context.failure_tally.peek(7) == 2


def test_successful_ci_gate_resets_failure_tally(tmp_path: Path) -> None:
    """Every successful merge begins a fresh failure budget."""
    context = _context(tmp_path, count=1)
    with (
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon._label_edit"),
    ):
        outcome = daemon_mod._run_ci_gate(
            owner="o",
            repo="r",
            n=7,
            issue_branch="bug/7",
            pr_head_sha="abc",
            repo_root=tmp_path,
            branch_name="feature/test",
            sched=context.sched,
            liveness_state=context.liveness_state,
            runlog=None,
            merged_issues=[],
            parked_reasons=context.parked_reasons,
            ci_poll_interval=0,
            ci_timeout=0,
            park_context=context,
        )

    assert outcome is MergeOutcome.MERGED
    assert context.failure_tally is not None
    assert context.failure_tally.peek(7) == 0


def test_all_scheduler_park_exits_route_through_shared_helper() -> None:
    """Work-unit and CI-gate code contain no direct scheduler park calls."""
    root = Path(__file__).resolve().parents[2]
    paths = [
        root / "src/baton_harness/chain/daemon/work_unit.py",
        root / "src/baton_harness/chain/daemon/gh_api_helpers.py",
    ]
    park_calls: list[ast.Call] = []
    direct_progress_clears: list[ast.Call] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        direct_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mark_parked"
        ]
        assert not direct_calls, f"direct park exit remains in {path.name}"
        park_calls.extend(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "park_issue"
        )
        direct_progress_clears.extend(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_label_edit"
            and any(
                kw.arg == "remove"
                and isinstance(kw.value, ast.List)
                and len(kw.value.elts) == 1
                and isinstance(kw.value.elts[0], ast.Constant)
                and kw.value.elts[0].value == "agent-in-progress"
                for kw in node.keywords
            )
        )

    assert len(park_calls) == 18
    for call in park_calls:
        declared = call.args[2]
        assert isinstance(declared, ast.Attribute)
        assert isinstance(declared.value, ast.Attribute)
        assert declared.value.attr == "ParkClass"
    assert len(direct_progress_clears) == 1, (
        "only orphan cleanup before redispatch may directly clear progress"
    )


def test_both_ci_gate_callers_thread_park_context() -> None:
    """Normal and convergence merge paths share the failure tally."""
    path = (
        Path(__file__).resolve().parents[2]
        / "src/baton_harness/chain/daemon/work_unit.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_run_ci_gate"
    ]
    assert len(calls) == 2
    for call in calls:
        assert any(kw.arg == "park_context" for kw in call.keywords)
