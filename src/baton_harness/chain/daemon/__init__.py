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

import logging
import subprocess

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

# LABEL_AGENT_READY and STATE_LABELS moved to poll.py (#277, Phase 6e) --
# their only usage was inside _poll_and_run.  LABEL_BLOCKED stays: it
# builds _DISPATCH_EXCLUDE_LABELS below, which stays defined in THIS
# module (see that constant's own comment for why).
from baton_harness.chain.labels import LABEL_BLOCKED

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

# load_obs_config: `as load_obs_config` self-reexport (#277, Phase 6e) --
# no remaining in-module caller (run_daemon, its only prior caller, now
# lives in poll.py and reaches it via _daemon_mod.load_obs_config(...),
# a live attribute lookup on THIS module) but test_daemon.py patches
# "baton_harness.chain.daemon.load_obs_config" directly, so the explicit
# `as` form is required (mypy --strict no_implicit_reexport).
from baton_harness.chain.obs_config import load_obs_config as load_obs_config

# reconcile_startup: kept as a plain re-export here purely so
# tests/chain/test_cli.py's regression guard (proving cli.main() never
# routes through this alias) still has a valid attribute to patch. It
# has NO production caller in this package any more -- run_daemon
# (poll.py, #277) imports reconcile_startup directly from
# baton_harness.chain.reconcile rather than reaching through this
# re-export (RATIFIED explicit multi-target patching, plan §6 Q6 --
# see poll.py's module docstring "Special case" note for the full
# rationale and the list of repointed test-patch sites).
from baton_harness.chain.reconcile import (
    reconcile_startup as reconcile_startup,
)
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

# RunLog: `as RunLog` self-reexport (#277, Phase 6e). run_daemon (its
# only prior in-module caller) now lives in poll.py; test_daemon.py's
# `patch.object(daemon_mod, "RunLog", mock_runlog_cls)` (two sites)
# injects a mock class at run_daemon's construction call, which poll.py
# reaches via `_daemon_mod.RunLog(...)`, a live attribute lookup on
# THIS module -- so the explicit `as` form is required here (mypy
# --strict no_implicit_reexport).
from baton_harness.chain.runlog import RunLog as RunLog
from baton_harness.chain.subproc import run_cmd

# gh_api_helpers.py cluster (#274, Phase 6b): re-exported so
# `baton_harness.chain.daemon._fetch_full_milestone_members` keeps
# resolving for tests that access them via the package path.
# `_find_issue_pr` / `._fetch_issue_obj` / `._effective_required_checks` /
# `._run_ci_gate` / `._open_pr` have no remaining in-module caller as of
# #276 (Phase 6d) -- `_run_work_unit` (their only prior caller here) now
# lives in work_unit.py and reaches all five via `_daemon_mod.X(...)`, a
# live attribute lookup on THIS module, so each uses an explicit `as`
# self-reexport (mypy --strict no_implicit_reexport) to stay a valid
# patch/getattr target (`daemon_mod._find_issue_pr`, `patch.object(
# daemon_mod, "_open_pr", ...)`).
# `_fetch_full_milestone_members` gains the same `as` treatment as of
# #277 (Phase 6e): its prior remaining caller (_poll_and_run) now lives
# in poll.py and reaches it via `_daemon_mod._fetch_full_milestone_
# members(...)`, a live attribute lookup on THIS module, matching
# test_daemon.py's `"baton_harness.chain.daemon._fetch_full_milestone_
# members"` patch target. `_slugify` is dropped from this re-export
# block entirely (#277): it had zero remaining callers and is not
# test-patched anywhere -- poll.py imports it directly from
# `.gh_api_helpers` instead. `_gh_api_helpers_mod` (the submodule import
# used to reset `_required_checks_warned`) also moves to poll.py (#277)
# -- run_daemon, its sole user, now lives there.
from .gh_api_helpers import (
    _effective_required_checks as _effective_required_checks,
)
from .gh_api_helpers import (
    _fetch_full_milestone_members as _fetch_full_milestone_members,
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

# poll.py cluster (#277, Phase 6e -- the FINAL sub-phase of the #268
# daemon.py -> daemon/ package split): run_daemon and
# warn_if_async_escalation_unconfigured are re-exported so
# `from baton_harness.chain.daemon import run_daemon` (chain/cli.py,
# tests) and `... import warn_if_async_escalation_unconfigured`
# (test_daemon.py) keep resolving. `_poll_and_run` is also re-exported:
# test_daemon.py patches "baton_harness.chain.daemon._poll_and_run" and
# expects run_daemon (poll.py) to observe it via a live attribute
# lookup on THIS module -- see poll.py's module docstring for why this
# applies even though both functions live in the same submodule.
# `_select_work_unit` has no test-patch target or direct test access
# (grep-verified against the full suite) and is not re-exported,
# matching the precedent set by push_probe.py's
# `_attempt_probe_ref_cleanup` (private helper, no external consumer).
from .poll import _poll_and_run as _poll_and_run
from .poll import run_daemon as run_daemon

# noqa: E501 -- the self-reexport alias must match the original name
# exactly (mypy --strict no_implicit_reexport), and the name itself is
# 38 characters; no further line-wrap is available.
from .poll import (
    warn_if_async_escalation_unconfigured as warn_if_async_escalation_unconfigured,  # noqa: E501
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

# work_unit.py cluster (#276, Phase 6d): `_run_work_unit` re-exported so
# `baton_harness.chain.daemon._run_work_unit` keeps resolving for tests
# that patch/access it via the package path (test_daemon.py:9594 calls
# it directly; test_daemon.py's "baton_harness.chain.daemon._run_work_
# unit" patch target intercepts _poll_and_run's call). As of #277 (Phase
# 6e), `_poll_and_run` (its only remaining caller) now lives in poll.py
# and reaches it via `_daemon_mod._run_work_unit(...)`, a live attribute
# lookup on THIS module, so the plain (non-`as`) import used through
# #276 is upgraded to an explicit `as` self-reexport here (mypy
# --strict no_implicit_reexport requires it once no bare-name caller
# remains in this module). The two CI-gate default constants
# (`_DEFAULT_CI_POLL_INTERVAL`, `_DEFAULT_CI_TIMEOUT`) move to poll.py
# (#277) -- run_daemon, their sole user, now lives there and imports
# them directly from `.work_unit` (they are not test-patched, so no
# live-lookup indirection is needed for them).
from .work_unit import (
    _run_work_unit as _run_work_unit,
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
#:
#: Stays defined in THIS module rather than moving to poll.py with its
#: three consumers (#277, Phase 6e): test_daemon.py does
#: ``patch.object(daemon_mod, "_DISPATCH_EXCLUDE_LABELS", ...)``, which
#: only rebinds THIS module's attribute -- poll.py's `_poll_and_run`
#: reaches it via `_daemon_mod._DISPATCH_EXCLUDE_LABELS`, a live
#: attribute lookup, so that patch keeps intercepting it.
_DISPATCH_EXCLUDE_LABELS: frozenset[str] = frozenset({LABEL_BLOCKED})


# _GENERIC_CHECKS_DETAIL, _COMPARATOR_TIMEOUT_SECONDS,
# _NonGitRepoRootSentinel, _NON_GIT_REPO_ROOT, and _active_probe_repo_root
# live in launch_gate.py (#275, Phase 6c); _should_launch_worker,
# _build_preflight_runner, _resolve_app_id, _launch_one_issue, and
# reconstruct also live there and are re-exported above purely for
# existing tests to keep resolving/patching them via the package path
# (#276, Phase 6d: none has a remaining in-module caller here any more --
# see the launch_gate.py cluster import comment above).
#
# _DEFAULT_POLL_INTERVAL_S moves to poll.py (#277, Phase 6e) -- its own
# docstring already scoped it to "the outer tick loop", which now lives
# there alongside its only (currently unused -- pre-existing, not
# introduced by #277) reference.

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


# _find_issue_pr, _fetch_issue_obj, _fetch_full_milestone_members,
# _effective_required_checks, _run_ci_gate, and _open_pr live in
# gh_api_helpers.py (#274, Phase 6b) and are re-exported above (see that
# import block's comment for the current per-symbol caller/patch-target
# breakdown, updated #277 Phase 6e) so existing tests keep resolving/
# patching them via the package path. This module has no remaining bare
# caller of any of them; _slugify (never test-patched) is not
# re-exported at all -- poll.py imports it directly from
# .gh_api_helpers instead.


# ---------------------------------------------------------------------------
# Per-work-unit runner: _run_work_unit and its Step 1 / Step 3 helpers
# live in work_unit.py (#276, Phase 6d); imported above and re-exported
# for tests that patch/access it via the package path. poll.py's
# _poll_and_run (#277, Phase 6e) reaches it via
# `_daemon_mod._run_work_unit(...)`, a live attribute lookup on THIS
# module.
#
# The outer poll loop itself (run_daemon, _poll_and_run,
# _select_work_unit, warn_if_async_escalation_unconfigured) lives in
# poll.py (#277, Phase 6e) and is re-exported above, alongside the
# other cluster import blocks.
# ---------------------------------------------------------------------------
