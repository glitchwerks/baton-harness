"""GitHub API helper cluster: issue/PR lookups, CI gate, PR creation.

Extracted verbatim (Phase 6b, #274, part of the #268 module-refactor
proposal) from ``chain/daemon.py`` / ``daemon/__init__.py``. This module
owns the ``gh`` CLI helper functions used to look up issues and PRs,
resolve the configured required-check set, run the CI gate to
merge-or-park an issue, and open the work unit's ready-for-review PR.

The ``_run_gh``, ``_label_edit``, ``merge_issue_branch``, and ``alert``
seams intentionally stay defined/imported in ``daemon/__init__.py`` (they
are shared by clusters not yet extracted) and are reached here via
``_daemon_mod.X(...)`` — a live attribute lookup on the parent package
module, not a captured import-time binding — so that
``mock.patch("baton_harness.chain.daemon.X", ...)`` in existing tests
continues to intercept calls made from this submodule (the "patch where
it's looked up" rule, plan §4 Phase 6 / issue #273).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import baton_harness.chain.daemon as _daemon_mod
from baton_harness.chain.app_auth import InstallationTokenSource, gh_env
from baton_harness.chain.heartbeat import LivenessState
from baton_harness.chain.merge import REQUIRED_CHECKS, MergeOutcome
from baton_harness.chain.runlog import RunLog
from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.tracker import Issue

# Hard-coded: preserves the pre-split "baton_harness.chain.daemon" logger
# name (plan §3.2) rather than the submodule's own __name__, so log
# aggregation and the caplog(logger=...) assertions in test_daemon.py stay
# byte-identical pre- and post-split. See #268.
_log = logging.getLogger("baton_harness.chain.daemon")

# (issue #225) Guard for the required_checks fallback warning -- fires
# at most once per ``run_daemon`` invocation (reset at the top of
# run_daemon, via ``_gh_api_helpers_mod._required_checks_warned`` since
# the reset lives in ``daemon/__init__.py`` while this guard's owning
# function lives here), not once per merge/work-unit within that run.
_required_checks_warned = False


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
    slug = title.lower()
    slug = slug.replace(" ", "-").replace("_", "-")
    # Strip unsafe characters.
    slug = re.sub(r"[^A-Za-z0-9._/-]", "", slug)
    # Strip leading non-alphanumeric.
    slug = re.sub(r"^[^A-Za-z0-9]+", "", slug)
    return slug or "milestone"


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
    proc = _daemon_mod._run_gh(
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
        _gh_call_env,
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
    proc = _daemon_mod._run_gh(
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
        _gh_call_env,
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
    proc = _daemon_mod._run_gh(
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
        _gh_call_env,
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
        outcome = _daemon_mod.merge_issue_branch(
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
        _daemon_mod._label_edit(
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
        _daemon_mod.alert(
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
        _daemon_mod._label_edit(
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
        _daemon_mod._label_edit(
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
        _daemon_mod.alert(
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
    proc = _daemon_mod._run_gh(
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
        _gh_call_env,
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
