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
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baton_harness.chain import branches
from baton_harness.chain import recovery as _recovery_mod
from baton_harness.chain.alert_post import post_slack_alert
from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    gh_env,
)
from baton_harness.chain.dag import build_dag
from baton_harness.chain.escalation import alert
from baton_harness.chain.gh_deps import (
    fetch_blocked_by,
)
from baton_harness.chain.heartbeat import LivenessState, run_heartbeat_loop
from baton_harness.chain.identity import Identity, env_for
from baton_harness.chain.labels import (
    LABEL_AGENT_READY,
    LABEL_BLOCKED,
    STATE_LABELS,
    assert_single_state,
    target_state_from_observed,
)
from baton_harness.chain.merge import (
    REQUIRED_CHECKS,
    MergeOutcome,
    merge_issue_branch,
)
from baton_harness.chain.obs_config import ObsConfig, load_obs_config
from baton_harness.chain.reconcile import (
    reconcile_startup as reconcile_startup,
)
from baton_harness.chain.recovery import RecoveryResult, scan_orphan_worktrees
from baton_harness.chain.redispatch import RedispatchTally
from baton_harness.chain.registry import RepoConfig
from baton_harness.chain.ruleset_status import (
    RulesetStatus,
    check_ruleset_signals,
    # Not called from this module (#206 hard swap moved the gate to
    # check_ruleset_signals in _should_launch_worker) -- stays bound at
    # module scope so tests can patch.object(daemon,
    # "ruleset_is_provisioned", ...) as a regression guard proving the
    # gate never reintroduces the old call site.
    ruleset_is_provisioned,  # noqa: F401
)
from baton_harness.chain.runlog import RunLog
from baton_harness.chain.scheduler import IssueScheduler
from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.orchestrator import Orchestrator
from baton_harness.vendor.symphony.tracker import Issue
from baton_harness.vendor.symphony.workspace import WorkspaceManager

_log = logging.getLogger(__name__)

#: Labels that disqualify an issue from dispatch.  Used by both the
#: tick-start snapshot filter, the tick-start live re-check, and the
#: mid-drain live re-check so all three gates share a single definition.
#: Currently contains only ``LABEL_BLOCKED`` (``"blocked"``); adding an
#: entry here automatically applies to all three gates.
_DISPATCH_EXCLUDE_LABELS: frozenset[str] = frozenset({LABEL_BLOCKED})


_GENERIC_CHECKS_DETAIL = (
    "harness-main-no-merge, harness-feature-daemon-only "
    "(ruleset check returned no detail)"
)

_PUSH_DENIAL_SIGNALS: tuple[str, ...] = (
    "403",
    "protected",
    "declined",
    "refusing to allow",
    "gh006",
)

# Serial-launch probe context for issue #223. `_launch_one_issue` sets this
# around its call to `_should_launch_worker`, then resets it in a `finally`
# block so direct callers of `_should_launch_worker` keep the legacy path.
_active_probe_repo_root: Path | None = None


def _should_launch_worker(
    issue_number: int,
    owner: str,
    repo: str,
    *,
    app_id: str,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
    obs: ObsConfig,
) -> bool:
    """Per-launch branch-protection preflight gate.

    Calls ``check_ruleset_signals`` (#206 hard swap — the App-token-safe
    replacement for ``ruleset_is_provisioned``, which a GitHub App
    installation token cannot use safely because it can't read
    ``bypass_actors``) and returns ``True`` only when the returned
    ``RulesetCheckResult.status`` is ``RulesetStatus.MATCH``.  On any
    non-MATCH result (``DRIFT``, ``ABSENT``, ``ERROR``, or
    ``NOT_PROVISIONED``) the worker launch is refused (returns
    ``False``) and a Slack alert is sent to ``obs.heartbeat_ping_url``
    if configured.  ``NOT_PROVISIONED`` (no pinned baseline) fails
    closed exactly like the other non-MATCH statuses — a missing
    baseline must never be treated as safe-by-default.

    The alert's failed-checks text is built directly from the single
    ``RulesetCheckResult`` already returned by ``check_ruleset_signals``
    — this function never calls it a second time to re-derive detail.

    Failure-isolation: if ``post_slack_alert`` raises despite its
    no-raise contract, the exception is caught and logged; the refusal
    still fires.

    Args:
        issue_number: Issue number about to be dispatched (for log context).
        owner: Repository owner (org or user login).
        repo: Repository name.
        app_id: Numeric GitHub App ID (string form) for placeholder
            substitution in the feature ruleset.
        runner: gh runner callable for the ruleset inspector.
        obs: Observability config; ``obs.heartbeat_ping_url`` is used as
            the Slack webhook target.

    Returns:
        ``True`` when the preflight passes (MATCH); ``False`` otherwise.
    """
    result = check_ruleset_signals(owner, repo, app_id=app_id, runner=runner)

    if _active_probe_repo_root is not None:
        if result.status is not RulesetStatus.MATCH:
            _log.warning(
                "daemon: comparator reported %s for issue #%d; "
                "treating as diagnostic-only while push probe decides",
                result.status.name,
                issue_number,
            )

        try:
            denied = _probe_worker_push_denied(_active_probe_repo_root)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "daemon: push-denial probe raised for issue #%d; "
                "failing closed: %s",
                issue_number,
                exc,
            )
            denied = False

        if denied:
            return True

        checks_detail = result.detail or _GENERIC_CHECKS_DETAIL
        message = (
            "baton-harness refusing to launch worker — "
            "push-denial probe did not confirm denial "
            f"(probe_denied={denied!r}; comparator={result.status.name}). "
            "Will NOT run in dangerous mode. "
            f"Failed checks: {checks_detail}."
        )

        if obs.heartbeat_ping_url is None:
            _log.warning(
                "daemon: no heartbeat_ping_url configured; "
                "Slack preflight alert not sent for issue #%d",
                issue_number,
            )
        else:
            try:
                post_slack_alert(obs.heartbeat_ping_url, message)
            except Exception:  # noqa: BLE001
                _log.warning(
                    "daemon: post_slack_alert raised despite no-raise "
                    "contract (issue #%d); swallowing",
                    issue_number,
                )
        return False

    if result.status is RulesetStatus.MATCH:
        return True

    checks_detail = result.detail or _GENERIC_CHECKS_DETAIL

    message = (
        "baton-harness refusing to launch worker — "
        "main branch protection missing/misconfigured. "
        "Will NOT run in dangerous mode. "
        f"Failed checks: {checks_detail}."
    )

    _log.warning(
        "daemon: preflight refused issue #%d — %s (%s)",
        issue_number,
        result.status.name,
        checks_detail,
    )

    if obs.heartbeat_ping_url is None:
        _log.warning(
            "daemon: no heartbeat_ping_url configured; "
            "Slack preflight alert not sent for issue #%d",
            issue_number,
        )
    else:
        try:
            post_slack_alert(obs.heartbeat_ping_url, message)
        except Exception:  # noqa: BLE001
            _log.warning(
                "daemon: post_slack_alert raised despite no-raise "
                "contract (issue #%d); swallowing",
                issue_number,
            )

    return False


def _build_preflight_runner(
    installation_token: InstallationTokenSource,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Build a gh runner with the correct auth environment for preflight.

    Uses ``chain.app_auth.gh_env(installation_token)`` when an
    installation token is provided; otherwise falls back to the worker
    identity environment from ``env_for(Identity.WORKER)``. Passes
    ``env=...`` to ``subprocess.run`` so ``gh`` authenticates with the
    intended per-call identity. This matches the pattern used by
    ``gh_deps``, ``escalation``, ``merge``, and ``recovery`` elsewhere
    in ``chain/``.

    Without this, a bare ``subprocess.run(["gh", ...])`` passes no env
    override, so ruleset ``gh api`` calls authenticate via ambient
    credentials.  In deployments without an ambient ``GH_TOKEN``, every
    launch is refused with ``RulesetStatus.ERROR``.

    Args:
        installation_token: GitHub App installation access token
            (``ghs_`` prefix, or a refreshable token-source object) to
            inject as ``GH_TOKEN`` / ``GITHUB_TOKEN`` in the subprocess
            environment via ``gh_env``.

    Returns:
        A callable that accepts a list of gh args and returns a
        ``CompletedProcess[str]`` with ``env`` set to the resolved App
        token environment when ``installation_token`` is truthy, or to
        ``env_for(Identity.WORKER)`` when it is falsy or absent.
    """
    _env = (
        gh_env(installation_token)
        if installation_token
        else env_for(Identity.WORKER)
    )

    def _runner(
        args: list[str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_env,
        )

    return _runner


def _resolve_app_id() -> str | None:
    """Resolve the GitHub App ID from the environment.

    Reads ``BH_GITHUB_APP_ID`` from the environment.  Returns ``None``
    (fail-closed) if the variable is absent or empty, logging a critical
    message so operators are informed.

    Returns:
        The App ID string if available; ``None`` otherwise.
    """
    try:
        app_id = os.environ["BH_GITHUB_APP_ID"]
    except KeyError:
        _log.critical(
            "daemon: BH_GITHUB_APP_ID is not set; "
            "refusing to launch worker (fail-closed)"
        )
        return None
    if not app_id:
        _log.critical(
            "daemon: BH_GITHUB_APP_ID is empty; "
            "refusing to launch worker (fail-closed)"
        )
        return None
    return app_id


async def _launch_one_issue(
    orch: Orchestrator,
    issue_obj: object,
    owner: str,
    repo: str,
    app_id: str,
    installation_token: InstallationTokenSource,
    obs: ObsConfig,
    *,
    repo_root: Path | None = None,
) -> str | None:
    """Preflight + launch helper extracted from the daemon's launch loop.

    Builds a token-authenticated gh runner via
    ``_build_preflight_runner(installation_token)`` so that ruleset ``gh
    api`` calls authenticate as the harness App rather than relying on
    ambient credentials (P1 fix).

    On preflight refusal, restores the ``agent-ready`` label so the issue
    remains visible to future poll ticks and posts a blocking comment via
    ``alert(severity="critical")`` (P2a fix).

    Returns the worker result string ("pr_created" / "no_pr" / etc.) on
    success; returns ``None`` when preflight refuses (parked).

    Args:
        orch: The Orchestrator instance to dispatch the worker through.
        issue_obj: Issue object with a ``.number`` attribute.
        owner: Repository owner (org or user login).
        repo: Repository name.
        app_id: Numeric GitHub App ID (string form) for ruleset checks.
        installation_token: GitHub App installation access token
            (``ghs_`` prefix).  Used to build the preflight runner so
            ruleset checks authenticate as the App, not ambient env.
        obs: Observability config for preflight alert routing.
        repo_root: Repository root used by the decisive worker-identity
            push probe. Defaults to ``Path.cwd()`` when omitted. When an
            explicit path is supplied but does not point at a git
            worktree (no ``.git`` entry), the decisive push probe is
            disabled but the launch still passes through
            ``_should_launch_worker``'s fail-closed comparator gate.

    Returns:
        The worker result string on success, or ``None`` when preflight
        refuses the launch.
    """
    global _active_probe_repo_root

    issue_number: int = issue_obj.number  # type: ignore[attr-defined]
    resolved_repo_root = repo_root or Path.cwd()
    has_git_dir = (resolved_repo_root / ".git").exists()

    preflight_runner = _build_preflight_runner(installation_token)
    _active_probe_repo_root = resolved_repo_root if has_git_dir else None
    try:
        preflight = _should_launch_worker(
            issue_number,
            owner,
            repo,
            app_id=app_id,
            runner=preflight_runner,
            obs=obs,
        )
    finally:
        _active_probe_repo_root = None
    if not preflight:
        # P2a: restore agent-ready so the issue stays visible to future
        # poll ticks (protection may be restored later).
        _label_edit(
            owner,
            repo,
            issue_number,
            add=["agent-ready"],
            remove=["agent-in-progress"],
            installation_token=installation_token,
        )
        # Post a blocking comment so operators know why the worker was
        # refused.
        alert(
            owner,
            repo,
            issue_number,
            "preflight refused — branch protection missing or "
            "misconfigured; worker not launched",
            severity="critical",
            installation_token=installation_token,
        )
        return None
    return await orch._run_worker(issue_obj)  # type: ignore[arg-type]


def reconstruct(
    repo_root: Path,
    owner: str,
    repo: str,
    branch_name: str,
    membership: frozenset[int],
    *,
    installation_token: InstallationTokenSource = "",
) -> RecoveryResult:
    """Thin dispatch wrapper for ``recovery.reconstruct``.

    Delegates to ``_recovery_mod.reconstruct`` via a module-level
    attribute lookup so that tests can patch either this symbol
    (``"baton_harness.chain.daemon.reconstruct"``) or the upstream
    symbol (``"baton_harness.chain.recovery.reconstruct"``) and both
    will affect the call site.

    Args:
        repo_root: Absolute ``Path`` to the repository root.
        owner: GitHub repository owner.
        repo: GitHub repository name.
        branch_name: Feature branch name (``"feature/<slug>"``).
        membership: Set of issue numbers in the work unit.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  Forwarded to ``recovery.reconstruct`` for
            env-discipline gh calls.

    Returns:
        A ``RecoveryResult`` describing the daemon's prior state for
        this work unit.
    """
    return _recovery_mod.reconstruct(
        repo_root,
        owner,
        repo,
        branch_name,
        membership,
        installation_token=installation_token,
    )


# Default poll configuration for the CI gate (overridable by caller).
_DEFAULT_CI_POLL_INTERVAL: float = 10.0
_DEFAULT_CI_TIMEOUT: float = 1800.0
_DEFAULT_POLL_INTERVAL_S: float = 30.0

# Claude attribution line for GitHub PR bodies (CLAUDE.md § GitHub Comments).
_CLAUDE_ATTRIBUTION = (
    "\n\n🤖 *Generated by Claude Code on behalf of @cbeaulieu-gt*"
)

# ---------------------------------------------------------------------------
# Subprocess helper (the sole I/O seam; patch this in tests)
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    env: dict[str, str] | None = None,
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

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr.
        Callers inspect ``returncode`` themselves.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _authed_git_push(
    repo_root: Path,
    branch_name: str,
    installation_token: InstallationTokenSource,
) -> subprocess.CompletedProcess[str]:
    """Push ``branch_name`` to origin, authed as the App installation.

    A "daemon-only" feature ruleset (bypass actor = the App) rejects a
    push authenticated as the ambient user PAT, so the push must
    instead inject the installation token via an env var and override
    the git credential helper inline (issue #220) — the raw token is
    never placed in argv/URL or persisted to ``.git/config``, where it
    would be visible in process listings, shell history, or disk.

    Args:
        repo_root: Path to the git worktree to push from.
        branch_name: Name of the branch to push to origin.
        installation_token: GitHub App installation access token (or a
            refreshable provider).  Falsy values fall back to a bare
            ``git push`` for non-App (PAT-only) deploys.

    Returns:
        The ``subprocess.CompletedProcess`` from the push invocation.
    """
    if not installation_token:
        return _run(
            ["git", "-C", str(repo_root), "push", "origin", branch_name]
        )

    push_env = gh_env(installation_token)
    return _run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "credential.https://github.com.helper=!f() { "
            "echo username=x-access-token; "
            'echo "password=$GH_INSTALLATION_TOKEN"; '
            "}; f",
            "push",
            "origin",
            branch_name,
        ],
        env=push_env,
    )


def _probe_worker_push_denied(repo_root: Path) -> bool:
    """Probe whether worker-identity pushes are correctly denied.

    Attempts a best-effort push of ``HEAD`` to a unique throwaway
    ``feature/`` ref using the worker identity. A recognized denial means
    the protection boundary is intact and launch is safe. Any accepted,
    indeterminate, or exception outcome fails closed.

    Args:
        repo_root: Repository root to run the probe push from.

    Returns:
        ``True`` when the push was denied with a recognizable protection
        signal. ``False`` when the push was accepted, indeterminate, or
        raised.
    """
    probe_ref = f"feature/__bh-probe-{uuid.uuid4().hex[:12]}"
    worker_env = env_for(Identity.WORKER)
    push_cmd = [
        "git",
        "-C",
        str(repo_root),
        "push",
        "origin",
        f"HEAD:refs/heads/{probe_ref}",
    ]

    try:
        push_result = _run(push_cmd, env=worker_env)
    except Exception as exc:  # noqa: BLE001
        _log.warning("daemon: push-denial probe transport failure: %s", exc)
        return False

    if push_result.returncode == 0:
        cleanup_cmd = [
            "git",
            "-C",
            str(repo_root),
            "push",
            "origin",
            "--delete",
            probe_ref,
        ]
        try:
            _run(cleanup_cmd, env=worker_env)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "daemon: probe cleanup delete failed for %s: %s",
                probe_ref,
                exc,
            )
        return False

    stderr_lower = (push_result.stderr or "").lower()
    if any(signal in stderr_lower for signal in _PUSH_DENIAL_SIGNALS):
        return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _slugify(title: str) -> str:
    """Convert a milestone title to a URL/git-ref safe slug.

    Rules:
    - Lowercase.
    - Spaces and underscores → ``-``.
    - Strip to ``[A-Za-z0-9._/-]``.
    - Leading non-alphanumeric chars are removed.

    Args:
        title: The milestone title.

    Returns:
        A kebab-case slug safe for use as a git ref component.
    """
    import re

    slug = title.lower()
    slug = slug.replace(" ", "-").replace("_", "-")
    # Strip unsafe characters.
    slug = re.sub(r"[^A-Za-z0-9._/-]", "", slug)
    # Strip leading non-alphanumeric.
    slug = re.sub(r"^[^A-Za-z0-9]+", "", slug)
    return slug or "milestone"


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
    _env_kw: dict[str, dict[str, str]] = (
        {"env": _gh_call_env} if _gh_call_env is not None else {}
    )
    proc = _run(cmd, **_env_kw)
    if proc.returncode != 0:
        _log.warning(
            "daemon: gh issue edit failed for #%d (exit %d): %s",
            issue,
            proc.returncode,
            proc.stderr,
        )


def _find_issue_pr(
    owner: str,
    repo: str,
    issue: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> tuple[str | None, str | None]:
    """Find an open PR's head branch and SHA for a given issue number.

    Looks for a PR whose head branch matches ``baton/*-<N>`` pattern
    (mirrors ``tracker.check_pr_exists`` heuristic).

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number to search for.
        installation_token: Optional GitHub App installation access token.
            When non-empty, overrides ``GH_TOKEN`` in the subprocess env
            via a per-call copy — ``os.environ`` is never mutated.

    Returns:
        A ``(branch_name, head_sha)`` tuple if found, or
        ``(None, None)`` if no matching PR exists.
    """
    _gh_call_env = gh_env(installation_token) if installation_token else None
    _env_kw: dict[str, dict[str, str]] = (
        {"env": _gh_call_env} if _gh_call_env is not None else {}
    )
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            "open",
            "--json",
            "number,headRefName,headRefOid",
            "--limit",
            "100",
        ],
        **_env_kw,
    )
    if proc.returncode != 0:
        _log.warning(
            "daemon: gh pr list failed (exit %d): %s",
            proc.returncode,
            proc.stderr,
        )
        return None, None

    try:
        prs = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return None, None

    suffix = f"-{issue}"
    for pr in prs:
        head = str(pr.get("headRefName", ""))
        if head.startswith("baton/") and head.endswith(suffix):
            sha = str(pr.get("headRefOid", ""))
            return head, sha

    return None, None


def _fetch_issue_labels(
    owner: str,
    repo: str,
    issue: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> set[str] | None:
    """Fetch current labels for an issue (lowercase).

    Returns ``None`` on any fetch failure so callers can distinguish
    an unreadable state from a genuinely empty label set.  This mirrors
    the sentinel pattern used by ``after_run._current_labels`` (#32).

    On a ``gh`` call failure (``returncode != 0``) the issue may still
    carry ``blocked`` or other state labels that we cannot see — returning
    ``None`` forces the caller to handle the unknown state conservatively
    rather than treating it as zero-state and triggering convergence.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.
        installation_token: Optional GitHub App installation access token.
            When non-empty, overrides ``GH_TOKEN`` in the subprocess env
            via a per-call copy — ``os.environ`` is never mutated.

    Returns:
        A ``set[str]`` of lowercase label name strings when the fetch
        succeeds (possibly empty — a genuine empty set is distinct from
        failure).  ``None`` when the ``gh`` call returns a non-zero exit
        code or when the response cannot be parsed (``JSONDecodeError``,
        ``KeyError``, or ``TypeError``).  Callers must guard on ``None``
        and must NOT attempt single-state convergence on an unknown state
        (Codex P1 #3, PR #95).
    """
    _gh_call_env = gh_env(installation_token) if installation_token else None
    _env_kw: dict[str, dict[str, str]] = (
        {"env": _gh_call_env} if _gh_call_env is not None else {}
    )
    proc = _run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "labels",
        ],
        **_env_kw,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return {lbl["name"].lower() for lbl in data.get("labels", [])}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _fetch_issue_obj(
    owner: str,
    repo: str,
    issue_number: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> Issue | None:
    """Fetch a single issue as an ``Issue`` dataclass.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue_number: The issue number.
        installation_token: Optional GitHub App installation access token.
            When non-empty, overrides ``GH_TOKEN`` in the subprocess env
            via a per-call copy — ``os.environ`` is never mutated.

    Returns:
        An ``Issue`` object or ``None`` on error.
    """
    _gh_call_env = gh_env(installation_token) if installation_token else None
    _env_kw: dict[str, dict[str, str]] = (
        {"env": _gh_call_env} if _gh_call_env is not None else {}
    )
    proc = _run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number,title,state,body,url,labels,assignees",
        ],
        **_env_kw,
    )
    if proc.returncode != 0:
        _log.warning(
            "daemon: gh issue view #%d failed (exit %d): %s",
            issue_number,
            proc.returncode,
            proc.stderr,
        )
        return None
    try:
        raw = json.loads(proc.stdout)
        # gh issue view returns a single object (not a list).
        return Issue.from_gh(raw)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        _log.warning("daemon: issue #%d parse error: %s", issue_number, exc)
        return None


def _fetch_full_milestone_members(
    owner: str,
    repo: str,
    milestone_number: int,
    milestone_title: str,
    *,
    installation_token: InstallationTokenSource = "",
) -> frozenset[int]:
    """Fetch all OPEN issues for a milestone — the full DAG membership set.

    Uses ``gh issue list --milestone <title> --state open`` (the CLI
    matches by title string, not by number).  Closed milestone issues are
    intentionally excluded: a closed blocker is a satisfied blocker, so
    its dependents correctly become ready.

    This is distinct from the ``agent-ready`` subset returned by the poll
    query.  The full set is required so that ``build_dag`` sees all
    blocker edges — if a non-ready member A blocks a ready member B and A
    is excluded from membership, the edge A→B is dropped and B is
    incorrectly dispatched before A completes.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        milestone_number: The milestone integer ID (used as fallback only).
        milestone_title: The milestone title string (used for the CLI
            ``--milestone`` filter).
        installation_token: Optional GitHub App installation access token.
            When non-empty, overrides ``GH_TOKEN`` in the subprocess env
            via a per-call copy — ``os.environ`` is never mutated.

    Returns:
        A ``frozenset`` of all open issue numbers in the milestone.
        Falls back to an empty frozenset on error.
    """
    _gh_call_env = gh_env(installation_token) if installation_token else None
    _env_kw: dict[str, dict[str, str]] = (
        {"env": _gh_call_env} if _gh_call_env is not None else {}
    )
    proc = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--milestone",
            milestone_title,
            "--state",
            "open",
            "--json",
            "number,title,state,body,url,labels,milestone,assignees",
            "--limit",
            "200",
        ],
        **_env_kw,
    )
    if proc.returncode != 0:
        _log.warning(
            "daemon: gh issue list --milestone %r failed (exit %d): %s; "
            "falling back to agent-ready subset",
            milestone_title,
            proc.returncode,
            proc.stderr,
        )
        return frozenset()
    try:
        issues_raw = json.loads(proc.stdout)
        return frozenset(i["number"] for i in issues_raw)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        _log.warning(
            "daemon: milestone member parse error for %r: %s; "
            "falling back to agent-ready subset",
            milestone_title,
            exc,
        )
        return frozenset()


# (issue #225) Guard for the required_checks fallback warning -- fires
# at most once per ``run_daemon`` invocation (reset at the top of
# run_daemon), not once per merge/work-unit within that run.
_required_checks_warned = False


def _effective_required_checks(config: WorkflowConfig) -> list[str]:
    """Resolve the merge gate's required-check set from ``config``.

    Design decision (fallback + warn, issue #225): an operator-set
    ``config.required_checks`` overrides the hardcoded default.  When
    unset (empty list), falls back to ``merge.REQUIRED_CHECKS`` and logs
    a startup WARNING (once per daemon run) so operators know they are
    relying on the hardcoded default.

    Args:
        config: The loaded ``WorkflowConfig`` for the current run.

    Returns:
        The required check names to gate merges on: ``config
        .required_checks`` when non-empty, else ``merge.REQUIRED_CHECKS``.
    """
    if config.required_checks:
        return config.required_checks

    global _required_checks_warned
    if not _required_checks_warned:
        _log.warning(
            "daemon: required_checks not set in config/WORKFLOW.md;"
            " falling back to hardcoded REQUIRED_CHECKS default %r ---"
            " set `required_checks:` in config/WORKFLOW.md to override",
            REQUIRED_CHECKS,
        )
        _required_checks_warned = True
    return REQUIRED_CHECKS


def _run_ci_gate(
    *,
    owner: str,
    repo: str,
    n: int,
    issue_branch: str,
    pr_head_sha: str,
    repo_root: Any,  # noqa: ANN401
    branch_name: str,
    sched: Any,  # noqa: ANN401
    liveness_state: LivenessState | None,
    runlog: RunLog | None,
    merged_issues: list[int],
    parked_reasons: dict[int, str],
    ci_poll_interval: float,
    ci_timeout: float,
    required_checks: list[str] | None = None,
    installation_token: InstallationTokenSource = "",
) -> None:
    """Run the CI gate and apply the merge/park terminal for one issue.

    Shared entry point for both the normal ``pr_created`` path and the
    converged path.  Callers supply the ``(issue_branch, pr_head_sha)``
    observation directly — no second ``_find_issue_pr`` call is made
    inside this helper.

    Handles every terminal outcome:

    * ``MergeOutcome.MERGED`` — removes labels, clears liveness, calls
      ``sched.mark_done``, appends to ``merged_issues``.
    * Any other ``MergeOutcome`` (CI_FAILED, CI_TIMEOUT, CONFLICT) —
      removes labels, clears liveness, calls ``sched.mark_parked``,
      fires ``alert``.
    * Exception from ``merge_issue_branch`` — same park path with the
      exception message in the park reason.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.
        n: Issue number being processed.
        issue_branch: The PR head branch name (e.g.
            ``"baton/issue-10-10"``).
        pr_head_sha: The PR head commit SHA.
        repo_root: Absolute ``Path`` to the repository root.
        branch_name: The feature branch name for the work unit; used for
            logging and forwarded to ``merge_issue_branch`` as the
            ``feature_branch`` argument.
        sched: The ``IssueScheduler`` instance for this work unit.
        liveness_state: Optional ``LivenessState`` shared with the
            heartbeat monitor; cleared on every terminal branch (C-I4).
        runlog: Optional ``RunLog`` handle for best-effort event
            emission.
        merged_issues: Mutable list accumulating merged issue numbers.
        parked_reasons: Mutable dict accumulating park reasons.
        ci_poll_interval: Seconds between CI status polls.
        ci_timeout: Hard ceiling for the CI gate in seconds.
        required_checks: The required check names for the merge gate
            (issue #225) -- the configured override, or the hardcoded
            ``merge.REQUIRED_CHECKS`` default; see
            ``_effective_required_checks``.  Defaults to ``None``
            (forwarded to ``merge_issue_branch`` as-is, which falls back
            to ``REQUIRED_CHECKS`` itself) for callers that predate
            issue #225.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  Threaded to ``_label_edit`` calls.
    """
    try:
        outcome = merge_issue_branch(
            repo_root=repo_root,
            owner=owner,
            repo=repo,
            issue=n,
            pr_head_sha=pr_head_sha,
            issue_branch=issue_branch,
            feature_branch=branch_name,
            required=required_checks,
            poll_interval=ci_poll_interval,
            timeout=ci_timeout,
            installation_token=installation_token,
        )
    except Exception as exc:
        _log.error(
            "daemon: merge_issue_branch raised for #%d: %s; parking",
            n,
            exc,
        )
        _label_edit(
            owner,
            repo,
            n,
            remove=["agent-in-progress"],
            installation_token=installation_token,
        )
        if liveness_state is not None:
            liveness_state.clear()
        sched.mark_parked(n)
        parked_reasons[n] = f"merge exception: {exc}"
        alert(
            owner,
            repo,
            n,
            f"Issue #{n} merge raised an exception: {exc}",
            severity="warn",
            kind="debug",
            runlog=runlog,
            installation_token=installation_token,
        )
        return

    if outcome == MergeOutcome.MERGED:
        # merge_issue_branch already added agent-merged + marker.
        _label_edit(
            owner,
            repo,
            n,
            remove=["agent-in-progress", "agent-done"],
            installation_token=installation_token,
        )
        if liveness_state is not None:
            liveness_state.clear()
        sched.mark_done(n)
        merged_issues.append(n)
        _log.info("daemon: issue #%d merged into %r", n, branch_name)
    else:
        _label_edit(
            owner,
            repo,
            n,
            remove=["agent-in-progress"],
            installation_token=installation_token,
        )
        if liveness_state is not None:
            liveness_state.clear()
        sched.mark_parked(n)
        parked_reasons[n] = f"CI gate: {outcome.name}"
        reason = (
            "CI check failed"
            if outcome == MergeOutcome.CI_FAILED
            else "CI timed out"
            if outcome == MergeOutcome.CI_TIMEOUT
            else "merge conflict"
        )
        alert(
            owner,
            repo,
            n,
            f"Issue #{n} parked: {reason} ({outcome.name}).",
            severity="critical",
            kind="debug",
            runlog=runlog,
            installation_token=installation_token,
        )


def _open_pr(
    owner: str,
    repo: str,
    branch_name: str,
    title: str,
    body: str,
    *,
    installation_token: InstallationTokenSource = "",
) -> None:
    """Open a ready-for-review PR from ``branch_name`` → main.

    The daemon NEVER merges to main — this function only creates a PR.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        branch_name: The feature branch to open the PR from.
        title: The PR title.
        body: The PR body (including attribution line).
        installation_token: Optional GitHub App installation access token.
            When non-empty, overrides ``GH_TOKEN`` in the subprocess env
            via a per-call copy — ``os.environ`` is never mutated.
    """
    _gh_call_env = gh_env(installation_token) if installation_token else None
    _env_kw: dict[str, dict[str, str]] = (
        {"env": _gh_call_env} if _gh_call_env is not None else {}
    )
    proc = _run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            f"{owner}/{repo}",
            "--base",
            "main",
            "--head",
            branch_name,
            "--title",
            title,
            "--body",
            body,
        ],
        **_env_kw,
    )
    if proc.returncode != 0:
        # "already exists" is OK — the PR already tracks this branch.
        if "already exists" in proc.stderr or "already exists" in proc.stdout:
            _log.info(
                "daemon: PR for %r already exists; skipping create",
                branch_name,
            )
        else:
            _log.warning(
                "daemon: gh pr create failed for %r (exit %d): %s",
                branch_name,
                proc.returncode,
                proc.stderr,
            )
    else:
        _log.info("daemon: PR opened for %r → main", branch_name)


# ---------------------------------------------------------------------------
# Per-work-unit runner
# ---------------------------------------------------------------------------


async def _run_work_unit(  # noqa: C901 (acceptable complexity)
    config: WorkflowConfig,
    repo_cfg: RepoConfig,
    branch_name: str,
    slug: str,
    membership: frozenset[int],
    *,
    agent_ready_issues: frozenset[int] | None = None,
    ci_poll_interval: float = _DEFAULT_CI_POLL_INTERVAL,
    ci_timeout: float = _DEFAULT_CI_TIMEOUT,
    runlog: RunLog | None = None,
    tally: RedispatchTally | None = None,
    liveness_state: LivenessState | None = None,
    obs: ObsConfig | None = None,
    installation_token: InstallationTokenSource = "",
) -> None:
    """Run one work unit (one DAG) to completion.

    Sequential ``await`` calls only — no concurrent ``asyncio.Task``
    objects (B-I3 serial contract).

    Args:
        config: The loaded ``WorkflowConfig``.
        repo_cfg: The repo registry entry.
        branch_name: The feature branch name (``"feature/<slug>"``).
        slug: The bare slug (without ``"feature/"`` prefix).
        membership: The full set of open milestone issue numbers (FIX 1).
            For un-milestoned single-issue units this is ``{N}``.
        agent_ready_issues: The subset of ``membership`` that currently
            carry the ``agent-ready`` label.  Only issues in this set are
            dispatched to the worker; others are treated as
            "waiting-for-greenlight" and skipped without escalation.
            Defaults to ``membership`` when ``None`` (backward-compat for
            un-milestoned units where membership IS the ready set).
        ci_poll_interval: Seconds between CI polls in the merge gate.
        ci_timeout: Hard ceiling for the CI gate in seconds.
        runlog: Optional ``RunLog`` handle for best-effort event
            emission.  When ``None``, all emission is a no-op.
        tally: Optional ``RedispatchTally`` for re-dispatch loop
            detection.  When ``None``, loop detection is skipped.
        liveness_state: Optional ``LivenessState`` shared with the
            heartbeat monitor.  When provided, this function calls
            ``mark_in_progress`` before dispatching a worker and
            ``clear`` on every terminal branch (C-I4).
        obs: Optional observability config.  Threaded to
            ``_launch_one_issue`` for branch-protection preflight
            alerting.  When ``None``, preflight refuses to launch
            (fail-closed — ``app_id`` cannot be resolved).
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, all ``gh`` subprocess
            calls use a per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated.
    """
    owner = repo_cfg.owner
    repo = repo_cfg.repo
    repo_root = repo_cfg.project_root

    # (issue #225) Resolve the merge gate's required-check set once for
    # this work unit -- config override, or the hardcoded default (with
    # a one-time warning); see _effective_required_checks.
    required_checks = _effective_required_checks(config)

    # FIX 1: default agent_ready_issues to membership when caller did not
    # supply it (un-milestoned N=1 unit where membership IS the ready set).
    if agent_ready_issues is None:
        agent_ready_issues = membership

    # --- Step 0: build the DAG and prepare the scheduler. ---
    blocked_by: dict[int, list[int]] = {}
    for m in membership:
        blocked_by[m] = fetch_blocked_by(
            owner, repo, m, installation_token=installation_token
        )

    dag = build_dag(membership, blocked_by)
    sched = IssueScheduler(dag.graph)
    try:
        sched.prepare()
    except Exception as exc:
        # FIX 4: CycleError is a recoverable escalated condition — warning,
        # not error.
        _log.warning(
            "daemon: CycleError in work unit %r: %s; skipping", slug, exc
        )
        alert(
            owner,
            repo,
            next(iter(membership)),
            f"Cyclic dependency detected in work unit '{slug}': {exc}",
            severity="warn",
            kind="block",
            runlog=runlog,
            installation_token=installation_token,
        )
        return

    # --- Step 1: create or resume the feature branch. ---
    branches.create_feature_branch(repo_root, branch_name, exist_ok=True)

    # Issue #67 / PR #69 (Codex P1): publish the feature branch to origin
    # NOW, before any worker/agent runs.  The agent's WORKFLOW.md uses
    #   gh pr create --base "$BH_FEATURE_BRANCH"
    # which requires the base branch to already exist on the remote.  The
    # completion push at Step 3 below publishes merge commits at unit end,
    # but for a fresh work unit `origin/<branch_name>` does not yet exist
    # when the first _run_worker executes.  An idempotent early push fixes
    # the ordering: re-runs where the branch is already on origin are no-ops
    # (git exits 0 for up-to-date / fast-forwardable pushes).
    early_push = _authed_git_push(repo_root, branch_name, installation_token)
    if early_push.returncode != 0:
        _log.warning(
            "daemon: early git push %r to origin failed (exit %d): %s",
            branch_name,
            early_push.returncode,
            early_push.stderr,
        )

    # Determine if we are resuming (branch existed before
    # create_feature_branch).  Recovery is idempotent on a fresh branch.
    recovery_result = reconstruct(
        repo_root,
        owner,
        repo,
        branch_name,
        membership,
        installation_token=installation_token,
    )

    # --- Step 2: per-DAG serial loop. ---
    merged_issues: list[int] = []
    parked_reasons: dict[int, str] = {}

    pending: list[int] = []
    seen: set[int] = set()

    # Orchestrator instance (one per work unit).
    state_path = str(repo_root / ".symphony" / "state.json")
    orch = Orchestrator(
        config=config,
        project_root=str(repo_root),
        state_path=state_path,
    )

    # P2 (#33): inject progress callback so per-turn liveness can detect a
    # hung worker.  The callback is best-effort: a callback exception is
    # logged and swallowed inside the vendored VP-3 guard and never crashes
    # the worker run.  last_progress_at is initialised by the FIRST call
    # at turn-loop entry — NOT before await orch._run_worker (IS-1).
    if liveness_state is not None:
        _ls_ref = liveness_state  # capture for closure

        def _progress_cb(
            issue_number: int,  # noqa: ARG001
            turn: int,  # noqa: ARG001
        ) -> None:
            """Update last_progress_at on each turn-loop entry."""
            _ls_ref.note_progress(datetime.now(timezone.utc))

        orch.progress_cb = _progress_cb  # type: ignore[assignment]

    # FIX 4: set BH_VENV once before the loop.  If it is already set by
    # the launcher, leave it; if unset, derive from the running interpreter
    # so hooks can self-activate the venv.
    if not os.environ.get("BH_VENV"):
        # sys.executable is e.g. /path/to/.venv/Scripts/python.exe;
        # the venv root is one level above the bin/Scripts dir.
        venv_root = str(
            __import__("pathlib").Path(sys.executable).parent.parent
        )
        os.environ["BH_VENV"] = venv_root

    while sched.is_active():
        for n in sched.get_ready():
            if n not in seen:
                seen.add(n)
                pending.append(n)

        # Remove parked from pending.
        pending = [n for n in pending if n not in sched.parked]

        if not pending:
            break  # Fully parked or nothing ready.

        # FIX 1: partition pending into actionable and non-actionable.
        # "Actionable" = recovery-case OR currently carries agent-ready.
        # "Non-actionable" = full milestone member that has NOT been
        # greenlit yet (agent-ready not set by human).  Non-actionable
        # issues are skipped silently — they will be picked up in a
        # future poll tick once the human adds agent-ready.
        recovery_actionable = {
            m
            for m in pending
            if m in recovery_result.done
            or m in recovery_result.parked_seed
            or m in recovery_result.ci_gate_reentry
            or m in recovery_result.redispatch
        }
        dispatch_actionable = {m for m in pending if m in agent_ready_issues}
        actionable = recovery_actionable | dispatch_actionable

        if not actionable:
            # Only un-greenlit milestone members remain in the frontier.
            # Exit the work unit cleanly (open the PR below) without
            # escalating.  The outer poll loop will re-trigger this milestone
            # on the next tick once the human labels more issues agent-ready.
            _log.info(
                "daemon: work unit %r: frontier has only un-greenlit members"
                " %s; exiting cleanly to wait for human greenlight",
                slug,
                sorted(pending),
            )
            break

        n = pending.pop(0)  # Serial: exactly one issue at a time.

        # FIX 1: if this issue is non-actionable, skip it (leave it
        # undispatched so the loop can continue processing actionable siblings
        # that were also in the pending list).
        if n not in actionable:
            _log.debug(
                "daemon: #%d not yet agent-ready; skipping this pass", n
            )
            continue

        # --- Recovery seeding. ---
        if n in recovery_result.done:
            sched.mark_done(n)
            merged_issues.append(n)
            continue

        if n in recovery_result.parked_seed:
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            sched.mark_parked(n)
            parked_reasons[n] = "blocked (recovery)"
            continue

        if n in recovery_result.ci_gate_reentry:
            # Rule 3a: re-enter CI gate without _run_worker.
            issue_branch, pr_head_sha = _find_issue_pr(
                owner,
                repo,
                n,
                installation_token=installation_token,
            )
            if issue_branch is None or pr_head_sha is None:
                _log.warning(
                    "daemon: ci_gate_reentry for #%d but no open PR found;"
                    " parking",
                    n,
                )
                _label_edit(
                    owner,
                    repo,
                    n,
                    remove=["agent-in-progress"],
                    installation_token=installation_token,
                )
                sched.mark_parked(n)
                parked_reasons[n] = "ci_gate_reentry: no open PR"
                alert(
                    owner,
                    repo,
                    n,
                    f"Issue #{n} needs CI-gate re-entry but has no open PR.",
                    severity="critical",
                    kind="debug",
                    runlog=runlog,
                    installation_token=installation_token,
                )
                continue

            # Liveness tracking: mark in-progress so heartbeat_monitor
            # can detect a stall during the CI poll (which may block up
            # to ci_timeout=1800s).  worker_active=False: no worker turns
            # occur during a CI gate wait, so the progress predicate must
            # NOT fire here (IS-1).
            if liveness_state is not None:
                liveness_state.mark_in_progress(
                    owner,
                    repo,
                    n,
                    datetime.now(timezone.utc),
                    worker_active=False,
                )

            # FIX 2: wrap merge_issue_branch in a per-issue try/except so a
            # transient git/gh error parks this issue but does not kill the
            # daemon.
            try:
                outcome = merge_issue_branch(
                    repo_root=repo_root,
                    owner=owner,
                    repo=repo,
                    issue=n,
                    pr_head_sha=pr_head_sha,
                    issue_branch=issue_branch,
                    feature_branch=branch_name,
                    poll_interval=ci_poll_interval,
                    timeout=ci_timeout,
                    required=required_checks,
                    installation_token=installation_token,
                )
            except Exception as exc:
                _log.error(
                    "daemon: merge_issue_branch raised for #%d (ci_gate"
                    "_reentry): %s; parking",
                    n,
                    exc,
                )
                _label_edit(
                    owner,
                    repo,
                    n,
                    remove=["agent-in-progress"],
                    installation_token=installation_token,
                )
                if liveness_state is not None:
                    liveness_state.clear()
                sched.mark_parked(n)
                parked_reasons[n] = f"merge exception (ci_gate): {exc}"
                alert(
                    owner,
                    repo,
                    n,
                    f"Issue #{n} merge failed (ci_gate_reentry): {exc}",
                    severity="warn",
                    kind="debug",
                    runlog=runlog,
                    installation_token=installation_token,
                )
                continue

            if outcome == MergeOutcome.MERGED:
                _label_edit(
                    owner,
                    repo,
                    n,
                    remove=["agent-in-progress", "agent-done"],
                    installation_token=installation_token,
                )
                if liveness_state is not None:
                    liveness_state.clear()
                sched.mark_done(n)
                merged_issues.append(n)
            else:
                _label_edit(
                    owner,
                    repo,
                    n,
                    remove=["agent-in-progress"],
                    installation_token=installation_token,
                )
                if liveness_state is not None:
                    liveness_state.clear()
                sched.mark_parked(n)
                parked_reasons[n] = f"ci_gate_reentry: {outcome.name}"
                alert(
                    owner,
                    repo,
                    n,
                    f"Issue #{n} CI-gate re-entry failed: {outcome.name}",
                    severity="critical",
                    kind="debug",
                    runlog=runlog,
                    installation_token=installation_token,
                )
            continue

        # --- Fresh dispatch (or 3b redispatch). ---
        if n in recovery_result.redispatch:
            # Re-dispatch loop detection (#77): record this attempt and
            # check whether it breaches the configured threshold.
            if tally is not None and tally.record_and_check(n):
                # Threshold breached — park the issue without dispatching.
                _log.warning(
                    "daemon: redispatch loop detected for #%d; parking",
                    n,
                )
                detail = (
                    f"Issue #{n} hit the re-dispatch loop threshold "
                    f"(window={tally.window_ticks} ticks, "
                    f"max={tally.max_count}). "
                    "Parking to prevent infinite crash-restart cycle."
                )
                if runlog is not None:
                    try:
                        runlog.emit(
                            {
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "event": "redispatch_loop",
                                "issue": n,
                                "outcome": None,
                                "severity": "critical",
                                "detail": detail,
                                "tick_id": None,
                            }
                        )
                    except Exception:  # noqa: BLE001
                        pass
                alert(
                    owner,
                    repo,
                    n,
                    f"Issue #{n} hit the re-dispatch loop threshold"
                    " — parked to prevent infinite crash-restart cycle.",
                    severity="critical",
                    kind="block",
                    runlog=runlog,
                    installation_token=installation_token,
                )
                _label_edit(
                    owner,
                    repo,
                    n,
                    remove=["agent-in-progress"],
                    installation_token=installation_token,
                )
                sched.mark_parked(n)
                parked_reasons[n] = "redispatch loop"
                continue

            # Clear orphan agent-in-progress before re-dispatch (C1).
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            _log.info("daemon: cleared orphan agent-in-progress for #%d", n)

        # Fail-closed blocked-gate (#128 P2a): re-read live labels
        # immediately before dispatch.  If the fetch fails (None), we
        # cannot confirm the issue is unblocked — skip it this cycle
        # conservatively rather than risk dispatching a blocked issue.
        # Distinct from the post-run None guard (L~1087): that guard
        # fires *after* the worker; this one fires *before*, so the
        # worker is never called.  Self-heals on the next poll tick.
        _pre_dispatch_labels = _fetch_issue_labels(
            owner,
            repo,
            n,
            installation_token=installation_token,
        )
        if _pre_dispatch_labels is None:
            _log.info(
                "daemon: #%d label fetch failed before dispatch;"
                " skipping this poll cycle (fail-closed, #128 P2a)",
                n,
            )
            alert(
                owner,
                repo,
                n,
                (
                    f"Issue #{n} labels unreadable before dispatch;"
                    " skipping this poll cycle."
                ),
                severity="critical",
                kind="block",
                runlog=runlog,
                installation_token=installation_token,
            )
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = "label fetch failed pre-dispatch"
            continue

        # Checkout feature branch (HEAD = feature branch, §3.4).
        branches.checkout_feature_branch(repo_root, slug)
        # Record cut-point (§3.7).
        cut_point = branches.record_cut_point(repo_root, slug)

        # Label transition: remove agent-ready, add agent-in-progress (C1).
        _label_edit(
            owner,
            repo,
            n,
            add=["agent-in-progress"],
            remove=["agent-ready"],
            installation_token=installation_token,
        )
        # Liveness tracking: record that this issue is now in-progress so
        # heartbeat_monitor can detect a stall.  worker_active=True enables
        # the per-turn progress-stall predicate (P2 / IS-1).
        if liveness_state is not None:
            liveness_state.mark_in_progress(
                owner,
                repo,
                n,
                datetime.now(timezone.utc),
                worker_active=True,
            )

        # Thread cut-point base to hooks via env (VP-1 wiring).
        os.environ["CHAIN_BASE_BRANCH"] = cut_point
        # Thread feature branch name to agent env so WORKFLOW.md step 4 can
        # use --base "$BH_FEATURE_BRANCH" in gh pr create (issue #67).
        os.environ["BH_FEATURE_BRANCH"] = branch_name

        # Fetch the Issue object.
        issue_obj = _fetch_issue_obj(
            owner, repo, n, installation_token=installation_token
        )
        if issue_obj is None:
            _log.error("daemon: could not fetch issue #%d; parking", n)
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = "issue fetch failed"
            alert(
                owner,
                repo,
                n,
                f"Issue #{n} could not be fetched; worker not dispatched.",
                severity="warn",
                kind="debug",
                runlog=runlog,
                installation_token=installation_token,
            )
            continue

        # Dispatch the worker.
        # Emit dispatch event (best-effort; never raises into the loop).
        if runlog is not None:
            try:
                runlog.emit(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "event": "dispatch",
                        "issue": n,
                        "outcome": None,
                        "severity": "info",
                        "detail": f"dispatching worker for issue #{n}",
                        "tick_id": None,
                    }
                )
            except Exception:  # noqa: BLE001
                pass
        # Branch-protection preflight: resolve app_id and obs before launch.
        # Both are required; either missing → fail-closed (park + continue).
        _app_id = _resolve_app_id()
        if _app_id is None or obs is None:
            _log.critical(
                "daemon: preflight cannot run for #%d — "
                "app_id=%r, obs=%s; refusing to launch (fail-closed)",
                n,
                _app_id,
                "set" if obs is not None else "None",
            )
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = "preflight refused"
            continue
        try:
            worker_result = await _launch_one_issue(
                orch,
                issue_obj,
                owner,
                repo,
                _app_id,
                installation_token,
                obs,
                repo_root=repo_root,
            )
        except Exception as exc:
            _log.error("daemon: _run_worker raised for #%d: %s", n, exc)
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = f"worker exception: {exc}"
            alert(
                owner,
                repo,
                n,
                f"Issue #{n} worker raised an exception: {exc}",
                severity="warn",
                kind="debug",
                runlog=runlog,
                installation_token=installation_token,
            )
            continue

        # Preflight refused: _launch_one_issue returns None when
        # _should_launch_worker denies launch.  Park + continue.
        if worker_result is None:
            _log.warning("daemon: preflight refused issue #%d; parking", n)
            # Labels (restore agent-ready, remove agent-in-progress) are
            # handled inside _launch_one_issue's refusal branch — outer
            # loop only needs to park the scheduler state.
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = "preflight refused"
            continue

        # Emit outcome event (best-effort; never raises into the loop).
        if runlog is not None:
            try:
                runlog.emit(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "event": "outcome",
                        "issue": n,
                        "outcome": str(worker_result),
                        "severity": "info",
                        "detail": (
                            f"worker for issue #{n} returned {worker_result!r}"
                        ),
                        "tick_id": None,
                    }
                )
            except Exception:  # noqa: BLE001
                pass

        # Re-read labels after _run_worker (after_run may have set blocked).
        post_labels = _fetch_issue_labels(
            owner,
            repo,
            n,
            installation_token=installation_token,
        )

        # Guard: None sentinel means the gh call failed or stdout was
        # unparsable.  The single-state invariant CANNOT be verified and
        # convergence MUST NOT fire on an unknown state (Codex P1 #3,
        # PR #95).  Take the conservative path: park + alert.
        if post_labels is None:
            _log.error(
                "daemon: label fetch failed for #%d"
                " (gh error / unparsable); parking conservatively",
                n,
            )
            if runlog is not None:
                try:
                    runlog.emit(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "event": "label_fetch_failed",
                            "issue": n,
                            "outcome": None,
                            "severity": "critical",
                            "detail": (
                                f"Issue #{n} labels unreadable;"
                                " cannot verify single-state invariant"
                            ),
                            "tick_id": None,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
            alert(
                owner,
                repo,
                n,
                (
                    f"Issue #{n} labels unreadable; cannot verify"
                    " single-state invariant — parking."
                ),
                severity="critical",
                kind="block",
                runlog=runlog,
                installation_token=installation_token,
            )
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = "labels unreadable"
            continue

        has_blocked = "blocked" in post_labels

        # Single-state invariant backstop (#34 P2 / #76).
        # Must run BEFORE the outcome-protocol branches so torn or zero-state
        # label sets are caught early rather than dispatched to logic that
        # assumes a clean state.
        _inv_violation = assert_single_state(post_labels)
        if _inv_violation is not None:
            # Convergence path (#31 P1 / #96): zero state labels + open PR +
            # not blocked → re-derive target and apply it instead of
            # parking.  This handles the torn-state window where a 60s
            # kill between after_run's remove-agent-ready and
            # add-agent-done leaves the issue in {agent-in-progress}
            # only.
            _state_labels_present = post_labels & set(
                ["agent-ready", "agent-done", "blocked"]
            )
            _zero_state = len(_state_labels_present) == 0
            if _zero_state and not has_blocked:
                _conv_branch, _conv_sha = _find_issue_pr(
                    owner,
                    repo,
                    n,
                    installation_token=installation_token,
                )
                if _conv_branch is not None:
                    # Definite completion evidence: derive target via
                    # the pure helper (avoids hard-coding "agent-done").
                    _target = target_state_from_observed(
                        blocked=False, pr_open=True
                    )
                    _log.warning(
                        "daemon: backstop converging #%d to %r"
                        " (zero state labels + open PR); skipping park",
                        n,
                        _target,
                    )
                    # Remove only the labels that are actually present to
                    # keep the edit idempotent.
                    _remove = ["agent-in-progress"] + [
                        lbl for lbl in _state_labels_present if lbl != _target
                    ]
                    _label_edit(
                        owner,
                        repo,
                        n,
                        add=[_target],
                        remove=_remove,
                        installation_token=installation_token,
                    )
                    if runlog is not None:
                        try:
                            runlog.emit(
                                {
                                    "ts": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                    "event": "label_invariant_converged",
                                    "issue": n,
                                    "outcome": None,
                                    "severity": "warning",
                                    "detail": (
                                        f"backstop converged #{n}"
                                        f" to {_target!r}:"
                                        f" {_inv_violation}"
                                    ),
                                    "tick_id": None,
                                }
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    # Do NOT mark_parked; do NOT fire critical alert.
                    # Reuse the convergence observation (no TOCTOU second
                    # _find_issue_pr call) and route directly through the
                    # shared CI-gate helper (#96 redesign).
                    # NOTE: do NOT clear liveness before _run_ci_gate —
                    # the CI gate (merge_issue_branch) can block for
                    # minutes; clearing early blinds the heartbeat stall
                    # monitor.  Every CI-gate terminal path clears
                    # liveness at its own exit point (Refs #31 P2).
                    assert _conv_sha is not None  # _conv_branch is not None
                    _run_ci_gate(
                        owner=owner,
                        repo=repo,
                        n=n,
                        issue_branch=_conv_branch,
                        pr_head_sha=_conv_sha,
                        repo_root=repo_root,
                        branch_name=branch_name,
                        sched=sched,
                        liveness_state=liveness_state,
                        runlog=runlog,
                        merged_issues=merged_issues,
                        parked_reasons=parked_reasons,
                        ci_poll_interval=ci_poll_interval,
                        ci_timeout=ci_timeout,
                        required_checks=required_checks,
                        installation_token=installation_token,
                    )
                    continue
            # No convergence target found (no open PR, or blocked):
            # invariant violated — park + alert (existing behavior).
            _log.error(
                "daemon: label invariant violated for #%d: %s; parking",
                n,
                _inv_violation,
            )
            if runlog is not None:
                try:
                    runlog.emit(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "event": "label_invariant_violation",
                            "issue": n,
                            "outcome": None,
                            "severity": "critical",
                            "detail": _inv_violation,
                            "tick_id": None,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
            alert(
                owner,
                repo,
                n,
                (
                    f"Issue #{n} failed the single-state"
                    f" label invariant: {_inv_violation}"
                ),
                severity="critical",
                kind="block",
                runlog=runlog,
                installation_token=installation_token,
            )
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = f"label invariant violation: {_inv_violation}"
            continue

        # Apply §3.5 outcome protocol.
        if worker_result == "pr_created" and not has_blocked:
            # Normal CI gate: locate the PR once, then delegate to the
            # shared merge entry point (_run_ci_gate).
            issue_branch, pr_head_sha = _find_issue_pr(
                owner,
                repo,
                n,
                installation_token=installation_token,
            )
            if issue_branch is None or pr_head_sha is None:
                _log.warning(
                    "daemon: pr_created but no open PR found for #%d; parking",
                    n,
                )
                _label_edit(
                    owner,
                    repo,
                    n,
                    remove=["agent-in-progress"],
                    installation_token=installation_token,
                )
                if liveness_state is not None:
                    liveness_state.clear()
                sched.mark_parked(n)
                parked_reasons[n] = "pr_created but no PR located"
                alert(
                    owner,
                    repo,
                    n,
                    f"Issue #{n} returned pr_created but no PR found.",
                    severity="warn",
                    kind="debug",
                    runlog=runlog,
                    installation_token=installation_token,
                )
                continue

            _run_ci_gate(
                owner=owner,
                repo=repo,
                n=n,
                issue_branch=issue_branch,
                pr_head_sha=pr_head_sha,
                repo_root=repo_root,
                branch_name=branch_name,
                sched=sched,
                liveness_state=liveness_state,
                runlog=runlog,
                merged_issues=merged_issues,
                parked_reasons=parked_reasons,
                ci_poll_interval=ci_poll_interval,
                ci_timeout=ci_timeout,
                required_checks=required_checks,
                installation_token=installation_token,
            )
        else:
            # Park path: blocked or no_pr.
            kind = "block" if has_blocked else "debug"
            reason_text = (
                "blocked label set"
                if has_blocked
                else "no PR created (agent may have failed)"
            )
            _label_edit(
                owner,
                repo,
                n,
                remove=["agent-in-progress"],
                installation_token=installation_token,
            )
            if liveness_state is not None:
                liveness_state.clear()
            sched.mark_parked(n)
            parked_reasons[n] = reason_text
            alert(
                owner,
                repo,
                n,
                f"Issue #{n} parked: {reason_text}.",
                severity="warn",
                kind=kind,
                runlog=runlog,
                installation_token=installation_token,
            )

    # --- Step 3: completion. ---
    # Push the feature branch.
    push_proc = _authed_git_push(repo_root, branch_name, installation_token)
    if push_proc.returncode != 0:
        _log.warning(
            "daemon: git push %r failed (exit %d): %s",
            branch_name,
            push_proc.returncode,
            push_proc.stderr,
        )

    # Build PR body.
    # Each merged issue needs its own ``Closes #N`` keyword so GitHub
    # auto-closes all of them when the feature → main PR merges.  GitHub
    # does NOT parse comma-continuation (``closes #100, #101`` only closes
    # #100), so we emit one keyword per line (issue #67).
    if merged_issues:
        merged_section = "\n".join(f"Closes #{n}" for n in merged_issues)
    else:
        merged_section = "(none)"
    parked_list = (
        "\n".join(f"- #{n}: {reason}" for n, reason in parked_reasons.items())
        or "(none)"
    )
    pr_body = (
        f"## Work unit: {slug}\n\n"
        f"### Issues merged\n\n{merged_section}\n\n"
        f"### Issues parked (need human attention)\n\n{parked_list}\n"
        f"{_CLAUDE_ATTRIBUTION}"
    )
    pr_title = f"[daemon] {slug}"

    # Guard: skip _open_pr when the feature branch has zero commits
    # over origin/main.  This prevents an empty "Warning: 1 uncommitted
    # change" PR when the agent produced no commits (issue #65).
    # Fail-open: if rev-list exits non-zero or stdout is unparseable, we
    # proceed to _open_pr rather than silently losing the PR.
    count_proc = _run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-list",
            "--count",
            f"origin/main..{branch_name}",
        ]
    )
    if count_proc.returncode == 0 and count_proc.stdout.strip() == "0":
        _log.info(
            "daemon: work unit %r produced no commits over main"
            " — skipping PR (%d merged, %d parked)",
            slug,
            len(merged_issues),
            len(parked_reasons),
        )
    else:
        _open_pr(
            owner,
            repo,
            branch_name,
            pr_title,
            pr_body,
            installation_token=installation_token,
        )


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
    # fresh on every new daemon run.
    global _required_checks_warned
    _required_checks_warned = False

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
    _poll_env_kw: dict[str, dict[str, str]] = (
        {"env": _poll_gh_env} if _poll_gh_env is not None else {}
    )
    proc = _run(
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
        **_poll_env_kw,
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
    orphan_proc = _run(
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
        **_poll_env_kw,
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
