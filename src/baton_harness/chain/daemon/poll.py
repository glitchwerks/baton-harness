"""Outer daemon loop: poll cycle, work-unit selection, startup warning.

Extracted (Phase 6e, #277, the FINAL sub-phase of the #268 module-refactor
proposal) from ``chain/daemon.py`` / ``daemon/__init__.py``. This module
owns:

* ``run_daemon`` -- the always-on outer loop: startup reconciliation,
  SIGTERM handling, the heartbeat monitor thread, and the serial
  per-repo/per-tick poll cycle (never spawns concurrent ``asyncio.Task``
  objects -- B-I3).
* ``_poll_and_run`` -- one repo's poll cycle: primary ``agent-ready``
  scan and serial work-unit drain, secondary ``agent-in-progress``
  orphan scan, and the worktree orphan-GC sweep.
* ``_select_work_unit`` -- pure selection of one ready work unit
  (milestone-first, then un-milestoned by issue number) from a list of
  ready issues.
* ``warn_if_async_escalation_unconfigured`` -- best-effort startup hint
  when neither async escalation channel (Slack webhook, heartbeat ping
  URL) is configured.

Several seams intentionally stay defined in ``daemon/__init__.py``
(``_run_gh``, ``_label_edit``, ``_fetch_issue_labels``,
``_DISPATCH_EXCLUDE_LABELS`` -- daemon-local/leaf seams; ``alert`` --
escalation re-export shared by every cluster; ``load_obs_config`` --
obs_config re-export; ``RunLog`` -- runlog re-export, needed as a live
lookup ONLY at its one construction call site, see below) or in a
sibling submodule (``_fetch_full_milestone_members`` -- gh_api_helpers.py
cluster; ``_run_work_unit`` -- work_unit.py cluster; ``_poll_and_run``
calling itself is the one case of two moved-together functions still
needing this treatment -- see below) and are reached here via
``_daemon_mod.X(...)`` -- a live attribute lookup on the parent package
module, not a captured import-time binding -- so that
``mock.patch("baton_harness.chain.daemon.X", ...)`` /
``patch.object(daemon_mod, "X", ...)`` in existing tests continues to
intercept calls made from this submodule (the "patch where it's looked
up" rule, plan §4 Phase 6 / issue #273). This applies even to
``_poll_and_run``, despite ``run_daemon`` and ``_poll_and_run`` living in
the SAME module here: ``test_daemon.py`` patches
``"baton_harness.chain.daemon._poll_and_run"`` and expects ``run_daemon``
to observe the patched replacement, which only a live lookup on the
package module (not a bare intra-module call) satisfies. ``RunLog`` is
ALSO imported directly (bare, for type annotations only -- ``runlog:
RunLog | None`` -- unaffected by which binding satisfies the type
checker); the live ``_daemon_mod.RunLog(...)`` lookup is used only at
the actual constructor call, matching ``test_daemon.py``'s
``patch.object(daemon_mod, "RunLog", ...)`` injection of a mock class.

**Special case -- ``reconcile_startup`` (RATIFIED, plan §6 Q6, issue
#277).** Unlike every other cross-boundary seam above, ``reconcile_
startup`` is imported here as a plain, direct binding from
``baton_harness.chain.reconcile`` rather than reached via
``_daemon_mod.reconcile_startup(...)``. This is deliberate: the
ratified fix for this symbol is **explicit multi-target patching**, not
a façade or a live-lookup indirection -- every fixture/test that needs
to intercept ``run_daemon``'s real startup-reconciliation call has been
repointed to this module's own binding
(``baton_harness.chain.daemon.poll.reconcile_startup``), including the
suite-wide autouse fixture at ``tests/chain/conftest.py``. ``daemon/
__init__.py`` keeps its own ``reconcile_startup`` re-export purely so
``tests/chain/test_cli.py``'s regression guard (proving ``cli.main()``
never routes through that alias) still has a valid attribute to patch --
that re-export has no production caller any more.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import baton_harness.chain.daemon as _daemon_mod
from baton_harness.chain.app_auth import InstallationTokenSource, gh_env
from baton_harness.chain.heartbeat import LivenessState, run_heartbeat_loop
from baton_harness.chain.labels import (
    LABEL_AGENT_READY,
    LABEL_BLOCKED,
    STATE_LABELS,
)
from baton_harness.chain.obs_config import ObsConfig
from baton_harness.chain.reconcile import reconcile_startup
from baton_harness.chain.recovery import scan_orphan_worktrees
from baton_harness.chain.redispatch import RedispatchTally
from baton_harness.chain.registry import RepoConfig
from baton_harness.chain.runlog import RunLog
from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.workspace import WorkspaceManager

# _gh_api_helpers_mod: imported as the submodule itself (not just a
# symbol) so run_daemon below can reset its own `_required_checks_warned`
# global directly -- mirrors the identical import in daemon/__init__.py
# prior to #277 (moved here verbatim since run_daemon is this module's
# sole caller of the reset).
from . import gh_api_helpers as _gh_api_helpers_mod

# _slugify: not test-patched anywhere (verified against the full test
# suite, issue #277) -- a plain sibling import is correct, matching the
# precedent set by launch_gate.py importing ProbeResult/ProbeDenialReason
# directly from push_probe.py (types/utilities with no patch seam).
from .gh_api_helpers import _slugify

# _DEFAULT_CI_POLL_INTERVAL / _DEFAULT_CI_TIMEOUT: not test-patched
# anywhere (verified against the full test suite, issue #277) and used
# only as run_daemon's own default *parameter values* below -- default
# values are bound once at function-definition time, so a live
# `_daemon_mod.X` lookup would not be "live" here anyway (it would just
# capture whatever `_daemon_mod` held at import time, with import-order
# fragility as a downside and no patchability upside). A direct import
# from the sole definition site is correct, mirroring the identical
# import daemon/__init__.py used before #277.
from .work_unit import _DEFAULT_CI_POLL_INTERVAL, _DEFAULT_CI_TIMEOUT

# Hard-coded: preserves the pre-split "baton_harness.chain.daemon" logger
# name (plan §3.2) rather than the submodule's own __name__, so log
# aggregation and the caplog(logger=...) assertions in test_daemon.py stay
# byte-identical pre- and post-split. See #268.
_log = logging.getLogger("baton_harness.chain.daemon")

#: Default poll interval (seconds) for the outer tick loop
#: (``_poll_and_run``). Distinct from ``_DEFAULT_CI_POLL_INTERVAL``
#: (work_unit.py), which paces the CI-gate poll inside a work unit.
_DEFAULT_POLL_INTERVAL_S: float = 30.0


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------


def warn_if_async_escalation_unconfigured(obs: ObsConfig) -> None:
    """Emit a WARNING when both async escalation channels are unconfigured.

    Checks two async failure-signal channels at daemon startup:

    - ``BH_SLACK_WEBHOOK_URL`` environment variable (unset or empty string
      counts as unconfigured).
    - ``obs.heartbeat_ping_url`` (``None`` counts as unconfigured).

    Emits a single WARNING via ``_log`` when **both** are absent.  Stays
    silent when either channel is configured.  Never raises — this is a
    best-effort startup hint only.

    Args:
        obs: Loaded observability config (used to read ``heartbeat_ping_url``).
    """
    slack_configured = bool(os.environ.get("BH_SLACK_WEBHOOK_URL"))
    ping_configured = obs.heartbeat_ping_url is not None
    if not slack_configured and not ping_configured:
        _log.warning(
            "async failure-signal escalation is unconfigured: neither"
            " BH_SLACK_WEBHOOK_URL nor BH_HEARTBEAT_PING_URL is set;"
            " an overnight stall will only surface as a GitHub comment."
            " Set one to get a push signal."
        )


# ---------------------------------------------------------------------------
# Outer daemon loop
# ---------------------------------------------------------------------------


async def run_daemon(
    config: WorkflowConfig,
    registry: list[RepoConfig],
    *,
    once: bool = False,
    poll_interval_s: float | None = None,
    ci_poll_interval: float = _DEFAULT_CI_POLL_INTERVAL,
    ci_timeout: float = _DEFAULT_CI_TIMEOUT,
    installation_token: InstallationTokenSource = "",
) -> None:
    """Run the always-on serial daemon outer loop.

    Polls the registry for ready work units and processes them one at a
    time (B-I3 serial).  Never exits on a block.

    Args:
        config: The loaded ``WorkflowConfig``.
        registry: The list of ``RepoConfig`` entries (one entry in v1).
        once: If ``True``, run exactly one tick then return (for tests
            and the ``--once`` CLI flag).
        poll_interval_s: Seconds to sleep between outer-loop ticks.
            Defaults to ``config.poll_interval_ms / 1000``.
        ci_poll_interval: Seconds between CI polls in the merge gate.
        ci_timeout: Hard ceiling for the CI gate in seconds.
        installation_token: GitHub App installation access token
            (``ghs_`` prefix).  Threaded to all ``gh`` subprocess
            calls via per-call env override.  ``os.environ`` is never
            mutated.  Pass ``""`` (default) to inherit ambient creds.
    """
    if poll_interval_s is None:
        poll_interval_s = config.poll_interval_ms / 1000

    _log.info(
        "daemon: starting (poll_interval=%.1fs, once=%s)",
        poll_interval_s,
        once,
    )

    # (issue #225) Reset the required_checks fallback-warning guard on
    # every daemon startup, so the warning fires at most once per
    # ``run_daemon`` invocation (not once per merge) while still firing
    # fresh on every new daemon run. Writes directly to the
    # gh_api_helpers submodule's own global (#274, Phase 6b) -- see the
    # `_gh_api_helpers_mod` import comment above for why a bare `global`
    # here would silently target the wrong module.
    _gh_api_helpers_mod._required_checks_warned = False

    # --- Observability startup (best-effort; risk R2 — must never raise). ---
    runlog: RunLog | None = None
    obs: ObsConfig | None = None
    tally: RedispatchTally | None = None
    try:
        obs = _daemon_mod.load_obs_config()
        warn_if_async_escalation_unconfigured(obs)  # risk R2 — never raises
        # Constructed via _daemon_mod.RunLog (a live attribute lookup, not
        # the bare `RunLog` name imported above for typing only) so that
        # test_daemon.py's `patch.object(daemon_mod, "RunLog", ...)`
        # keeps injecting a mock class here.
        runlog = _daemon_mod.RunLog(obs.runlog_path)
        runlog.emit(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "daemon_start",
                "issue": None,
                "outcome": None,
                "severity": "info",
                "detail": "daemon starting up",
                "tick_id": None,
            }
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("daemon: observability init failed: %s", exc)
        runlog = None
    try:
        if tally is None:
            obs_for_tally = _daemon_mod.load_obs_config()
            tally = RedispatchTally(
                obs_for_tally.redispatch_counts_path,
                window_ticks=obs_for_tally.redispatch_window_ticks,
                max_count=obs_for_tally.redispatch_max,
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("daemon: redispatch tally init failed: %s", exc)
        tally = None

    # --- Startup reconciliation sweep (G3 creds, G2 marker, G1 orphans). ---
    # reconcile_startup may raise SystemExit on fatal credential failure —
    # that propagates out of run_daemon intentionally (the daemon cannot
    # operate without valid credentials).  All other failures are suppressed
    # inside reconcile_startup itself.
    await reconcile_startup(
        registry,
        obs,
        runlog,
        installation_token=installation_token,
    )

    # --- SIGTERM handler (Fix 3 / PR #107): graceful shutdown clears marker.
    # Build the marker path once so both the handler and the finally block
    # reference the same path (single source of truth).
    _daemon_marker = (
        Path(registry[0].project_root) / ".baton-harness" / "daemon.alive"
    )

    def _sigterm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        """Clear the daemon.alive marker then raise SystemExit."""
        try:
            _daemon_marker.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (OSError, ValueError):
        # signal.signal may fail if called from a non-main thread (e.g.
        # certain test harnesses); degrade gracefully rather than crashing.
        pass

    # --- Heartbeat monitor setup. ----------------------------------------
    # Construct liveness state shared between the daemon and the monitor.
    #
    # LivenessState is written from the daemon (asyncio) thread and read
    # from the monitor thread; field assignments are atomic under the GIL
    # and this is best-effort liveness, so no lock is required.
    liveness_state = LivenessState()

    # Start the heartbeat OS thread unconditionally — including once=True
    # runs (--once CLI / smoke tests).  A real thread beats independently
    # of the asyncio event loop, so it continues writing heartbeats even
    # while the loop is blocked inside the synchronous CI gate
    # (time.sleep in merge.py up to 1800 s).
    stop_event = threading.Event()
    monitor_thread: threading.Thread | None = None
    if obs is not None:
        try:
            monitor_thread = threading.Thread(
                target=run_heartbeat_loop,
                args=(obs, liveness_state, stop_event),
                kwargs={"runlog": runlog},
                name="heartbeat-monitor",
                daemon=True,
            )
            monitor_thread.start()
        except Exception as exc:  # noqa: BLE001
            _log.warning("daemon: heartbeat thread startup failed: %s", exc)
            monitor_thread = None

    try:
        while True:
            # Advance the re-dispatch tally tick once per outer poll cycle
            # (before iterating repos so the tick is shared across all
            # repos in the same outer loop iteration).
            if tally is not None:
                tally.advance_tick()

            for repo_cfg in registry:
                # FIX 2: defensive catch around each per-repo tick.  A
                # failure building or running one work unit must not kill
                # the always-on daemon.  Log, escalate if possible, then
                # continue to the next repo/tick.
                try:
                    await _daemon_mod._poll_and_run(
                        config,
                        repo_cfg,
                        ci_poll_interval=ci_poll_interval,
                        ci_timeout=ci_timeout,
                        runlog=runlog,
                        tally=tally,
                        liveness_state=liveness_state,
                        obs=obs,
                        installation_token=installation_token,
                    )
                except Exception as exc:
                    _log.error(
                        "daemon: unhandled exception for %s/%s: %s; "
                        "daemon continues",
                        repo_cfg.owner,
                        repo_cfg.repo,
                        exc,
                    )
                    try:
                        _daemon_mod.alert(
                            repo_cfg.owner,
                            repo_cfg.repo,
                            None,
                            f"Daemon tick failed for {repo_cfg.owner}/"
                            f"{repo_cfg.repo}: {exc}",
                            severity="critical",
                            kind="debug",
                            runlog=runlog,
                            installation_token=installation_token,
                        )
                    except Exception:
                        pass  # escalation may fail; daemon must survive

            if once:
                break

            await asyncio.sleep(poll_interval_s)
    finally:
        # Signal the monitor thread and wait for it to exit cleanly.
        stop_event.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=5.0)
        # Clear the G2 ungraceful-exit marker on graceful shutdown so the
        # next startup does not misread a clean stop as a crash.
        try:
            _daemon_marker.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass  # best-effort; never raise in finally

    _log.info("daemon: stopped")


async def _poll_and_run(
    config: WorkflowConfig,
    repo_cfg: RepoConfig,
    *,
    ci_poll_interval: float,
    ci_timeout: float,
    runlog: RunLog | None = None,
    tally: RedispatchTally | None = None,
    liveness_state: LivenessState | None = None,
    obs: ObsConfig | None = None,
    installation_token: InstallationTokenSource = "",
) -> None:
    """Poll one repo for a ready work unit and run it if found.

    The poll cycle has three sequential phases (B-I3 serial invariant —
    no concurrent tasks are spawned):

    1. **Primary scan** — query ``agent-ready`` issues, then serially
       drain all ready work units (one at a time, each awaited to
       completion).  Before dispatching each work unit, re-fetches live
       labels for every member issue and skips any whose ``blocked`` (or
       other exclude) label was applied mid-drain.  Tracks which milestone
       numbers were processed (``processed_ms_nums``) so the secondary
       scan can dedup.

    2. **Secondary orphan scan** — query open ``agent-in-progress``
       issues.  For each orphan whose milestone was *not* already
       processed in phase 1, seed a work unit with
       ``agent_ready_issues=frozenset()`` so the existing
       ``reconstruct → redispatch-tally → liveness`` path runs for
       crash-orphaned issues that have no ``agent-ready`` sibling.
       Un-milestoned orphans are deduped by issue number.

    3. **Worktree orphan-GC sweep** — call ``scan_orphan_worktrees``
       to detect (and optionally reclaim) worktrees whose issue is
       terminal and has no live work.  Guarded: never raises into the
       daemon loop.  Mode is read from ``obs.worktree_gc`` (default
       ``"detect"``).

    Args:
        config: The loaded ``WorkflowConfig``.
        repo_cfg: The repo registry entry.
        ci_poll_interval: Seconds between CI polls.
        ci_timeout: Hard CI ceiling in seconds.
        runlog: Optional ``RunLog`` handle for best-effort event
            emission.  When ``None``, all emission is a no-op.
        tally: Optional ``RedispatchTally`` for re-dispatch loop
            detection.  When ``None``, loop detection is skipped.
        liveness_state: Optional ``LivenessState`` shared with the
            heartbeat monitor.  Passed through to ``_run_work_unit``.
        obs: Optional loaded ``ObsConfig`` used to read
            ``worktree_gc`` mode for Phase 3.  When ``None``, Phase 3
            runs in ``"detect"`` mode (no reclaim).
        installation_token: GitHub App installation access token
            (``ghs_`` prefix).  Threaded to all ``gh`` subprocess
            calls.  Pass ``""`` (default) to inherit ambient creds.
    """
    owner = repo_cfg.owner
    repo = repo_cfg.repo

    # Track which milestone numbers (and un-milestoned issue numbers)
    # were processed this cycle so the secondary scan can dedup.
    # Milestone dedup key: milestone number (int).
    # Un-milestoned dedup key: negative issue number (avoids collisions).
    processed_ms_nums: set[int] = set()
    processed_issue_nums: set[int] = set()

    # ------------------------------------------------------------------
    # Phase 1: primary agent-ready scan.
    # ------------------------------------------------------------------

    # Fetch open agent-ready issues.
    _poll_gh_env = gh_env(installation_token) if installation_token else None
    proc = _daemon_mod._run_gh(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--label",
            "agent-ready",
            "--state",
            "open",
            "--json",
            "number,title,state,body,url,labels,milestone,assignees",
            "--limit",
            "100",
        ],
        _poll_gh_env,
    )
    if proc.returncode != 0:
        _log.error(
            "daemon: gh issue list failed (exit %d): %s",
            proc.returncode,
            proc.stderr,
        )
        return

    try:
        issues_raw = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        _log.error("daemon: issue list parse error: %s", exc)
        return

    # Filter out issues that carry any dispatch-exclude label (e.g.
    # ``blocked``).  Uses the module-level ``_DISPATCH_EXCLUDE_LABELS``
    # set (owned by daemon/__init__.py, reached via a live attribute
    # lookup so `patch.object(daemon_mod, "_DISPATCH_EXCLUDE_LABELS",
    # ...)` in existing tests keeps intercepting it) so the mid-drain
    # re-check, the tick-start live re-check, and this snapshot gate all
    # share a single definition.
    ready_issues = [
        i
        for i in issues_raw
        if _daemon_mod._DISPATCH_EXCLUDE_LABELS.isdisjoint(
            {lbl["name"].lower() for lbl in i.get("labels", [])}
        )
    ]

    # Live blocked re-check (#128): the snapshot above may race with a
    # concurrent label update.  For each snapshot-ready issue, re-read
    # the live label set; if `blocked` and `agent-ready` are both live,
    # the issue is in a torn pre-dispatch state — skip it this poll cycle
    # so it is not selected as the work unit.  Treat a fetch failure
    # (None) as clean to avoid false exclusions on transient API errors.
    # The `agent-ready` guard distinguishes this pre-dispatch torn state
    # from a post-worker torn state ({agent-done, blocked}) which has no
    # `agent-ready` and must reach the post-run invariant check instead.
    ready_issues_live: list[dict[str, Any]] = []
    for issue in ready_issues:
        n = issue["number"]
        live_labels = _daemon_mod._fetch_issue_labels(
            owner,
            repo,
            n,
            installation_token=installation_token,
        )
        if (
            live_labels is not None
            and not _daemon_mod._DISPATCH_EXCLUDE_LABELS.isdisjoint(
                live_labels
            )
            and LABEL_AGENT_READY in live_labels
        ):
            _log.info(
                "daemon: #%d is live-excluded (torn snapshot); skipping"
                " this poll cycle",
                n,
            )
            # Malformed multi-state: blocked + ≥2 state labels is a
            # genuine invariant violation — page the operator.
            if len(live_labels & STATE_LABELS) >= 2:
                extra = sorted((live_labels & STATE_LABELS) - {LABEL_BLOCKED})
                _daemon_mod.alert(
                    owner,
                    repo,
                    n,
                    f"Issue #{n} has malformed multi-state labels:"
                    f" {sorted(live_labels & STATE_LABELS)!r};"
                    f" extra state labels beyond {LABEL_BLOCKED!r}:"
                    f" {extra!r} — operator action required.",
                    severity="critical",
                    kind="block",
                    installation_token=installation_token,
                )
            _daemon_mod._label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
        else:
            ready_issues_live.append(issue)
    ready_issues = ready_issues_live

    if ready_issues:
        # FIX #132: process ALL ready work units in a single tick —
        # milestoned first (lowest number), then un-milestoned by issue
        # number.  Before this fix, only ONE work unit was selected per
        # tick, silently dropping un-milestoned agent-ready issues whenever
        # a milestoned issue was also present.  With ``--once`` (smoke-run
        # mode) those un-milestoned issues were NEVER dispatched.
        #
        # Design constraint (non-negotiable): each un-milestoned issue is
        # treated as a degenerate N=1 DAG routed through the standard
        # build_dag → IssueScheduler path inside ``_run_work_unit``.
        # B-I3 serial contract is maintained: we ``await`` each work unit
        # to completion before starting the next; no concurrent tasks are
        # spawned.
        #
        # ``remaining`` is the shrinking pool of issues not yet assigned
        # to a work unit.  After each selection we remove the issues that
        # belong to the selected work unit so ``_select_work_unit`` does
        # not re-select the same milestone or issue on the next iteration.
        remaining: list[dict[str, Any]] = list(ready_issues)
        # Capture the full agent_ready_issues set once (used inside
        # _run_work_unit to distinguish actionable issues from un-greenlit
        # milestone members — same semantics as before the fix).
        all_ready_nums: frozenset[int] = frozenset(
            i["number"] for i in ready_issues
        )
        # Index of the current drain iteration (0-based).  The mid-drain
        # blocked re-check only fires on iterations > 0: the first work
        # unit was already validated by the tick-start live re-check at
        # L~1764; subsequent units may have become blocked while the
        # first unit ran.
        _drain_idx: int = 0

        while remaining:
            # Priority: milestoned first; then un-milestoned by number.
            work_unit = _select_work_unit(remaining)
            if work_unit is None:
                break
            branch_name, slug, membership, milestone_info = work_unit

            # FIX 1: Expand membership to ALL open milestone members so
            # that build_dag sees blocker edges from non-ready members.
            # An agent-ready subset as membership silently drops A→B
            # edges where A is not yet greenlit, causing B to be
            # dispatched out of dependency order.
            if milestone_info is not None:
                ms_num, ms_title = milestone_info
                full_members = _daemon_mod._fetch_full_milestone_members(
                    owner,
                    repo,
                    ms_num,
                    ms_title,
                    installation_token=installation_token,
                )
                if full_members:
                    membership = full_members
                # Record this milestone as processed so phase 2 skips it.
                processed_ms_nums.add(ms_num)
                # Remove all issues belonging to this milestone from the
                # pool so the next iteration picks a different work unit.
                remaining = [
                    i
                    for i in remaining
                    if not (
                        i.get("milestone") is not None
                        and i.get("milestone", {}).get("number") == ms_num
                    )
                ]
            else:
                # Un-milestoned N=1: track by issue number and remove from
                # pool.
                processed_issue_nums.update(membership)
                remaining = [
                    i for i in remaining if i["number"] not in membership
                ]

            # Mid-drain blocked re-check (VP-2 drain-level, #132):
            # The tick-start live re-check (L~1764) already validated ALL
            # issues before the drain loop started, so the first work unit
            # is clean.  Subsequent units may have become blocked WHILE
            # the preceding unit(s) ran — re-fetch their live labels and
            # skip if any member now carries an exclude label OR has lost
            # ``agent-ready`` (de-greenlit mid-drain).  Re-uses the same
            # ``_fetch_issue_labels`` helper and ``alert`` path as the
            # pre-dispatch gate inside ``_run_work_unit`` (L~984) — no new
            # code paths.  Single fetch per member covers both checks
            # (Codex P2, #145).
            if _drain_idx > 0:
                # Re-check live labels for members that were greenlit at
                # tick-start (``membership ∩ all_ready_nums``).  Un-greenlit
                # milestone siblings were never going to be dispatched this
                # tick — iterating the full ``membership`` would trip the
                # ``LABEL_AGENT_READY not in labels`` check on them and
                # incorrectly skip the whole work unit (Codex P2 #132).
                # Uses the same ``_DISPATCH_EXCLUDE_LABELS`` set and
                # ``agent-ready`` re-check as the tick-start gate.
                # Fail-closed: ``None`` (unreadable) also skips the unit.
                mid_drain_excluded: int | None = None
                for _md_n in membership & all_ready_nums:
                    _md_labels = _daemon_mod._fetch_issue_labels(
                        owner,
                        repo,
                        _md_n,
                        installation_token=installation_token,
                    )
                    if (
                        _md_labels is None
                        or not (
                            _daemon_mod._DISPATCH_EXCLUDE_LABELS.isdisjoint(
                                _md_labels
                            )
                        )
                        or LABEL_AGENT_READY not in _md_labels
                    ):
                        mid_drain_excluded = _md_n
                        break
                if mid_drain_excluded is not None:
                    _log.critical(
                        "daemon: work unit %s skipped — issue #%d is "
                        "excluded mid-drain (VP-2 invariant, #128/#132)",
                        slug,
                        mid_drain_excluded,
                    )
                    _daemon_mod.alert(
                        owner,
                        repo,
                        mid_drain_excluded,
                        (
                            f"Issue #{mid_drain_excluded} became excluded"
                            f" mid-drain; work unit '{slug}' skipped"
                            f" (VP-2 invariant, #128/#132)."
                        ),
                        severity="critical",
                        kind="block",
                        runlog=runlog,
                        installation_token=installation_token,
                    )
                    _drain_idx += 1
                    continue

            _drain_idx += 1
            await _daemon_mod._run_work_unit(
                config=config,
                repo_cfg=repo_cfg,
                branch_name=branch_name,
                slug=slug,
                membership=membership,
                agent_ready_issues=all_ready_nums,
                ci_poll_interval=ci_poll_interval,
                ci_timeout=ci_timeout,
                runlog=runlog,
                tally=tally,
                liveness_state=liveness_state,
                obs=obs,
                installation_token=installation_token,
            )
    else:
        _log.debug("daemon: no ready issues in %s/%s", owner, repo)

    # ------------------------------------------------------------------
    # Phase 2: secondary agent-in-progress orphan scan.
    #
    # Query open agent-in-progress issues.  For each orphan whose
    # milestone was NOT already processed in phase 1, seed a work unit
    # with agent_ready_issues=frozenset() so reconstruct/redispatch/
    # tally/liveness fire for lone crash-orphaned issues.
    # Daemon is strictly serial (B-I3) — awaits run sequentially.
    # ------------------------------------------------------------------
    orphan_proc = _daemon_mod._run_gh(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--label",
            "agent-in-progress",
            "--state",
            "open",
            "--json",
            "number,title,state,body,url,labels,milestone,assignees",
            "--limit",
            "100",
        ],
        _poll_gh_env,
    )
    if orphan_proc.returncode != 0:
        _log.warning(
            "daemon: orphan scan gh issue list failed (exit %d): %s",
            orphan_proc.returncode,
            orphan_proc.stderr,
        )
        return

    try:
        orphans_raw = json.loads(orphan_proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        _log.warning("daemon: orphan scan parse error: %s", exc)
        return

    if not orphans_raw:
        return

    # Walk each orphan; dedup by milestone number (or issue number for
    # un-milestoned).  Each milestone is seeded at most once.
    seen_orphan_ms: set[int] = set()
    seen_orphan_issues: set[int] = set()

    for orphan in orphans_raw:
        issue_num: int = orphan["number"]
        ms_info = orphan.get("milestone")

        if ms_info:
            ms_num = ms_info["number"]
            ms_title = ms_info.get("title", f"milestone-{ms_num}")

            # Skip if this milestone was already handled in phase 1 or
            # already seeded in this orphan loop.
            if ms_num in processed_ms_nums or ms_num in seen_orphan_ms:
                continue
            seen_orphan_ms.add(ms_num)

            ms_slug = _slugify(ms_title)
            orphan_branch = f"feature/{ms_slug}"
            orphan_slug = ms_slug

            # Expand membership to all open milestone members (mirrors
            # FIX 1 in phase 1).
            orphan_membership = _daemon_mod._fetch_full_milestone_members(
                owner,
                repo,
                ms_num,
                ms_title,
                installation_token=installation_token,
            )
            if not orphan_membership:
                # Fallback: at minimum include the orphan itself.
                orphan_membership = frozenset({issue_num})
        else:
            # Un-milestoned orphan: dedup by issue number.
            if (
                issue_num in processed_issue_nums
                or issue_num in seen_orphan_issues
            ):
                continue
            seen_orphan_issues.add(issue_num)

            from baton_harness.chain.branches import feature_branch_name

            orphan_branch = feature_branch_name(issue=issue_num)
            orphan_slug = f"issue-{issue_num}"
            orphan_membership = frozenset({issue_num})

        _log.info(
            "daemon: orphan scan seeding work unit %r for orphan #%d",
            orphan_slug,
            issue_num,
        )
        await _daemon_mod._run_work_unit(
            config=config,
            repo_cfg=repo_cfg,
            branch_name=orphan_branch,
            slug=orphan_slug,
            membership=orphan_membership,
            # Empty frozenset: no agent-ready issues; reconstruct/
            # redispatch classification drives the recovery path.
            agent_ready_issues=frozenset(),
            ci_poll_interval=ci_poll_interval,
            ci_timeout=ci_timeout,
            runlog=runlog,
            tally=tally,
            liveness_state=liveness_state,
            obs=obs,
            installation_token=installation_token,
        )

    # ------------------------------------------------------------------
    # Phase 3: worktree orphan-GC sweep (IS-5 detect-first).
    #
    # Scans git worktrees for entries whose issue is terminal and has no
    # live work.  Guarded: exceptions never escape to the daemon loop.
    # Mode is "detect" by default; set BH_WORKTREE_GC=reclaim to opt
    # in to cleanup.
    # ------------------------------------------------------------------
    worktree_gc_mode = obs.worktree_gc if obs is not None else "detect"
    try:
        # WorkspaceManager provides cleanup_worktree(issue_number) — the
        # same implementation used by the Orchestrator.  A fresh instance
        # is cheap (no I/O in __init__) and avoids exposing orch outside
        # _run_work_unit (B-I3 serial invariant).
        #
        # NOTE: symphony_dir is intentionally omitted here, defaulting to
        # ``<project_root>/.symphony`` (see WorkspaceManager.__init__).
        # Neither RepoConfig nor WorkflowConfig currently exposes a
        # configurable symphony dir, and the Orchestrator itself
        # constructs WorkspaceManager the same default way (see
        # vendor/symphony/orchestrator.py) — so this sweep matches the
        # rest of the daemon's assumption of the default ``.symphony``
        # layout.  If a non-default symphony_dir is ever introduced via
        # config, it must be threaded through both construction sites
        # together, or a "reclaim" sweep here would target the wrong path.
        _ws = WorkspaceManager(str(repo_cfg.project_root))
        await scan_orphan_worktrees(
            owner=owner,
            repo=repo,
            running_issues=frozenset(processed_issue_nums),
            worktree_gc=worktree_gc_mode,
            cleanup_worktree=_ws.cleanup_worktree,
            runlog=runlog,
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("daemon: worktree GC sweep raised (suppressed): %s", exc)


def _select_work_unit(
    issues: list[dict[str, Any]],
) -> tuple[str, str, frozenset[int], tuple[int, str] | None] | None:
    """Select exactly one ready work unit from the list of ready issues.

    A milestoned issue represents a milestone work unit (all members of
    that milestone form the unit).  An un-milestoned issue is its own
    N=1 unit.

    Selection priority: milestone units first (lowest milestone number),
    then un-milestoned by issue number.

    Args:
        issues: List of raw issue dicts from the ``gh issue list`` output.

    Returns:
        A ``(branch_name, slug, membership, milestone_info)`` tuple or
        ``None`` if no work unit can be determined.  ``milestone_info`` is
        a ``(milestone_number, milestone_title)`` pair for milestone work
        units, or ``None`` for un-milestoned single-issue units.  The
        caller uses ``milestone_info`` to expand ``membership`` to the full
        set of open milestone issues (FIX 1).
    """
    # Prefer milestoned issues first.
    milestoned = [i for i in issues if i.get("milestone")]
    if milestoned:
        # Pick the milestone with the lowest number.
        milestone = min(
            (i["milestone"] for i in milestoned),
            key=lambda m: m.get("number", 0),
        )
        ms_num = milestone["number"]
        ms_title = milestone.get("title", f"milestone-{ms_num}")
        ms_slug = _slugify(ms_title)
        branch_name = f"feature/{ms_slug}"
        slug = ms_slug
        # Initial membership from the agent-ready subset only.
        # _poll_and_run expands this to the full open milestone set via
        # _fetch_full_milestone_members (FIX 1).
        members = frozenset(
            i["number"]
            for i in milestoned
            if i.get("milestone", {}).get("number") == milestone["number"]
        )
        return branch_name, slug, members, (ms_num, ms_title)

    # Un-milestoned: pick the lowest issue number.
    un_milestoned = sorted(
        [i for i in issues if not i.get("milestone")],
        key=lambda i: i["number"],
    )
    if un_milestoned:
        n = un_milestoned[0]["number"]
        from baton_harness.chain.branches import feature_branch_name

        branch_name = feature_branch_name(issue=n)
        slug = f"issue-{n}"
        return branch_name, slug, frozenset({n}), None

    return None
