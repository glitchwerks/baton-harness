"""Always-on serial daemon: outer poll loop + per-DAG work-unit runner.

The daemon is the central orchestrator.  It:

1. Polls the repo registry for ready work units (milestoned issues with
   at least one ``agent-ready`` issue, or un-milestoned ``agent-ready``
   issues as N=1 units).
2. Serially drains **all** ready work units per tick — selects one,
   awaits it to completion, then selects the next until none remain
   (B-I3 serial contract — never spawns concurrent ``asyncio.Task``
   objects per work unit).
3. For each work unit, runs the per-DAG serial loop:
   - Builds the DAG via ``gh_deps`` + ``dag.build_dag``.
   - Uses ``IssueScheduler`` for topological ordering.
   - Applies crash / unblock recovery via ``recovery.reconstruct``.
   - Dispatches issues one at a time: ``checkout_feature_branch``,
     record cut-point, label transition (``agent-ready`` →
     ``agent-in-progress``), ``await orch._run_worker(issue)``.
   - Applies the §3.5 outcome protocol.
   - CI-gates merges via ``merge.merge_issue_branch``.
4. On work-unit completion: pushes the feature branch and opens a
   ready-for-review ``feature/<slug> → main`` PR.  The daemon NEVER
   merges to ``main``.
5. Escalates stalled issues via ``escalation.alert`` (dual-channel:
   GitHub comment + optional Slack).

Concurrency contract (B-I3):
    All work units are processed with sequential ``await`` calls.  The
    daemon NEVER spawns ``asyncio.Task`` objects for work units or for
    individual issues within a DAG.  This guarantees that the shared
    repo-root HEAD is only ever checked out to one feature branch at a
    time.

Label state machine (C1 — single writer):
    The daemon is the sole label writer during a run.  ``after_run``
    (inside ``_run_worker``) sets terminal labels; the daemon sets and
    clears ``agent-in-progress``.  ``agent-in-progress`` MUST be cleared
    on every terminal branch (success and all park paths — C-I4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# post_slack_alert: `as post_slack_alert` self-reexport (#275, Phase 6c)
# so mypy --strict's no_implicit_reexport treats
# `_daemon_mod.post_slack_alert` (launch_gate.py's patch-safe lookup of
# this name) as an explicit export rather than flagging attr-defined.
from baton_harness.chain.alert_post import (
    post_slack_alert as post_slack_alert,
)
from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    gh_env,
)

# alert: `as alert` self-reexport (#274, Phase 6b) so mypy --strict's
# no_implicit_reexport treats `_daemon_mod.alert` (gh_api_helpers.py's
# patch-safe lookup of this name, mirroring the fan-out note in plan §6
# Phase 6b) as an explicit export rather than flagging attr-defined.
from baton_harness.chain.escalation import alert as alert

# fetch_blocked_by: `as fetch_blocked_by` self-reexport (#276, Phase 6d)
# so mypy --strict's no_implicit_reexport treats
# `_daemon_mod.fetch_blocked_by` (work_unit.py's patch-safe lookup of
# this name) as an explicit export rather than flagging attr-defined.
from baton_harness.chain.gh_deps import (
    fetch_blocked_by as fetch_blocked_by,
)
from baton_harness.chain.heartbeat import LivenessState, run_heartbeat_loop

# Identity: `as Identity` self-reexport (#275, Phase 6c). Not called
# directly from this module any more (its only prior use, inside
# _build_preflight_runner, now lives in launch_gate.py), but
# test_daemon_push_probe.py accesses `daemon_mod.Identity.WORKER`
# directly (not via patch), so the import must stay here.
from baton_harness.chain.identity import Identity as Identity

# Imported under its original private name (#272): dozens of existing
# tests patch "baton_harness.chain.daemon._fetch_issue_labels" by dotted
# path, and this module's own call sites below still call the bare name
# ``_fetch_issue_labels(...)`` — both keep working unmodified against the
# relocated implementation in label_ops.py. The rename-on-import form
# (``as _fetch_issue_labels``, alias != original name) is NOT treated as
# an explicit re-export by mypy --strict's no_implicit_reexport (only
# ``as X`` with a *matching* name qualifies) -- work_unit.py's new
# ``_daemon_mod._fetch_issue_labels(...)`` live lookup (#276, Phase 6d)
# needs that, so a plain module-level assignment restates the binding
# just below the import block (see ``_fetch_issue_labels = ...`` near
# the other module-level constants) -- a plain assignment is always part
# of a module's own namespace regardless of aliasing.
from baton_harness.chain.label_ops import (
    fetch_daemon_labels as _fetch_issue_labels_impl,
)
from baton_harness.chain.labels import (
    LABEL_AGENT_READY,
    LABEL_BLOCKED,
    STATE_LABELS,
)

# assert_single_state: `as assert_single_state` self-reexport (#276,
# Phase 6d) so mypy --strict's no_implicit_reexport treats
# `_daemon_mod.assert_single_state` (work_unit.py's patch-safe lookup of
# this name) as an explicit export rather than flagging attr-defined.
from baton_harness.chain.labels import (
    assert_single_state as assert_single_state,
)

# merge_issue_branch: `as merge_issue_branch` self-reexport (#274, Phase
# 6b) so mypy --strict's no_implicit_reexport treats
# `_daemon_mod.merge_issue_branch` (gh_api_helpers.py's patch-safe
# lookup of this name) as an explicit export rather than flagging
# attr-defined.
from baton_harness.chain.merge import (
    merge_issue_branch as merge_issue_branch,
)
from baton_harness.chain.obs_config import ObsConfig, load_obs_config
from baton_harness.chain.reconcile import (
    reconcile_startup as reconcile_startup,
)
from baton_harness.chain.recovery import scan_orphan_worktrees
from baton_harness.chain.redispatch import RedispatchTally
from baton_harness.chain.registry import RepoConfig
from baton_harness.chain.ruleset_status import (
    # Neither symbol is called directly from this module any more
    # (#275, Phase 6c) -- check_ruleset_signals's only caller,
    # _should_launch_worker, now lives in launch_gate.py and reaches it
    # via _daemon_mod.check_ruleset_signals(...), a live attribute
    # lookup on THIS module -- so the import must stay here (as an
    # explicit `as` self-reexport, mypy --strict no_implicit_reexport)
    # as the binding that lookup resolves and that existing tests patch
    # (patch.object(daemon_mod, "check_ruleset_signals", ...)).
    # ruleset_is_provisioned stays bound at module scope for the same
    # patch-target reason: tests use patch.object(daemon,
    # "ruleset_is_provisioned", ...) as a regression guard proving the
    # gate never reintroduces the old call site.
    check_ruleset_signals as check_ruleset_signals,
)
from baton_harness.chain.ruleset_status import (
    ruleset_is_provisioned,  # noqa: F401
)
from baton_harness.chain.runlog import RunLog
from baton_harness.chain.subproc import run_cmd
from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.workspace import WorkspaceManager

# gh_api_helpers.py cluster (#274, Phase 6b): re-exported so
# `baton_harness.chain.daemon._slugify` / `._fetch_full_milestone_members`
# keep resolving for this module's own remaining caller (_poll_and_run)
# and for tests that access them via the package path.
# `_find_issue_pr` / `._fetch_issue_obj` / `._effective_required_checks` /
# `._run_ci_gate` / `._open_pr` have no remaining in-module caller as of
# #276 (Phase 6d) -- `_run_work_unit` (their only prior caller here) now
# lives in work_unit.py and reaches all five via `_daemon_mod.X(...)`, a
# live attribute lookup on THIS module, so each uses an explicit `as`
# self-reexport (mypy --strict no_implicit_reexport) to stay a valid
# patch/getattr target (`daemon_mod._find_issue_pr`, `patch.object(
# daemon_mod, "_open_pr", ...)`).
# `_gh_api_helpers_mod` is imported separately (not just its symbols) so
# `run_daemon` below can reset the submodule's own
# `_required_checks_warned` global directly -- a bare `global
# _required_checks_warned` in THIS module would create/target a
# different module-level name than the one `_effective_required_checks`
# reads, silently breaking the "once per run" reset (same class of bug
# flagged for `_active_probe_repo_root`, plan §6 Phase 6b/6c).
from . import gh_api_helpers as _gh_api_helpers_mod
from .gh_api_helpers import (
    _effective_required_checks as _effective_required_checks,
)
from .gh_api_helpers import (
    _fetch_full_milestone_members,
    _slugify,
)
from .gh_api_helpers import (
    _fetch_issue_obj as _fetch_issue_obj,
)
from .gh_api_helpers import (
    _find_issue_pr as _find_issue_pr,
)
from .gh_api_helpers import (
    _open_pr as _open_pr,
)
from .gh_api_helpers import (
    _run_ci_gate as _run_ci_gate,
)

# launch_gate.py cluster (#275, Phase 6c): re-exported so
# `baton_harness.chain.daemon._should_launch_worker` /
# `._build_preflight_runner` / `._resolve_app_id` / `._launch_one_issue` /
# `.reconstruct` keep resolving for tests that access/patch them via the
# package path. None have a remaining in-module caller as of #276 (Phase
# 6d): `_run_work_unit` (the prior caller of `_resolve_app_id`,
# `_launch_one_issue`, and `reconstruct`) now lives in work_unit.py and
# reaches all three via `_daemon_mod.X(...)`, a live attribute lookup on
# THIS module -- the same pattern `_should_launch_worker` and
# `_build_preflight_runner` already used since #275, when their own
# caller (`_launch_one_issue`) moved to launch_gate.py alongside them.
# Every symbol in this cluster now uses an explicit `as` self-reexport
# (mypy --strict no_implicit_reexport) to stay a valid patch/getattr
# target (`daemon_mod.reconstruct`, `patch.object(daemon_mod,
# "_resolve_app_id", ...)`, `daemon_mod._launch_one_issue(...)`).
from .launch_gate import (
    _build_preflight_runner as _build_preflight_runner,
)
from .launch_gate import (
    _launch_one_issue as _launch_one_issue,
)
from .launch_gate import (
    _resolve_app_id as _resolve_app_id,
)
from .launch_gate import (
    _should_launch_worker as _should_launch_worker,
)
from .launch_gate import (
    reconstruct as reconstruct,
)

# push_probe.py cluster (#273, Phase 6a step 2): re-exported so
# `baton_harness.chain.daemon.ProbeResult` / `.ProbeDenialReason` /
# `._authed_git_push` / `._probe_worker_push_denied` keep resolving for
# tests that access them via the package path. None have a remaining
# in-module caller as of #276 (Phase 6d): `_run_work_unit` (the prior
# caller of `_authed_git_push`) now lives in work_unit.py and reaches it
# via `_daemon_mod._authed_git_push(...)`, a live attribute lookup on
# THIS module -- the same pattern `_probe_worker_push_denied` already
# used since #275, when its own caller (`_should_launch_worker`) moved
# to launch_gate.py. Every symbol in this cluster now uses an explicit
# `as` self-reexport (mypy --strict no_implicit_reexport) to stay a
# valid patch/getattr target (`daemon_mod.ProbeResult`, `patch.object(
# daemon_mod, "_authed_git_push", ...)`).
from .push_probe import (
    ProbeDenialReason as ProbeDenialReason,
)
from .push_probe import (
    ProbeResult as ProbeResult,
)
from .push_probe import (
    _authed_git_push as _authed_git_push,
)
from .push_probe import (
    _probe_worker_push_denied as _probe_worker_push_denied,
)

# work_unit.py cluster (#276, Phase 6d): `_run_work_unit` and the two CI
# gate default constants it also uses as its own signature defaults keep
# a genuine local caller here (`_poll_and_run` calls `_run_work_unit`;
# `run_daemon` uses both defaults for its own `ci_poll_interval` /
# `ci_timeout` parameters) -- like `_slugify` / `_fetch_full_milestone_
# members` above, a plain (non-`as`) import is correct: mypy's
# no_implicit_reexport only requires the `as X` self-reexport form for
# symbols with zero remaining bare-name usage in THIS module, and
# `patch("baton_harness.chain.daemon._run_work_unit", ...)` still
# intercepts `_poll_and_run`'s call because it resolves the bare name via
# this module's own globals, which this import populates -- unaffected
# by which form (`import X` vs `import X as X`) populated it.
from .work_unit import (
    _DEFAULT_CI_POLL_INTERVAL,
    _DEFAULT_CI_TIMEOUT,
    _run_work_unit,
)

_log = logging.getLogger(__name__)

# `_fetch_issue_labels` re-bound here as a plain assignment (not an
# `import ... as` rename) -- see the label_ops import comment above for
# why: mypy --strict's no_implicit_reexport does not treat a rename-on-
# import as an explicit export, but a plain module-level assignment
# always is, satisfying both this module's own bare-name callers and
# work_unit.py's `_daemon_mod._fetch_issue_labels(...)` live lookup.
_fetch_issue_labels = _fetch_issue_labels_impl

#: Labels that disqualify an issue from dispatch.  Used by both the
#: tick-start snapshot filter, the tick-start live re-check, and the
#: mid-drain live re-check so all three gates share a single definition.
#: Currently contains only ``LABEL_BLOCKED`` (``"blocked"``); adding an
#: entry here automatically applies to all three gates.
_DISPATCH_EXCLUDE_LABELS: frozenset[str] = frozenset({LABEL_BLOCKED})


# _GENERIC_CHECKS_DETAIL, _COMPARATOR_TIMEOUT_SECONDS,
# _NonGitRepoRootSentinel, _NON_GIT_REPO_ROOT, and _active_probe_repo_root
# live in launch_gate.py (#275, Phase 6c); _should_launch_worker,
# _build_preflight_runner, _resolve_app_id, _launch_one_issue, and
# reconstruct also live there and are re-exported above purely for
# existing tests to keep resolving/patching them via the package path
# (#276, Phase 6d: none has a remaining in-module caller here any more --
# see the launch_gate.py cluster import comment above).


#: Default poll interval (seconds) for the outer tick loop
#: (``_poll_and_run``). Distinct from ``_DEFAULT_CI_POLL_INTERVAL``
#: (work_unit.py), which paces the CI-gate poll inside a work unit.
_DEFAULT_POLL_INTERVAL_S: float = 30.0

# ---------------------------------------------------------------------------
# Subprocess helper (the sole I/O seam; patch this in tests)
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external command and return its completed process.

    Centralises subprocess invocation so tests can patch a single symbol
    (spike finding F8 — hooks must be independently testable).

    Args:
        cmd: Command and arguments to execute (no shell interpolation).
        env: Optional environment dict for the subprocess.  When
            ``None``, the subprocess inherits ``os.environ`` unchanged.
            Pass ``gh_env(installation_token)`` for daemon-side calls
            to override ``GH_TOKEN`` without mutating ``os.environ``.
        timeout: Optional deadline in seconds forwarded to
            ``subprocess.run``. ``None`` (the default) means no
            deadline, matching prior behavior for existing callers.
            Raises ``subprocess.TimeoutExpired`` if the deadline
            elapses; callers that need a bounded wait (e.g. the #223
            push-denial probe) must catch it explicitly.

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr.
        Callers inspect ``returncode`` themselves.
    """
    return run_cmd(
        cmd,
        capture=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def _run_gh(
    cmd: list[str],
    gh_call_env: dict[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    """Call ``_run``, omitting ``env=`` entirely when no override applies.

    A handful of ``gh``-invoking helpers build an optional per-call
    ``env`` override via ``gh_env(installation_token)`` and only need it
    passed through when an installation token was actually supplied.
    Many test doubles patch ``_run`` with a ``cmd``-only signature, so
    always passing ``env=None`` explicitly would raise ``TypeError``
    against those stubs — omitting the kwarg when there's no override
    keeps call sites both type-safe (vs. a ``**kwargs`` unpack, which
    mypy checks against every remaining ``_run`` parameter including the
    unrelated ``timeout``) and compatible with the existing test suite.

    Args:
        cmd: Command and arguments to execute.
        gh_call_env: The env override to pass through, or ``None`` to
            call ``_run`` with no ``env=`` kwarg at all.

    Returns:
        The ``subprocess.CompletedProcess`` from ``_run``.
    """
    if gh_call_env is not None:
        return _run(cmd, env=gh_call_env)
    return _run(cmd)


# _authed_git_push, _attempt_probe_ref_cleanup, and _probe_worker_push_denied
# live in push_probe.py (#273, Phase 6a step 2) and are re-exported above
# (see that import block's comment for the current caller/patch-target
# breakdown, updated #276 Phase 6d) so work_unit.py's call site (via
# _daemon_mod) and launch_gate.py's _should_launch_worker (also via
# _daemon_mod, #275, Phase 6c) keep resolving them via the package path.


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _label_edit(
    owner: str,
    repo: str,
    issue: int,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    installation_token: InstallationTokenSource = "",
) -> None:
    """Edit labels on a GitHub issue.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.
        add: Labels to add (optional).
        remove: Labels to remove (optional).
        installation_token: Optional GitHub App installation access token.
            When non-empty, overrides ``GH_TOKEN`` in the subprocess env
            via a per-call copy — ``os.environ`` is never mutated.
    """
    cmd = ["gh", "issue", "edit", str(issue), "--repo", f"{owner}/{repo}"]
    for lbl in add or []:
        cmd += ["--add-label", lbl]
    for lbl in remove or []:
        cmd += ["--remove-label", lbl]
    _gh_call_env = gh_env(installation_token) if installation_token else None
    proc = _run_gh(cmd, _gh_call_env)
    if proc.returncode != 0:
        _log.warning(
            "daemon: gh issue edit failed for #%d (exit %d): %s",
            issue,
            proc.returncode,
            proc.stderr,
        )


# _slugify, _find_issue_pr, _fetch_issue_obj,
# _fetch_full_milestone_members, _effective_required_checks,
# _run_ci_gate, and _open_pr live in gh_api_helpers.py (#274, Phase 6b)
# and are re-exported above (see that import block's comment for the
# current per-symbol caller/patch-target breakdown, updated #276 Phase
# 6d) so this module's own remaining call site (_poll_and_run, for
# _slugify / _fetch_full_milestone_members) and existing tests keep
# resolving/patching them via the package path.


# ---------------------------------------------------------------------------
# Per-work-unit runner: _run_work_unit and its Step 1 / Step 3 helpers
# now live in work_unit.py (#276, Phase 6d); imported above and
# re-exported for _poll_and_run's local call and for tests that
# patch/access them via the package path.
# ---------------------------------------------------------------------------

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
        obs = load_obs_config()
        warn_if_async_escalation_unconfigured(obs)  # risk R2 — never raises
        runlog = RunLog(obs.runlog_path)
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
            obs_for_tally = load_obs_config()
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
                    await _poll_and_run(
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
                        alert(
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
    proc = _run_gh(
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
    # set so the mid-drain re-check and this snapshot gate stay in sync.
    ready_issues = [
        i
        for i in issues_raw
        if _DISPATCH_EXCLUDE_LABELS.isdisjoint(
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
        live_labels = _fetch_issue_labels(
            owner,
            repo,
            n,
            installation_token=installation_token,
        )
        if (
            live_labels is not None
            and not _DISPATCH_EXCLUDE_LABELS.isdisjoint(live_labels)
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
                alert(
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
            _label_edit(
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
                full_members = _fetch_full_milestone_members(
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
                    _md_labels = _fetch_issue_labels(
                        owner,
                        repo,
                        _md_n,
                        installation_token=installation_token,
                    )
                    if (
                        _md_labels is None
                        or not (
                            _DISPATCH_EXCLUDE_LABELS.isdisjoint(_md_labels)
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
                    alert(
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
            await _run_work_unit(
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
    orphan_proc = _run_gh(
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
            orphan_membership = _fetch_full_milestone_members(
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
        await _run_work_unit(
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
