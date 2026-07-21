"""Per-launch preflight + dispatch cluster: the #223/#144/#206 launch gate.

Extracted verbatim (Phase 6c, #275, part of the #268 module-refactor
proposal) from ``chain/daemon.py`` / ``daemon/__init__.py``. This module
owns the composite branch-protection preflight decision
(``_should_launch_worker``), the token-authenticated ``gh`` runner it
uses (``_build_preflight_runner``), the App-ID resolver
(``_resolve_app_id``), the per-issue preflight+dispatch helper
(``_launch_one_issue``), and the crash/unblock recovery dispatch wrapper
(``reconstruct``).

Four module-level names also move here from ``daemon/__init__.py``
(re-scoped 2026-07-20 during plan review, see plan §4 Phase 6 6a step 2):
``_COMPARATOR_TIMEOUT_SECONDS``, ``_NonGitRepoRootSentinel``,
``_NON_GIT_REPO_ROOT``, and ``_active_probe_repo_root``. Their only
readers/writers (``_should_launch_worker`` and ``_launch_one_issue``) are
both in this cluster — Python's ``global`` statement is module-local, so
splitting the globals from their readers/writers across two different
modules would silently create two distinct module-level names instead of
one shared one (the #223 probe-context gate would go permanently inert
with no loud failure signal).

Several leaf dependencies (``check_ruleset_signals``, ``post_slack_alert``,
``_probe_worker_push_denied``, ``_label_edit``, ``alert``) intentionally
stay imported/defined in ``daemon/__init__.py`` or a sibling submodule and
are reached here via ``_daemon_mod.X(...)`` — a live attribute lookup on
the parent package module, not a captured import-time binding — so that
``mock.patch("baton_harness.chain.daemon.X", ...)`` in existing tests
continues to intercept calls made from this submodule (the "patch where
it's looked up" rule, plan §4 Phase 6 / issue #273). This also applies to
``_should_launch_worker`` and ``_build_preflight_runner`` as seen from
``_launch_one_issue``: even though both are defined in THIS module,
several pre-existing tests patch them at the package level
(``daemon_mod._should_launch_worker`` / ``daemon_mod._build_preflight_runner``)
while driving the composite decision through ``daemon_mod._launch_one_issue``
— so ``_launch_one_issue`` reaches them via the same live parent-package
lookup rather than a bare local call.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import baton_harness.chain.daemon as _daemon_mod
from baton_harness.chain import recovery as _recovery_mod
from baton_harness.chain.app_auth import InstallationTokenSource, gh_env
from baton_harness.chain.identity import Identity, env_for
from baton_harness.chain.obs_config import ObsConfig
from baton_harness.chain.recovery import RecoveryResult
from baton_harness.chain.ruleset_status import (
    RulesetCheckResult,
    RulesetStatus,
)
from baton_harness.vendor.symphony.orchestrator import Orchestrator

from .push_probe import ProbeDenialReason, ProbeResult

# Hard-coded: preserves the pre-split "baton_harness.chain.daemon" logger
# name (plan §3.2) rather than the submodule's own __name__, so log
# aggregation and the caplog(logger=...) assertions in test_daemon.py stay
# byte-identical pre- and post-split. See #268.
_log = logging.getLogger("baton_harness.chain.daemon")

_GENERIC_CHECKS_DETAIL = (
    "harness-main-no-merge, harness-feature-daemon-only "
    "(ruleset check returned no detail)"
)

#: Timeout (seconds) applied to the comparator's gh runner (built by
#: `_build_preflight_runner`) so a stalled `gh api` ruleset call cannot
#: hang daemon launch indefinitely (CodeRabbit PR #253 round 2, finding
#: #5). The comparator is diagnostic-only (#223 demotion) -- a timeout
#: here degrades to an ERROR result rather than blocking launch.
_COMPARATOR_TIMEOUT_SECONDS: float = 30.0


class _NonGitRepoRootSentinel:
    """Marks the resolved ``repo_root`` as not a git worktree.

    Distinct from the legacy default (``None``), which means "no probe
    context is active — a direct caller of ``_should_launch_worker``";
    see ``_active_probe_repo_root`` below.
    """


#: Sentinel value for ``_active_probe_repo_root`` when `_launch_one_issue`
#: resolves a ``repo_root`` with no ``.git`` entry (CodeRabbit PR #253
#: finding C10). Distinct from the legacy ``None`` default so
#: `_should_launch_worker` can fail closed on it WITHOUT invoking the
#: behavioral probe (there is no git worktree to push from), while
#: direct callers of `_should_launch_worker` (bypassing
#: `_launch_one_issue` entirely) still see the legacy ``None`` and keep
#: the comparator-only gate.
_NON_GIT_REPO_ROOT = _NonGitRepoRootSentinel()

# Serial-launch probe context for issue #223. `_launch_one_issue` sets this
# around its call to `_should_launch_worker`, then resets it in a `finally`
# block so direct callers of `_should_launch_worker` keep the legacy path.
_active_probe_repo_root: Path | _NonGitRepoRootSentinel | None = None


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
    try:
        result = _daemon_mod.check_ruleset_signals(
            owner, repo, app_id=app_id, runner=runner
        )
    except subprocess.TimeoutExpired as exc:
        # CodeRabbit PR #253 round 2, finding #5: even with a bound
        # timeout on the runner (see _build_preflight_runner), a
        # TimeoutExpired that still escapes check_ruleset_signals must
        # not propagate out of the launch decision -- the comparator is
        # diagnostic-only (#223 demotion), so its own failure degrades
        # to an ERROR result and the existing non-MATCH refusal path
        # below handles the rest (fail-closed, alert still attempted).
        _log.warning(
            "daemon: check_ruleset_signals raised TimeoutExpired for "
            "issue #%d; degrading to ERROR (comparator is "
            "diagnostic-only): %s",
            issue_number,
            exc,
        )
        result = RulesetCheckResult(
            status=RulesetStatus.ERROR,
            detail=f"comparator check_ruleset_signals timed out: {exc}",
        )

    # Snapshot the module-level probe context into a local so mypy can
    # narrow it (Path vs the non-git sentinel vs the legacy None) across
    # the two checks below without losing the narrowing to a call in
    # between.
    active_repo_root = _active_probe_repo_root

    if isinstance(active_repo_root, _NonGitRepoRootSentinel):
        # CodeRabbit PR #253 finding C10: a resolved repo_root with no
        # `.git` entry means the decisive behavioral probe cannot run at
        # all — refuse outright rather than falling through to the
        # comparator-only gate below, where a bare MATCH would otherwise
        # authorize launch with no behavioral check.
        _log.warning(
            "daemon: repo_root for issue #%d is not a git worktree; "
            "the behavioral push-denial probe cannot run — refusing "
            "launch (comparator=%s)",
            issue_number,
            result.status.name,
        )
        checks_detail = result.detail or _GENERIC_CHECKS_DETAIL
        message = (
            "baton-harness refusing to launch worker — "
            "repo_root is not a git worktree; the decisive push-denial "
            "probe cannot run. Will NOT run in dangerous mode. "
            f"Comparator status: {result.status.name}. "
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
                _daemon_mod.post_slack_alert(obs.heartbeat_ping_url, message)
            except Exception:  # noqa: BLE001
                _log.warning(
                    "daemon: post_slack_alert raised despite no-raise "
                    "contract (issue #%d); swallowing",
                    issue_number,
                )
        return False

    if active_repo_root is not None:
        if result.status is not RulesetStatus.MATCH:
            _log.warning(
                "daemon: comparator reported %s for issue #%d; "
                "treating as diagnostic-only while push probe decides",
                result.status.name,
                issue_number,
            )

        try:
            probe_result = _daemon_mod._probe_worker_push_denied(
                active_repo_root
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "daemon: push-denial probe raised for issue #%d; "
                "failing closed: %s",
                issue_number,
                exc,
            )
            probe_result = ProbeResult(
                denied=False,
                reason=ProbeDenialReason.TRANSPORT_ERROR,
                detail=f"push-denial probe raised unexpectedly: {exc}",
            )

        if probe_result.denied:
            return True

        checks_detail = result.detail or _GENERIC_CHECKS_DETAIL
        reason_text = probe_result.detail or (
            probe_result.reason.name
            if probe_result.reason is not None
            else "UNKNOWN"
        )
        message = (
            "baton-harness refusing to launch worker — "
            "push-denial probe did not confirm denial "
            f"(reason={reason_text}; comparator={result.status.name}). "
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
                _daemon_mod.post_slack_alert(obs.heartbeat_ping_url, message)
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
            _daemon_mod.post_slack_alert(obs.heartbeat_ping_url, message)
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

    The returned runner also bounds its ``subprocess.run`` call with
    ``timeout=_COMPARATOR_TIMEOUT_SECONDS`` (CodeRabbit PR #253 round 2,
    finding #5) so a stalled ``gh api`` call cannot hang daemon launch
    indefinitely; a resulting ``subprocess.TimeoutExpired`` degrades to
    an ``ERROR`` result rather than propagating (see
    ``check_ruleset_signals`` and ``_should_launch_worker``).

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
            timeout=_COMPARATOR_TIMEOUT_SECONDS,
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
            worktree (no ``.git`` entry), the decisive push probe
            cannot run at all — launch is refused outright (fail
            closed) rather than falling back to the comparator-only
            gate (CodeRabbit PR #253 finding C10).

    Returns:
        The worker result string on success, or ``None`` when preflight
        refuses the launch.
    """
    global _active_probe_repo_root

    issue_number: int = issue_obj.number  # type: ignore[attr-defined]
    resolved_repo_root = repo_root or Path.cwd()
    has_git_dir = (resolved_repo_root / ".git").exists()

    preflight_runner = _daemon_mod._build_preflight_runner(installation_token)
    _active_probe_repo_root = (
        resolved_repo_root if has_git_dir else _NON_GIT_REPO_ROOT
    )
    try:
        preflight = _daemon_mod._should_launch_worker(
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
        _daemon_mod._label_edit(
            owner,
            repo,
            issue_number,
            add=["agent-ready"],
            remove=["agent-in-progress"],
            installation_token=installation_token,
        )
        # Post a blocking comment so operators know why the worker was
        # refused.
        _daemon_mod.alert(
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
