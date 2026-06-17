"""Unit tests for P1 worktree orphan-GC (issue #33, detect-first).

Tests the async ``scan_orphan_worktrees`` function (to be added to
``baton_harness.chain.recovery``) and the daemon wiring that emits
``orphan_worktree`` alerts and conditionally calls ``cleanup_worktree``.

All subprocess calls go through a patchable seam so no real git binary
or live worktree is required.

Async test functions are driven with ``asyncio.run`` so no pytest-asyncio
dependency is needed.

Coverage:
- Only true orphans (terminal-state, clean, pushed) are flagged.
- A worktree whose issue is in the active running set is never an orphan.
- A worktree with the ``agent-in-progress`` label is never an orphan.
- A worktree with uncommitted changes (dirty) is never an orphan (IS-5).
- A worktree with unpushed commits is never an orphan (IS-5).
- A worktree with no upstream branch is treated as unpushed → live (IS-5).
- ``cleanup_worktree`` is called only when ``worktree_gc == "reclaim"``
  AND the worktree is a confirmed orphan.
- ``cleanup_worktree`` is NEVER called in ``detect`` mode.
- Alert emitted with ``severity="warn"`` and ``kind="debug"`` for orphans.
- Runlog event ``{"event": "orphan_worktree"}`` emitted for orphans.
- Sweep never raises into the caller (guarded).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import baton_harness.chain.recovery as recovery_mod

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OWNER = "glitchwerks"
_REPO = "baton-harness"

# A path under .symphony/worktrees/<issue> as WorkspaceManager produces.
_WT_PATH_42 = "/repo/.symphony/worktrees/42"
_WT_PATH_99 = "/repo/.symphony/worktrees/99"
_WT_PATH_7 = "/repo/.symphony/worktrees/7"

# ---------------------------------------------------------------------------
# Porcelain output helpers
# ---------------------------------------------------------------------------


def _porcelain_block(
    worktree: str, branch: str = "refs/heads/baton/42"
) -> str:
    """Build one git worktree list --porcelain block."""
    return f"worktree {worktree}\nHEAD abc123def456\nbranch {branch}\n\n"


def _porcelain_bare_block(worktree: str) -> str:
    """Build a bare/detached porcelain block (no branch line)."""
    return f"worktree {worktree}\nHEAD abc123def456\ndetached\n\n"


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr=stderr
    )


# ---------------------------------------------------------------------------
# Fake subprocess router
# ---------------------------------------------------------------------------


def _make_git_seam(
    porcelain_output: str = "",
    *,
    issue_labels: dict[int, list[str]] | None = None,
    dirty_paths: set[str] | None = None,
    unpushed_paths: set[str] | None = None,
    no_upstream_paths: set[str] | None = None,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Return a callable suitable for patching the _run seam in recovery_mod.

    Args:
        porcelain_output: Output from ``git worktree list --porcelain``.
        issue_labels: Map of issue number → label list for label queries.
        dirty_paths: Worktree paths that ``git status --porcelain`` reports
            as dirty (non-empty output).
        unpushed_paths: Worktree paths that have unpushed commits.
        no_upstream_paths: Worktree paths with no upstream configured.
    """
    issue_labels = issue_labels or {}
    dirty_paths = dirty_paths or set()
    unpushed_paths = unpushed_paths or set()
    no_upstream_paths = no_upstream_paths or set()

    def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if (
            "worktree" in cmd_str
            and "list" in cmd_str
            and "--porcelain" in cmd_str
        ):
            return _ok(porcelain_output)
        # git -C <path> status --porcelain
        if "status" in cmd_str and "--porcelain" in cmd_str:
            # Extract the path from the -C flag position.
            try:
                c_idx = cmd.index("-C")
                wt_path = cmd[c_idx + 1]
            except (ValueError, IndexError):
                return _ok("")
            if wt_path in dirty_paths:
                return _ok("M  some_file.py\n")
            return _ok("")
        # git -C <path> log @{u}.. --oneline
        if "log" in cmd_str and "@{u}" in cmd_str:
            try:
                c_idx = cmd.index("-C")
                wt_path = cmd[c_idx + 1]
            except (ValueError, IndexError):
                return _ok("")
            if wt_path in no_upstream_paths:
                # Simulate no upstream: returncode non-zero
                return _fail("fatal: no upstream configured")
            if wt_path in unpushed_paths:
                return _ok("abc1234 some unpushed commit\n")
            return _ok("")
        # gh issue view --json labels for label checks
        return _ok("{}")

    return _run


# ---------------------------------------------------------------------------
# Seam 1 — scan_orphan_worktrees: porcelain parsing and orphan classification
# ---------------------------------------------------------------------------


def test_clean_pushed_terminal_issue_is_orphan_candidate() -> None:
    """A worktree whose issue is terminal, clean, and pushed is flagged orphan.

    This is the minimal positive case: no live predicates hold → orphan.
    """
    porcelain = _porcelain_block(_WT_PATH_42, "refs/heads/baton/42")

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
            )
        )

    assert 42 in orphans, (
        f"Issue 42 should be an orphan candidate; got orphans={orphans!r}"
    )


def test_active_running_issue_is_never_orphan() -> None:
    """A worktree whose issue is in the running set is live, not an orphan.

    IS-5 predicate (a): issue in current running/membership set → live.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset({42}),
                terminal_issues=frozenset({42}),
            )
        )

    assert 42 not in orphans, (
        "Issue 42 is in the running set; must not be flagged as orphan."
    )


def test_agent_in_progress_label_makes_worktree_live() -> None:
    """A worktree whose issue carries agent-in-progress is live, not an orphan.

    IS-5 predicate (b): agent-in-progress label → live.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_labels={42: ["agent-in-progress"]},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
            )
        )

    assert 42 not in orphans, (
        "Issue 42 has agent-in-progress label; must not be flagged as orphan."
    )


def test_dirty_worktree_is_live_not_orphan() -> None:
    """A terminal-state worktree with uncommitted changes is classified live.

    IS-5 predicate (c): dirty tree (git status --porcelain non-empty) → live.
    This is the single highest-value safety test per the plan.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            dirty_paths={_WT_PATH_42},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
            )
        )

    assert 42 not in orphans, (
        "Dirty worktree for issue 42 must not be flagged as orphan (IS-5)."
    )


def test_unpushed_commits_makes_worktree_live() -> None:
    """A terminal-state worktree with unpushed commits is classified live.

    IS-5 predicate (c): unpushed commits (git log @{u}.. non-empty) → live.
    This is the single highest-value safety test per the plan.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            unpushed_paths={_WT_PATH_42},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
            )
        )

    assert 42 not in orphans, (
        "Worktree with unpushed commits for issue 42 must not be flagged "
        "orphan (IS-5)."
    )


def test_no_upstream_branch_makes_worktree_live() -> None:
    """A worktree with no upstream configured is treated as unpushed → live.

    IS-5: no upstream → treat as unpushed → live. git log @{u}.. returns
    non-zero when no upstream is configured.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            no_upstream_paths={_WT_PATH_42},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
            )
        )

    assert 42 not in orphans, (
        "Worktree with no upstream for issue 42 must be treated as "
        "unpushed → live (IS-5)."
    )


def test_mixed_worktrees_only_true_orphans_flagged() -> None:
    """Mixed pool: only the clean + pushed + terminal worktree is an orphan.

    Verifies the classifier handles multiple worktrees simultaneously and
    does not conflate live predicates across different paths.

    - Issue 42: terminal, clean, pushed → orphan
    - Issue 99: terminal, dirty → live
    - Issue 7: in running set → live
    """
    porcelain = (
        _porcelain_block(_WT_PATH_42, "refs/heads/baton/42")
        + _porcelain_block(_WT_PATH_99, "refs/heads/baton/99")
        + _porcelain_block(_WT_PATH_7, "refs/heads/baton/7")
    )

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            dirty_paths={_WT_PATH_99},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset({7}),
                terminal_issues=frozenset({42, 99}),
            )
        )

    assert orphans == {42}, (
        f"Only issue 42 should be an orphan; got orphans={orphans!r}"
    )


def test_empty_worktree_list_returns_empty_set() -> None:
    """An empty git worktree list produces an empty orphan set."""
    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=""),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset(),
            )
        )

    assert orphans == set(), f"Expected empty orphan set; got {orphans!r}"


def test_non_terminal_issue_not_in_running_set_is_not_orphan() -> None:
    """A worktree for an issue that is neither running nor terminal is live.

    An issue with no terminal classification (e.g. still open, awaiting
    dispatch) must not be reclaimed — it is an unknown/fresh frontier.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset(),  # 42 is NOT terminal
            )
        )

    assert 42 not in orphans, (
        "Issue 42 is not in terminal set; must not be flagged orphan "
        "(conservative: unknown → live)."
    )


# ---------------------------------------------------------------------------
# Seam 2 — cleanup_worktree called only when worktree_gc == "reclaim"
# ---------------------------------------------------------------------------


def test_cleanup_worktree_called_for_orphan_in_reclaim_mode() -> None:
    """cleanup_worktree is called for a confirmed orphan in reclaim mode."""
    porcelain = _porcelain_block(_WT_PATH_42)
    mock_cleanup = AsyncMock()

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
                worktree_gc="reclaim",
                cleanup_worktree=mock_cleanup,
            )
        )

    mock_cleanup.assert_awaited_once_with(42)


def test_cleanup_worktree_never_called_in_detect_mode() -> None:
    """cleanup_worktree is NEVER called when worktree_gc == 'detect'."""
    porcelain = _porcelain_block(_WT_PATH_42)
    mock_cleanup = AsyncMock()

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
                worktree_gc="detect",
                cleanup_worktree=mock_cleanup,
            )
        )

    mock_cleanup.assert_not_awaited()


def test_cleanup_worktree_not_called_for_live_worktree_in_reclaim_mode() -> (
    None
):
    """cleanup_worktree is NOT called for a live worktree even in reclaim mode.

    Dirty worktree + reclaim mode must not trigger cleanup (IS-5 safety).
    """
    porcelain = _porcelain_block(_WT_PATH_42)
    mock_cleanup = AsyncMock()

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            dirty_paths={_WT_PATH_42},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
                worktree_gc="reclaim",
                cleanup_worktree=mock_cleanup,
            )
        )

    mock_cleanup.assert_not_awaited()


def test_cleanup_worktree_called_only_for_confirmed_orphans_not_live() -> None:
    """In reclaim mode, cleanup only touches confirmed orphans — not live ones.

    Issue 42: terminal, clean, pushed → orphan → cleanup called.
    Issue 99: terminal, dirty → live → cleanup NOT called.
    """
    porcelain = _porcelain_block(
        _WT_PATH_42, "refs/heads/baton/42"
    ) + _porcelain_block(_WT_PATH_99, "refs/heads/baton/99")
    mock_cleanup = AsyncMock()

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            dirty_paths={_WT_PATH_99},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42, 99}),
                worktree_gc="reclaim",
                cleanup_worktree=mock_cleanup,
            )
        )

    mock_cleanup.assert_awaited_once_with(42)


# ---------------------------------------------------------------------------
# Seam 3 — alert and runlog emit for orphan_worktree event
# ---------------------------------------------------------------------------


def test_alert_called_with_warn_severity_for_orphan() -> None:
    """alert() is called with severity='warn' for each orphan candidate."""
    porcelain = _porcelain_block(_WT_PATH_42)

    mock_alert = MagicMock(return_value=True)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        with patch.object(recovery_mod, "alert", mock_alert):
            asyncio.run(
                recovery_mod.scan_orphan_worktrees(
                    owner=_OWNER,
                    repo=_REPO,
                    running_issues=frozenset(),
                    terminal_issues=frozenset({42}),
                )
            )

    assert mock_alert.called, "alert() must be called for an orphan candidate"
    _, kwargs = mock_alert.call_args
    assert kwargs.get("severity") == "warn", (
        f"Expected severity='warn'; got {kwargs.get('severity')!r}"
    )
    assert kwargs.get("kind") == "debug", (
        f"Expected kind='debug'; got {kwargs.get('kind')!r}"
    )


def test_alert_not_called_when_no_orphans() -> None:
    """alert() is not called when there are no orphan candidates."""
    porcelain = _porcelain_block(_WT_PATH_42)
    mock_alert = MagicMock(return_value=True)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        with patch.object(recovery_mod, "alert", mock_alert):
            asyncio.run(
                recovery_mod.scan_orphan_worktrees(
                    owner=_OWNER,
                    repo=_REPO,
                    running_issues=frozenset({42}),  # live
                    terminal_issues=frozenset(),
                )
            )

    mock_alert.assert_not_called()


def test_runlog_emit_called_with_orphan_worktree_event() -> None:
    """runlog.emit called with {'event': 'orphan_worktree'} for each orphan."""
    porcelain = _porcelain_block(_WT_PATH_42)
    mock_runlog = MagicMock()

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
                runlog=mock_runlog,
            )
        )

    assert mock_runlog.emit.called, "runlog.emit must be called for an orphan"
    emitted_event = mock_runlog.emit.call_args[0][0]
    assert emitted_event.get("event") == "orphan_worktree", (
        f"Expected event='orphan_worktree'; got {emitted_event!r}"
    )


def test_runlog_emit_includes_issue_number() -> None:
    """The orphan_worktree runlog event includes the issue number."""
    porcelain = _porcelain_block(_WT_PATH_42)
    mock_runlog = MagicMock()

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset({42}),
                runlog=mock_runlog,
            )
        )

    emitted_event = mock_runlog.emit.call_args[0][0]
    assert "issue" in emitted_event, (
        f"Expected 'issue' key in runlog event; got {emitted_event!r}"
    )
    assert emitted_event["issue"] == 42, (
        f"Expected issue=42 in runlog event; got {emitted_event!r}"
    )


def test_runlog_not_called_when_no_orphans() -> None:
    """runlog.emit is not called when there are no orphan candidates."""
    porcelain = _porcelain_block(_WT_PATH_42)
    mock_runlog = MagicMock()

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(porcelain_output=porcelain),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset({42}),  # live
                terminal_issues=frozenset(),
                runlog=mock_runlog,
            )
        )

    mock_runlog.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Seam 4 — sweep never raises into the caller
# ---------------------------------------------------------------------------


def test_sweep_does_not_raise_on_git_failure() -> None:
    """scan_orphan_worktrees does not raise when git worktree list fails.

    The sweep is guarded — a git failure is logged and swallowed; the
    caller never sees an exception (consistent with daemon.py:L1506 FIX-2
    pattern).
    """

    def _always_fail(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return _fail("fatal: not a git repository")

    with patch.object(recovery_mod, "_run", side_effect=_always_fail):
        # Must not raise.
        result = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                terminal_issues=frozenset(),
            )
        )

    assert isinstance(result, (set, frozenset)), (
        f"Expected a set on git failure; got {type(result)!r}"
    )
