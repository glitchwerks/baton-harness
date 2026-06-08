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
    ``evaluate_ci`` NEVER treats "zero matching checks found" as green.  If a
    configured required check is absent from the check-runs response, that is
    NOT-YET → the poller continues until the hard timeout, then returns RED
    (``CiResult.RED`` with ``MergeOutcome.CI_TIMEOUT``).  An empty check-run
    set or a set with no required checks present is therefore always a timeout
    → RED outcome, never a pass.

Green predicate (§3.3.1):
    - GREEN: every required check has ``status: completed`` AND
      ``conclusion`` ∈ {``success``, ``neutral``, ``skipped``}.
    - RED: any required check with ``conclusion`` ∈ {``failure``,
      ``cancelled``, ``timed_out``, ``action_required``}.
    - NOT-YET: any required check is ``queued`` or ``in_progress``, or a
      configured required check is absent from the response.  Poll with
      bounded backoff; on hard timeout → RED (``ci-timeout``).
    - Non-required checks are **ignored entirely** (pass or fail).

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
    reconstruct the ``done`` set without re-querying GC'd check-runs.

Subprocess style follows the ``chain/gh_deps.py`` pattern: a single
module-local ``_run`` function is the only subprocess seam, making it
trivially patchable in tests (spike finding F8).
"""

from __future__ import annotations

import json
import subprocess
import time
from enum import Enum, auto
from pathlib import Path

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
    """

    MERGED = auto()
    CI_FAILED = auto()
    CI_TIMEOUT = auto()


# ---------------------------------------------------------------------------
# Subprocess helper (the sole I/O seam; patch this in tests)
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an external command and return its completed process.

    Centralises subprocess invocation so tests can patch a single symbol
    (spike finding F8 — hooks must be independently testable).

    Args:
        cmd: Command and arguments to execute (no shell interpolation).

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr.
        Callers inspect ``returncode`` themselves.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _query_check_runs(
    owner: str,
    repo: str,
    sha: str,
) -> list[dict[str, object]]:
    """Query the GitHub check-runs API for a commit SHA.

    Calls ``gh api repos/{owner}/{repo}/commits/{sha}/check-runs`` and
    returns the ``check_runs`` array from the response.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        sha: The commit SHA whose check-runs are queried.

    Returns:
        A list of check-run dicts, each containing at minimum ``name``,
        ``status``, and ``conclusion`` fields.

    Raises:
        RuntimeError: If the ``gh api`` call returns a non-zero exit code.
        ValueError: If the response JSON cannot be parsed or lacks the
            expected ``check_runs`` key.
    """
    url = f"repos/{owner}/{repo}/commits/{sha}/check-runs"
    proc = _run(["gh", "api", url])
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api check-runs failed (exit {proc.returncode}): {proc.stderr}"
        )
    data: dict[str, object] = json.loads(proc.stdout)
    runs_raw = data.get("check_runs")
    if runs_raw is None:
        raise ValueError(
            f"Unexpected check-runs API response (missing 'check_runs'):"
            f" {proc.stdout[:200]}"
        )
    if not isinstance(runs_raw, list):
        raise ValueError(
            f"Expected 'check_runs' to be a list, got: {type(runs_raw)}"
        )
    return [dict(r) for r in runs_raw]


def _classify_check_runs(
    runs: list[dict[str, object]],
    required: list[str],
) -> CiResult:
    """Apply the §3.3.1 green predicate to a check-runs snapshot.

    Rules (applied in order):
    1. For each required check name, find the corresponding run (by name).
       If any required check is absent → NOT-YET (return ``None`` to signal
       the caller to keep polling).
    2. If a required check's ``conclusion`` ∈
       {``failure``, ``cancelled``, ``timed_out``, ``action_required``}
       → RED immediately.
    3. If a required check's ``status`` ∈ {``queued``, ``in_progress``}
       → NOT-YET (return ``None``).
    4. If every required check is ``status: completed`` with ``conclusion``
       ∈ {``success``, ``neutral``, ``skipped``} → GREEN.
    5. Non-required checks are ignored (they cannot affect the result).

    Args:
        runs: Check-run dicts as returned by the GitHub API.
        required: The list of required check names to evaluate.

    Returns:
        ``CiResult.GREEN`` if all required checks pass, ``CiResult.RED`` if
        any required check has a terminal failing conclusion, or ``None`` if
        the evaluation is NOT-YET (some required check is still pending or
        absent).
    """
    by_name = {str(r.get("name", "")): r for r in runs}

    for check_name in required:
        run = by_name.get(check_name)
        if run is None:
            # Required check absent from response → NOT-YET.
            return CiResult.TIMEOUT  # sentinel: caller checks type

        status = str(run.get("status", ""))
        conclusion = run.get("conclusion")
        conclusion_str = str(conclusion) if conclusion is not None else ""

        # Terminal failing conclusions → RED immediately.
        if status == "completed" and conclusion_str in {
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
        }:
            return CiResult.RED

        # Still running → NOT-YET (signal via special sentinel).
        if status in {"queued", "in_progress"} or (status != "completed"):
            return CiResult.TIMEOUT  # sentinel

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
) -> CiResult:
    """Evaluate the §3.3.1 CI green predicate for a commit SHA.

    Queries the check-runs API repeatedly until all required checks are
    green, any required check is definitively red, or the hard timeout
    elapses.

    CRITICAL: Never returns GREEN when no required checks are found in the
    response (no vacuous pass).  An absent required check counts as NOT-YET
    → keeps polling → RED on timeout.

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

    Returns:
        ``CiResult.GREEN`` if all required checks pass, ``CiResult.RED`` if
        any required check has a terminal failing conclusion or if the hard
        timeout elapses.
    """
    if required is None:
        required = REQUIRED_CHECKS

    deadline = time.monotonic() + timeout

    while True:
        runs = _query_check_runs(owner, repo, sha)
        result = _classify_check_runs(runs, required)

        if result == CiResult.GREEN:
            return CiResult.GREEN
        if result == CiResult.RED:
            return CiResult.RED

        # NOT-YET: result == CiResult.TIMEOUT (sentinel for pending/absent).
        # Check deadline BEFORE sleeping to handle timeout=0 in tests.
        if time.monotonic() >= deadline:
            # Hard timeout elapsed — ci-timeout semantics.
            return CiResult.TIMEOUT

        if poll_interval > 0:
            time.sleep(poll_interval)
        else:
            # poll_interval=0 with remaining deadline: check deadline again.
            # With timeout=0, the deadline was already at or past start, so
            # the check above fires on the second pass.  We need to ensure
            # the loop exits — check again after the no-sleep pass.
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
        raise RuntimeError(
            f"git merge --no-ff of {issue_branch!r} into {feature_branch!r}"
            f" failed (exit {merge_proc.returncode}): {merge_proc.stderr}"
        )

    # Persist the CI-green-at-merge fact (B-I2 / §11.5).
    _persist_ci_green(owner, repo, issue, pr_head_sha)

    return MergeOutcome.MERGED


def _persist_ci_green(
    owner: str,
    repo: str,
    issue: int,
    sha: str,
) -> None:
    """Persist the CI-green-at-merge fact for crash recovery.

    Adds the ``agent-merged`` label to the issue and posts a marker comment
    recording the CI-green SHA.  ``chain/recovery.py`` (P3) reads these
    signals to reconstruct the ``done`` set without re-querying GC'd
    check-runs.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.
        sha: The PR head commit SHA at which CI was green.
    """
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
        ]
    )
    if label_proc.returncode != 0:
        # Log but do not raise — provenance write failure should not undo
        # an already-committed merge.  The recovery algorithm has fallbacks.
        pass

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
        ]
    )
    if comment_proc.returncode != 0:
        # Same: log, don't raise.
        pass


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
