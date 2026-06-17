"""Unit tests for P1 worktree orphan-GC (issue #33, detect-first).

Tests the async ``scan_orphan_worktrees`` function (to be added to /
already present in ``baton_harness.chain.recovery``) and the daemon
wiring that emits ``orphan_worktree`` alerts and conditionally calls
``cleanup_worktree``.

All subprocess calls go through a patchable seam so no real git binary
or live worktree is required.

Async test functions are driven with ``asyncio.run`` so no pytest-asyncio
dependency is needed.

**New contract (P1 revision — self-determined terminal-ness):**

``scan_orphan_worktrees`` no longer accepts a ``terminal_issues``
parameter.  Instead it determines whether each worktree's issue is
terminal by fetching that issue's state from GitHub via the ``_run``
seam.  The precise gh command emitted is::

    ["gh", "issue", "view", str(N), "--repo", "owner/repo",
     "--json", "state,labels"]

The response is parsed as::

    {"state": "CLOSED" | "OPEN", "labels": [{"name": "..."}]}

Terminal predicate:
  - ``state == "CLOSED"`` → terminal (orphan-eligible, subject to live
    predicates).
  - ``state == "OPEN"`` → NOT terminal → live (conservative).
  - ``agent-done`` + open PR + OPEN state → NOT terminal (Rule 4
    ci_gate_reentry in-flight; worktree still needed).

IS-5 live predicates (safety overlay, applied regardless of terminal):
  a. Issue in ``running_issues`` → live.
  b. Issue carries ``agent-in-progress`` label → live.
  c. Worktree dirty (``git -C <wt> status --porcelain`` non-empty)
     → live.
  d. Unpushed commits (``git -C <wt> log @{u}..`` non-empty or
     non-zero returncode → no upstream → treat as unpushed) → live.

Orphan candidate = CLOSED state AND none of (a)–(d) hold.

Coverage:
- CLOSED issue, clean, pushed, not running → orphan.
- OPEN issue (clean, pushed, not running) → NOT orphan (marquee test).
- agent-done + OPEN state (open-PR ci_gate_reentry) → NOT orphan.
- A worktree whose issue is in the active running set is never an orphan.
- A worktree with the ``agent-in-progress`` label is never an orphan.
- A worktree with uncommitted changes (dirty) is never an orphan (IS-5).
- A worktree with unpushed commits is never an orphan (IS-5).
- A worktree with no upstream branch is treated as unpushed → live
  (IS-5).
- ``cleanup_worktree`` is called only when ``worktree_gc == "reclaim"``
  AND the worktree is a confirmed orphan.
- ``cleanup_worktree`` is NEVER called in ``detect`` mode.
- Alert emitted with ``severity="warn"`` and ``kind="debug"`` for orphans.
- Runlog event ``{"event": "orphan_worktree"}`` emitted for orphans.
- Sweep never raises into the caller (guarded).
"""

from __future__ import annotations

import asyncio
import json
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
    issue_states: dict[int, str] | None = None,
    issue_labels: dict[int, list[str]] | None = None,
    dirty_paths: set[str] | None = None,
    unpushed_paths: set[str] | None = None,
    no_upstream_paths: set[str] | None = None,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Return a callable suitable for patching the _run seam in recovery_mod.

    Handles all subprocess commands emitted by ``scan_orphan_worktrees``.
    The key new shape for issue-state+label queries is::

        ["gh", "issue", "view", str(N), "--repo", "<owner>/<repo>",
         "--json", "state,labels"]

    Response shape::

        {"state": "CLOSED" | "OPEN", "labels": [{"name": "<label>"}]}

    Args:
        porcelain_output: Output from ``git worktree list --porcelain``.
        issue_states: Map of issue number → ``"CLOSED"`` or ``"OPEN"``.
            Defaults to ``"OPEN"`` for any issue not listed (conservative).
        issue_labels: Map of issue number → label list.  Merged into the
            JSON response alongside the state.
        dirty_paths: Worktree paths that ``git status --porcelain`` reports
            as dirty (non-empty output).
        unpushed_paths: Worktree paths that have unpushed commits.
        no_upstream_paths: Worktree paths with no upstream configured.
    """
    issue_states = issue_states or {}
    issue_labels = issue_labels or {}
    dirty_paths = dirty_paths or set()
    unpushed_paths = unpushed_paths or set()
    no_upstream_paths = no_upstream_paths or set()

    def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        # ---- git worktree list --porcelain --------------------------------
        if (
            "worktree" in cmd_str
            and "list" in cmd_str
            and "--porcelain" in cmd_str
        ):
            return _ok(porcelain_output)
        # ---- git -C <path> status --porcelain -----------------------------
        if "status" in cmd_str and "--porcelain" in cmd_str:
            try:
                c_idx = cmd.index("-C")
                wt_path = cmd[c_idx + 1]
            except (ValueError, IndexError):
                return _ok("")
            if wt_path in dirty_paths:
                return _ok("M  some_file.py\n")
            return _ok("")
        # ---- git -C <path> log @{u}.. --oneline ---------------------------
        if "log" in cmd_str and "@{u}" in cmd_str:
            try:
                c_idx = cmd.index("-C")
                wt_path = cmd[c_idx + 1]
            except (ValueError, IndexError):
                return _ok("")
            if wt_path in no_upstream_paths:
                return _fail("fatal: no upstream configured")
            if wt_path in unpushed_paths:
                return _ok("abc1234 some unpushed commit\n")
            return _ok("")
        # ---- gh issue view <N> --repo <o>/<r> --json state,labels ---------
        # New contract: scan_orphan_worktrees fetches both state AND labels
        # in one call.  The argv shape is:
        #   ["gh", "issue", "view", str(N), "--repo", "owner/repo",
        #    "--json", "state,labels"]
        # Response: {"state": "CLOSED"|"OPEN", "labels": [{"name": "..."}]}
        if (
            len(cmd) >= 5
            and cmd[0] == "gh"
            and cmd[1] == "issue"
            and cmd[2] == "view"
            and "--json" in cmd
        ):
            json_idx = cmd.index("--json")
            json_fields = cmd[json_idx + 1] if json_idx + 1 < len(cmd) else ""
            # Accept both "state,labels" and "labels" (backwards compat for
            # _fetch_labels legacy path); only the state,labels form is used
            # by the new scan_orphan_worktrees.
            if "state" in json_fields or "labels" in json_fields:
                try:
                    issue_num = int(cmd[3])
                except (IndexError, ValueError):
                    return _ok('{"state": "OPEN", "labels": []}')
                state = issue_states.get(issue_num, "OPEN")
                names = issue_labels.get(issue_num, [])
                payload = json.dumps(
                    {
                        "state": state,
                        "labels": [{"name": n} for n in names],
                    }
                )
                return _ok(payload)
        return _ok("{}")

    return _run


# ---------------------------------------------------------------------------
# Seam 1 — scan_orphan_worktrees: terminal-ness from issue state
# ---------------------------------------------------------------------------


def test_open_issue_is_never_orphan_regardless_of_liveness() -> None:
    """An OPEN issue is never classified as an orphan — the marquee test.

    This is the highest-value IS-1-equivalent: an open/in-flight worktree
    must never be reaped, even if it is clean, pushed, and not running.
    Covers the ci_gate_reentry in-flight case (Rule 4 — agent-done + open
    PR + OPEN state) as well as any other OPEN issue.
    """
    porcelain = _porcelain_block(_WT_PATH_42, "refs/heads/baton/42")

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "OPEN"},  # explicitly OPEN
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "OPEN issue 42 must NEVER be flagged as orphan "
        "(open/in-flight worktree must never be reaped)."
    )


def test_agent_done_open_state_open_pr_is_not_orphan() -> None:
    """agent-done + OPEN state (ci_gate_reentry Rule 4) is never an orphan.

    This is the specific in-flight case from recovery.py Rule 4: the agent
    finished work but the CI gate / merge is still in progress.  The
    worktree is still needed.  OPEN state → not terminal → not orphan.
    """
    porcelain = _porcelain_block(_WT_PATH_42, "refs/heads/baton/42")

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "OPEN"},
            issue_labels={42: ["agent-done"]},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "Issue 42 is agent-done + OPEN (ci_gate_reentry in-flight); "
        "must not be flagged as orphan."
    )


def test_closed_issue_clean_pushed_not_running_is_orphan() -> None:
    """A CLOSED issue, clean, pushed, not running is an orphan candidate.

    This is the minimal positive case: CLOSED state AND no live predicates
    hold → orphan.  CLOSED = work is finished and merged.
    """
    porcelain = _porcelain_block(_WT_PATH_42, "refs/heads/baton/42")

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 in orphans, (
        f"CLOSED issue 42 (clean, pushed, not running) should be an "
        f"orphan candidate; got orphans={orphans!r}"
    )


def test_active_running_issue_is_never_orphan() -> None:
    """A worktree whose issue is in the running set is live, not an orphan.

    IS-5 predicate (a): issue in current running/membership set → live.
    Applies even when the issue is CLOSED.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset({42}),
            )
        )

    assert 42 not in orphans, (
        "Issue 42 is in the running set; must not be flagged as orphan."
    )


def test_agent_in_progress_label_makes_worktree_live() -> None:
    """A worktree whose issue carries agent-in-progress is live, not an orphan.

    IS-5 predicate (b): agent-in-progress label → live.
    Applies even when the issue is CLOSED (belt-and-suspenders safety).
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
            issue_labels={42: ["agent-in-progress"]},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "Issue 42 has agent-in-progress label; must not be flagged as orphan."
    )


def test_dirty_worktree_is_live_not_orphan() -> None:
    """A CLOSED-state worktree with uncommitted changes is classified live.

    IS-5 predicate (c): dirty tree (git status --porcelain non-empty) → live.
    This is the single highest-value safety test per the plan.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
            dirty_paths={_WT_PATH_42},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "Dirty worktree for issue 42 must not be flagged as orphan (IS-5)."
    )


def test_unpushed_commits_makes_worktree_live() -> None:
    """A CLOSED-state worktree with unpushed commits is classified live.

    IS-5 predicate (c): unpushed commits (git log @{u}.. non-empty) → live.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
            unpushed_paths={_WT_PATH_42},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "Worktree with unpushed commits for issue 42 must not be flagged "
        "orphan (IS-5)."
    )


def test_no_upstream_branch_makes_worktree_live() -> None:
    """A worktree with no upstream configured is treated as unpushed → live.

    IS-5: no upstream → treat as unpushed → live.  git log @{u}.. returns
    non-zero when no upstream is configured.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
            no_upstream_paths={_WT_PATH_42},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "Worktree with no upstream for issue 42 must be treated as "
        "unpushed → live (IS-5)."
    )


def test_mixed_worktrees_only_true_orphans_flagged() -> None:
    """Mixed pool: only the CLOSED + clean + pushed worktree is an orphan.

    Verifies the classifier handles multiple worktrees simultaneously and
    does not conflate live predicates or states across different paths.

    - Issue 42: CLOSED, clean, pushed → orphan
    - Issue 99: CLOSED, dirty → live
    - Issue 7: OPEN, in running set → live
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
            issue_states={42: "CLOSED", 99: "CLOSED", 7: "OPEN"},
            dirty_paths={_WT_PATH_99},
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset({7}),
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
            )
        )

    assert orphans == set(), f"Expected empty orphan set; got {orphans!r}"


def test_open_issue_not_in_running_set_is_not_orphan() -> None:
    """A worktree for an OPEN issue not in the running set is live.

    Conservative-live: OPEN state means not terminal.

    OPEN state → not terminal → must not be reclaimed.  Unknown/fresh
    frontier issues (state unknown or OPEN) are always kept.
    """
    porcelain = _porcelain_block(_WT_PATH_42)

    with patch.object(
        recovery_mod,
        "_run",
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "OPEN"},  # open, not running
        ),
    ):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "OPEN issue 42 (not in running set) must not be flagged orphan "
        "(conservative: OPEN → live)."
    )


def test_state_fetch_failure_treats_issue_as_live() -> None:
    """When gh issue view fails, the issue is treated as NOT terminal (live).

    A state-fetch failure must not trigger a false orphan classification.
    Conservatism: unknown state → live.
    """
    porcelain = _porcelain_block(_WT_PATH_42, "refs/heads/baton/42")

    def _seam(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "--porcelain" in cmd_str and "worktree" in cmd_str:
            return _ok(porcelain)
        # gh issue view → fail (network error / permission denied)
        if "gh" in cmd_str and "issue" in cmd_str and "view" in cmd_str:
            return _fail("error: HTTP 403")
        # git status → clean
        if "status" in cmd_str and "--porcelain" in cmd_str:
            return _ok("")
        # git log → pushed
        if "log" in cmd_str and "@{u}" in cmd_str:
            return _ok("")
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=_seam):
        orphans = asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
            )
        )

    assert 42 not in orphans, (
        "When state fetch fails for issue 42, it must be treated as live "
        "(conservative: unknown state → not orphan)."
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
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
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
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
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
            issue_states={42: "CLOSED"},
            dirty_paths={_WT_PATH_42},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
                worktree_gc="reclaim",
                cleanup_worktree=mock_cleanup,
            )
        )

    mock_cleanup.assert_not_awaited()


def test_cleanup_worktree_called_only_for_confirmed_orphans_not_live() -> None:
    """In reclaim mode, cleanup only touches confirmed orphans — not live ones.

    Issue 42: CLOSED, clean, pushed → orphan → cleanup called.
    Issue 99: CLOSED, dirty → live → cleanup NOT called.
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
            issue_states={42: "CLOSED", 99: "CLOSED"},
            dirty_paths={_WT_PATH_99},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
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
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
        ),
    ):
        with patch.object(recovery_mod, "alert", mock_alert):
            asyncio.run(
                recovery_mod.scan_orphan_worktrees(
                    owner=_OWNER,
                    repo=_REPO,
                    running_issues=frozenset(),
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
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "OPEN"},  # OPEN → live
        ),
    ):
        with patch.object(recovery_mod, "alert", mock_alert):
            asyncio.run(
                recovery_mod.scan_orphan_worktrees(
                    owner=_OWNER,
                    repo=_REPO,
                    running_issues=frozenset({42}),
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
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
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
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "CLOSED"},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset(),
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
        side_effect=_make_git_seam(
            porcelain_output=porcelain,
            issue_states={42: "OPEN"},
        ),
    ):
        asyncio.run(
            recovery_mod.scan_orphan_worktrees(
                owner=_OWNER,
                repo=_REPO,
                running_issues=frozenset({42}),  # live
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
            )
        )

    assert isinstance(result, (set, frozenset)), (
        f"Expected a set on git failure; got {type(result)!r}"
    )
