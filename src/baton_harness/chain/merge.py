"""CI-gated ``--no-ff`` merge into the feature branch.

Implements the §3.3.1 green predicate and the ``--no-ff`` merge of a
per-issue branch into ``feature/<slug>`` once CI is confirmed green.

Design decisions (§3.3 / §3.3.1 / C-I2):

Required-check set (C-I2 finding):
    The target repo has NO classic branch-protection rule exposing a
    "required checks" set (the branch-protection API returns 404 for this
    repo; the ruleset "default" is present but its required status-check
    contexts are NOT enumerated in the API response).  Therefore this module
    takes the required-check set from a **module constant**
    (``REQUIRED_CHECKS``)
    that defaults to the three actual CI check names confirmed from the repo's
    workflow file:

        - ``"Lint (ruff)"``
        - ``"Test (pytest)"``
        - ``"Type check (mypy)"``

    TODO: wire ``REQUIRED_CHECKS`` to a ``required_checks`` list in
    ``config/WORKFLOW.md`` or a daemon config object (P3) so operators can
    override it without editing this file.

CRITICAL — no vacuous green:
    ``evaluate_ci`` NEVER treats "zero matching jobs found" as green.  If a
    configured required job is absent from the Actions API response, that is
    NOT-YET → the poller continues until the hard timeout, then returns RED
    (``CiResult.RED`` with ``MergeOutcome.CI_TIMEOUT``).  An empty job
    list or a list with no required jobs present is therefore always a timeout
    → RED outcome, never a pass.

Green predicate (§3.3.1):
    - GREEN: every required job has ``status: completed`` AND
      ``conclusion`` ∈ {``success``, ``neutral``, ``skipped``}.
    - RED: any required job with ``conclusion`` ∈ {``failure``,
      ``cancelled``, ``timed_out``, ``action_required``}.
    - NOT-YET: any required job is ``queued`` or ``in_progress``, or a
      configured required job is absent from the response.  Poll with
      bounded backoff; on hard timeout → RED (``ci-timeout``).
    - Non-required jobs are **ignored entirely** (pass or fail).

Data source (#121 — Actions API):
    CI job data is sourced from the GitHub Actions API via two calls per
    poll:

    1. ``repos/{owner}/{repo}/actions/runs?head_sha={sha}`` — returns a
       ``workflow_runs`` list (each with an ``id`` field).
    2. ``repos/{owner}/{repo}/actions/runs/{id}/jobs`` — returns a
       ``jobs`` list (each with ``name``, ``status``, ``conclusion``).

    ``_query_action_jobs`` flattens the jobs from all runs into the same
    ``[{name, status, conclusion}]`` shape that ``_classify_check_runs``
    already consumes.  Re-run dedup: when the same job ``name`` appears in
    multiple workflow runs for the SHA, the job from the run with the
    highest ``id`` wins.

    403 fail-fast (#119 hardening): ``CiAuthError`` is raised immediately on
    any permission error (non-zero exit with ``Resource not accessible`` or
    ``403`` in stderr).  ``evaluate_ci`` does NOT catch ``CiAuthError``;
    it propagates to the caller to avoid a 30-minute timeout loop.

Merge safety:
    This module ONLY merges per-issue branches INTO ``feature/<slug>``.  It
    NEVER merges ``feature/<slug>`` → ``main`` (hard constraint, §3.3 /
    issue #27).  All generated merge commands target the feature branch
    explicitly; the word ``main`` must not appear as a git merge target.

Provenance (§11.5 / B-I2):
    On a green merge the module:
    1. Issues ``git merge --no-ff -m "<provenance-trailer> …"`` with a
       structured trailer: ``Baton-Harness-Merge: issue-<N> ci=green``.
    2. Adds the ``agent-merged`` label to the issue.
    3. Posts a marker comment on the issue recording the CI-green-at-merge
       fact.

    These three signals are what ``chain/recovery.py`` (P3) uses to
    reconstruct the ``done`` set without re-querying GC'd jobs.

Subprocess style follows the ``chain/gh_deps.py`` pattern: a single
module-local ``_run`` function is the only subprocess seam, making it
trivially patchable in tests (spike finding F8).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from enum import Enum, auto
from pathlib import Path

from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    gh_env,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required-check set (C-I2 resolution)
#
# TODO (P3): read this from config/WORKFLOW.md or a daemon config object so
# the list can be overridden without editing this file.  The current default
# matches the three checks confirmed from .github/workflows/ci.yml.
# ---------------------------------------------------------------------------

REQUIRED_CHECKS: list[str] = [
    "Lint (ruff)",
    "Test (pytest)",
    "Type check (mypy)",
]

# Default poll configuration (§3.3.1).
# The daemon (P3) may override these via the function's keyword arguments.
_DEFAULT_POLL_INTERVAL: float = 10.0  # seconds between check-runs queries
_DEFAULT_TIMEOUT: float = 1800.0  # 30-minute hard ceiling


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class CiResult(Enum):
    """The outcome of the CI green-predicate evaluation.

    Attributes:
        GREEN: All required checks completed with a passing conclusion.
        RED: At least one required check completed with a failing conclusion.
        TIMEOUT: A required check never completed before the hard timeout.
    """

    GREEN = auto()
    RED = auto()
    TIMEOUT = auto()


class MergeOutcome(Enum):
    """The outcome of a ``merge_issue_branch`` call.

    Attributes:
        MERGED: CI was green; the branch was merged with ``--no-ff``.
        CI_FAILED: CI returned RED; no merge was attempted.
        CI_TIMEOUT: CI never completed within the hard timeout; no merge.
        MERGE_CONFLICT: The ``git merge --no-ff`` step itself failed (e.g.
            a content conflict).  ``git merge --abort`` was issued to restore
            a clean state.  The daemon (P3) should park and escalate.
    """

    MERGED = auto()
    CI_FAILED = auto()
    CI_TIMEOUT = auto()
    MERGE_CONFLICT = auto()


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class CiAuthError(RuntimeError):
    """Raised when the Actions API returns a permission error.

    Signals a 403 / ``Resource not accessible`` response from ``gh api``.
    ``evaluate_ci`` does NOT catch this; it propagates immediately to avoid
    a 30-minute timeout loop (#119 hardening).
    """


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_auth_error(proc: subprocess.CompletedProcess[str]) -> bool:
    """Return True if ``proc`` represents a 403 / permission error.

    Args:
        proc: A completed process whose ``returncode`` is non-zero.

    Returns:
        ``True`` if stderr contains ``"Resource not accessible"`` or
        ``"403"``, ``False`` otherwise.
    """
    return "Resource not accessible" in proc.stderr or "403" in proc.stderr


def _query_action_jobs(
    owner: str,
    repo: str,
    sha: str,
    *,
    installation_token: InstallationTokenSource = "",
) -> list[dict[str, object]]:
    """Query the GitHub Actions API for jobs associated with a commit SHA.

    Issues two ``gh api`` calls per invocation:

    1. ``repos/{owner}/{repo}/actions/runs?head_sha={sha}`` — returns the
       ``workflow_runs`` list (each entry has an ``id`` field).
    2. ``repos/{owner}/{repo}/actions/runs/{id}/jobs`` — returns the
       ``jobs`` list (each entry has ``name``, ``status``, ``conclusion``).

    All jobs from all runs are flattened into a single list in the same
    ``[{name, status, conclusion}]`` shape that ``_classify_check_runs``
    consumes.

    Re-run dedup: when the same job ``name`` appears in multiple workflow
    runs, the job from the run with the highest ``id`` wins.  This is
    implemented by processing runs in ascending ``id`` order (lowest-id
    first), so later writes in the by-name dict overwrite earlier ones.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        sha: The commit SHA whose Actions jobs are queried.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, the ``gh`` subprocess
            calls override ``GH_TOKEN`` via a per-call env copy —
            ``os.environ`` is never mutated.  Pass ``""`` (default) to
            inherit the ambient credential unchanged.

    Returns:
        A flat list of job dicts, each containing at minimum ``name``,
        ``status``, and ``conclusion``.  Returns ``[]`` when no workflow
        runs are found for ``sha``.

    Raises:
        CiAuthError: If either ``gh api`` call returns a non-zero exit
            with ``"Resource not accessible"`` or ``"403"`` in stderr.
        RuntimeError: If either ``gh api`` call returns a non-zero exit
            for a reason other than a permission error.
        ValueError: If the response JSON cannot be parsed or lacks the
            expected keys.
    """
    gh_call_env = gh_env(installation_token) if installation_token else None
    _env_kw: dict[str, dict[str, str]] = (
        {"env": gh_call_env} if gh_call_env is not None else {}
    )
    # Step 1: list workflow runs for this SHA.
    runs_url = f"repos/{owner}/{repo}/actions/runs?head_sha={sha}"
    runs_proc = _run(["gh", "api", runs_url], **_env_kw)
    if runs_proc.returncode != 0:
        if _is_auth_error(runs_proc):
            raise CiAuthError(
                f"gh api actions/runs permission denied"
                f" (exit {runs_proc.returncode}): {runs_proc.stderr}"
            )
        raise RuntimeError(
            f"gh api actions/runs failed"
            f" (exit {runs_proc.returncode}): {runs_proc.stderr}"
        )
    runs_data: dict[str, object] = json.loads(runs_proc.stdout)
    workflow_runs_raw = runs_data.get("workflow_runs")
    if not isinstance(workflow_runs_raw, list):
        raise ValueError(
            f"Expected 'workflow_runs' list in Actions API response:"
            f" {runs_proc.stdout[:200]}"
        )
    # Narrow element type after isinstance guard.
    workflow_runs: list[dict[str, object]] = [
        dict(r) for r in workflow_runs_raw
    ]

    if not workflow_runs:
        return []

    # Step 2: for each run, fetch its jobs.  Process in ascending id order
    # so that higher-id runs overwrite lower-id runs in by_name (dedup).
    sorted_runs = sorted(workflow_runs, key=lambda r: int(str(r["id"])))

    by_name: dict[str, dict[str, object]] = {}
    for run in sorted_runs:
        run_id = run["id"]
        jobs_url = f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        jobs_proc = _run(["gh", "api", jobs_url], **_env_kw)
        if jobs_proc.returncode != 0:
            if _is_auth_error(jobs_proc):
                raise CiAuthError(
                    f"gh api actions/runs/{run_id}/jobs permission denied"
                    f" (exit {jobs_proc.returncode}): {jobs_proc.stderr}"
                )
            raise RuntimeError(
                f"gh api actions/runs/{run_id}/jobs failed"
                f" (exit {jobs_proc.returncode}): {jobs_proc.stderr}"
            )
        jobs_data: dict[str, object] = json.loads(jobs_proc.stdout)
        jobs_raw = jobs_data.get("jobs")
        if not isinstance(jobs_raw, list):
            raise ValueError(
                f"Expected 'jobs' list in Actions runs/{run_id}/jobs"
                f" response: {jobs_proc.stdout[:200]}"
            )
        # Higher-id run overwrites lower-id run for duplicate job names.
        for job in jobs_raw:
            job_dict: dict[str, object] = dict(job)
            name = str(job_dict.get("name", ""))
            by_name[name] = job_dict

    return list(by_name.values())


_PASS_CONCLUSIONS: frozenset[str] = frozenset(
    {"success", "neutral", "skipped"}
)
_FAIL_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "cancelled", "timed_out", "action_required"}
)


def _classify_check_runs(
    runs: list[dict[str, object]],
    required: list[str],
) -> CiResult | None:
    """Apply the §3.3.1 green predicate to a check-runs snapshot.

    Rules (applied in order):

    1. For each required check name, find the corresponding run (by name).
       If any required check is absent → NOT-YET (return ``None``).
    2. If a required check's ``conclusion`` ∈
       {``failure``, ``cancelled``, ``timed_out``, ``action_required``}
       → RED immediately.
    3. If a required check's ``status`` is not ``completed``, or is
       ``completed`` but ``conclusion`` is NOT in the pass set
       {``success``, ``neutral``, ``skipped``} → NOT-YET (return ``None``).
       This covers unrecognised or null conclusions on completed runs so they
       never vacuously pass.
    4. If every required check is ``status: completed`` with ``conclusion``
       ∈ {``success``, ``neutral``, ``skipped``} → GREEN.
    5. Non-required checks are ignored (they cannot affect the result).

    Args:
        runs: Check-run dicts as returned by the GitHub API.
        required: The list of required check names to evaluate.

    Returns:
        ``CiResult.GREEN`` if all required checks pass, ``CiResult.RED`` if
        any required check has a terminal failing conclusion, or ``None`` to
        signal NOT-YET (some required check is still pending, absent, or has
        an unrecognised conclusion).  The deadline-TIMEOUT outcome is
        determined by ``evaluate_ci``'s polling loop, not here.
    """
    by_name = {str(r.get("name", "")): r for r in runs}

    for check_name in required:
        run = by_name.get(check_name)
        if run is None:
            # Required check absent from response → NOT-YET.
            return None

        status = str(run.get("status", ""))
        conclusion = run.get("conclusion")
        conclusion_str = str(conclusion) if conclusion is not None else ""

        # Terminal failing conclusions → RED immediately.
        if conclusion_str in _FAIL_CONCLUSIONS:
            return CiResult.RED

        # A completed run is GREEN only if conclusion is in the pass set.
        # Anything else (unrecognised, null, still running) → NOT-YET.
        if status != "completed" or conclusion_str not in _PASS_CONCLUSIONS:
            return None

    # All required checks are completed with passing conclusions → GREEN.
    return CiResult.GREEN


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_ci(
    owner: str,
    repo: str,
    sha: str,
    required: list[str] | None = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    installation_token: InstallationTokenSource = "",
) -> CiResult:
    """Evaluate the §3.3.1 CI green predicate for a commit SHA.

    Queries the Actions API (via ``_query_action_jobs``) repeatedly until
    all required jobs are green, any required job is definitively red, or
    the hard timeout elapses.

    CRITICAL: Never returns GREEN when no required jobs are found in the
    response (no vacuous pass).  An absent required job counts as NOT-YET
    → keeps polling → RED on timeout.

    ``CiAuthError`` from ``_query_action_jobs`` is NOT caught here; it
    propagates immediately to avoid looping to the 30-minute timeout.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        sha: The PR head commit SHA to evaluate.
        required: The list of required check names.  Defaults to
            ``REQUIRED_CHECKS``.
        poll_interval: Seconds to wait between polls.  Use ``0`` in tests.
        timeout: Hard ceiling in seconds.  Returns ``CiResult.RED``
            (``ci-timeout`` semantics) if the deadline elapses with checks
            still pending.  Use ``0`` for an immediate-timeout in tests.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  Threaded to ``_query_action_jobs`` for
            per-call env override.  Pass ``""`` (default) to inherit the
            ambient credential unchanged.

    Returns:
        ``CiResult.GREEN`` if all required checks pass, ``CiResult.RED`` if
        any required check has a terminal failing conclusion or if the hard
        timeout elapses.
    """
    if required is None:
        required = REQUIRED_CHECKS

    deadline = time.monotonic() + timeout

    while True:
        runs = _query_action_jobs(
            owner, repo, sha, installation_token=installation_token
        )
        result = _classify_check_runs(runs, required)

        if result == CiResult.GREEN:
            return CiResult.GREEN
        if result == CiResult.RED:
            return CiResult.RED

        # NOT-YET: result is None (pending, absent, or unrecognised
        # conclusion).  Check deadline BEFORE sleeping to handle timeout=0.
        if time.monotonic() >= deadline:
            # Hard timeout elapsed — ci-timeout semantics.
            return CiResult.TIMEOUT

        if poll_interval > 0:
            time.sleep(poll_interval)
        else:
            # poll_interval=0 with remaining deadline: check deadline again.
            # With timeout=0 the deadline was already past at entry, so the
            # check above fires on the second pass.  Ensure the loop exits by
            # re-checking immediately after the no-sleep pass.
            if time.monotonic() >= deadline:
                return CiResult.TIMEOUT


def merge_issue_branch(
    repo_root: Path,
    owner: str,
    repo: str,
    issue: int,
    pr_head_sha: str,
    issue_branch: str,
    feature_branch: str,
    required: list[str] | None = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    installation_token: InstallationTokenSource = "",
) -> MergeOutcome:
    """Evaluate CI and ``--no-ff`` merge if green; persist provenance.

    Implements the full §3.3 / §3.3.1 merge gate for a single issue:

    1. Call ``evaluate_ci`` with the PR head SHA.
    2. On GREEN: check out ``feature_branch``, merge ``issue_branch`` into it
       with ``--no-ff`` and a daemon-provenance trailer, then persist the
       CI-green-at-merge fact (``agent-merged`` label + marker comment).
    3. On RED or TIMEOUT: do NOT merge; return the appropriate outcome.

    HARD CONSTRAINT: the merge target is ALWAYS ``feature_branch``.  This
    function NEVER issues a command that merges into ``main``.

    Args:
        repo_root: Absolute path to the local repository checkout.
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number being merged.
        pr_head_sha: The PR head commit SHA used to query check-runs.
        issue_branch: The per-issue branch to merge (e.g.
            ``"baton/v2-daemon-44"``).
        feature_branch: The feature branch to merge INTO (e.g.
            ``"feature/v2-daemon"``).  Must start with ``feature/`` — this
            guard ensures the merge never accidentally targets ``main``.
        required: Required check names; defaults to ``REQUIRED_CHECKS``.
        poll_interval: Seconds between CI polls.  Use ``0`` in tests.
        timeout: Hard CI-poll ceiling in seconds.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  Threaded to ``evaluate_ci`` and to all
            ``gh`` subprocess calls in the provenance-write path via a
            per-call env copy — ``os.environ`` is never mutated.  Pass
            ``""`` (default) to inherit the ambient credential unchanged.

    Returns:
        ``MergeOutcome.MERGED`` on a successful green merge.
        ``MergeOutcome.CI_FAILED`` when CI is RED.
        ``MergeOutcome.CI_TIMEOUT`` when the hard timeout elapses.

    Raises:
        ValueError: If ``feature_branch`` does not start with ``"feature/"``
            (hard constraint — prevents accidental merge to main).
        RuntimeError: If the git merge command itself fails after CI is green.
    """
    # Hard constraint guard: never merge to main.
    if not feature_branch.startswith("feature/"):
        raise ValueError(
            f"feature_branch must start with 'feature/', got:"
            f" {feature_branch!r}.  This module NEVER merges to main."
        )

    ci_result = evaluate_ci(
        owner,
        repo,
        pr_head_sha,
        required=required,
        poll_interval=poll_interval,
        timeout=timeout,
        installation_token=installation_token,
    )

    if ci_result == CiResult.RED:
        return MergeOutcome.CI_FAILED
    if ci_result == CiResult.TIMEOUT:
        return MergeOutcome.CI_TIMEOUT

    # ci_result == CiResult.GREEN from here.
    # Check out the feature branch before merging.
    checkout_proc = _run(
        ["git", "-C", str(repo_root), "checkout", feature_branch]
    )
    if checkout_proc.returncode != 0:
        raise RuntimeError(
            f"git checkout {feature_branch!r} before merge failed"
            f" (exit {checkout_proc.returncode}): {checkout_proc.stderr}"
        )

    # Build the provenance trailer (§11.5 / B-I2).
    trailer = f"Baton-Harness-Merge: issue-{issue} ci=green"
    merge_message = (
        f"Merge branch '{issue_branch}' into {feature_branch}\n\n{trailer}"
    )

    merge_proc = _run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge",
            "--no-ff",
            "-m",
            merge_message,
            issue_branch,
        ]
    )
    if merge_proc.returncode != 0:
        # FIX 4: abort the failed merge so the repo is not left mid-merge
        # (MERGE_HEAD / conflicted index).  Best-effort: ignore abort errors.
        _run(["git", "-C", str(repo_root), "merge", "--abort"])
        _log.warning(
            "git merge --no-ff of %r into %r failed (exit %d);"
            " merge --abort issued.  stderr: %s",
            issue_branch,
            feature_branch,
            merge_proc.returncode,
            merge_proc.stderr,
        )
        return MergeOutcome.MERGE_CONFLICT

    # Persist the CI-green-at-merge fact (B-I2 / §11.5).
    provenance_persisted = _persist_ci_green(
        owner,
        repo,
        issue,
        pr_head_sha,
        installation_token=installation_token,
    )
    if not provenance_persisted:
        _log.warning(
            "Provenance persistence failed for issue #%d sha=%s."
            " The merge is committed but the agent-merged label / marker"
            " comment could not be written.  The daemon (P3) should"
            " retry or escalate.",
            issue,
            pr_head_sha,
        )

    return MergeOutcome.MERGED


def _persist_ci_green(
    owner: str,
    repo: str,
    issue: int,
    sha: str,
    *,
    installation_token: InstallationTokenSource = "",
) -> bool:
    """Persist the CI-green-at-merge fact for crash recovery.

    Adds the ``agent-merged`` label to the issue and posts a marker comment
    recording the CI-green SHA.  ``chain/recovery.py`` (P3) reads these
    signals to reconstruct the ``done`` set without re-querying GC'd
    check-runs.

    Per §11.5/B-I2 the CI-green fact MUST persist for recovery.  Failures
    are logged loudly (WARNING or higher) so the daemon can detect and retry
    or escalate.  The caller must NOT undo the merge on a failure here.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.
        sha: The PR head commit SHA at which CI was green.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, all ``gh`` subprocess
            calls use a per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated.

    Returns:
        ``True`` if both the label and comment were persisted successfully,
        ``False`` if either write failed (a WARNING is logged in that case).
    """
    persisted = True
    gh_call_env = gh_env(installation_token) if installation_token else None
    _env_kw: dict[str, dict[str, str]] = (
        {"env": gh_call_env} if gh_call_env is not None else {}
    )

    # Add agent-merged label.
    label_proc = _run(
        [
            "gh",
            "issue",
            "edit",
            str(issue),
            "--repo",
            f"{owner}/{repo}",
            "--add-label",
            "agent-merged",
        ],
        **_env_kw,
    )
    if label_proc.returncode != 0:
        # FIX 5: loud warning — do NOT silently swallow this failure.
        _log.warning(
            "Failed to add agent-merged label to issue #%d (exit %d): %s",
            issue,
            label_proc.returncode,
            label_proc.stderr,
        )
        persisted = False

    # Post marker comment with the CI-green SHA.
    marker = (
        f"baton-harness: CI-green-at-merge sha={sha}"
        f" issue={issue} label=agent-merged"
    )
    comment_proc = _run(
        [
            "gh",
            "issue",
            "comment",
            str(issue),
            "--repo",
            f"{owner}/{repo}",
            "--body",
            marker,
        ],
        **_env_kw,
    )
    if comment_proc.returncode != 0:
        # FIX 5: loud warning — do NOT silently swallow this failure.
        _log.warning(
            "Failed to post CI-green marker comment on issue #%d"
            " (exit %d): %s",
            issue,
            comment_proc.returncode,
            comment_proc.stderr,
        )
        persisted = False

    return persisted


def merge_issue_branches(
    repo_root: Path,
    owner: str,
    repo: str,
    issues: list[int],
    pr_head_shas: list[str],
    issue_branches: list[str],
    feature_branch: str,
    required: list[str] | None = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[int, MergeOutcome]:
    """Merge a list of per-issue branches in dependency order.

    Iterates the provided lists in order (lowest-dependency-first, as
    ``graphlib`` natural order), calling ``merge_issue_branch`` for each.
    Stops processing the remainder when an issue's CI is RED or TIMEOUT
    (the later issues depend on earlier ones; a failure in the sequence
    would leave an unsound integration base).

    Args:
        repo_root: Absolute path to the local repository checkout.
        owner: The GitHub repository owner.
        repo: The repository name.
        issues: Ordered list of issue numbers to merge (dependency order).
        pr_head_shas: Corresponding list of PR head commit SHAs.
        issue_branches: Corresponding list of per-issue branch names.
        feature_branch: The feature branch to merge INTO.
        required: Required check names; defaults to ``REQUIRED_CHECKS``.
        poll_interval: Seconds between CI polls.  Use ``0`` in tests.
        timeout: Hard CI-poll ceiling in seconds.

    Returns:
        A ``dict[int, MergeOutcome]`` mapping each processed issue number to
        its outcome.  Issues not reached (because an earlier one was RED)
        are absent from the dict.

    Raises:
        ValueError: If the lengths of ``issues``, ``pr_head_shas``, and
            ``issue_branches`` are inconsistent, or if ``feature_branch``
            does not start with ``"feature/"``.
    """
    if not (len(issues) == len(pr_head_shas) == len(issue_branches)):
        raise ValueError(
            "issues, pr_head_shas, and issue_branches must have the same"
            " length."
        )

    outcomes: dict[int, MergeOutcome] = {}
    for issue, sha, branch in zip(
        issues, pr_head_shas, issue_branches, strict=True
    ):
        outcome = merge_issue_branch(
            repo_root=repo_root,
            owner=owner,
            repo=repo,
            issue=issue,
            pr_head_sha=sha,
            issue_branch=branch,
            feature_branch=feature_branch,
            required=required,
            poll_interval=poll_interval,
            timeout=timeout,
        )
        outcomes[issue] = outcome
        if outcome != MergeOutcome.MERGED:
            # Stop: an earlier dependency failed; later branches cannot be
            # safely merged onto this integration state.
            break

    return outcomes
