"""Provenance-hardened crash / unblock recovery reconstruction.

Reconstructs the scheduler ``done``/``parked``/frontier state after a
daemon restart (crash recovery) or after a parked work unit is unblocked
by a human (live re-entry path).  Both paths share one reconstruction
algorithm (§11.5).

B-I2 invariant:
    **Only a daemon-authored, CI-green-marked merge commit is
    authoritative that issue N is done.**  A human ``git merge --no-ff``
    produces no daemon-provenance trailer and is therefore NOT read as
    done.  A bare merge commit (with the trailer) but missing the
    ``agent-merged`` label means the daemon died after merging but before
    writing the label — this is classified under rule 3a (CI gate
    re-entry), NOT done.

Classification precedence (first match wins):

1. **done** — feature branch git log contains a ``--no-ff`` merge commit
   with the exact trailer ``Baton-Harness-Merge: issue-<N> ci=green``
   AND issue carries the ``agent-merged`` label.
2. **ci_gate_reentry (3a)** — provenance merge commit present but
   ``agent-merged`` label absent (daemon died after merge, before marker).
3. **parked_seed** — issue carries ``blocked`` label.
4. **ci_gate_reentry (3a)** — ``agent-done`` + open PR + no
   daemon-provenance merge commit (agent finished; CI-gate/merge was
   interrupted).
5. **redispatch (3b)** — ``agent-in-progress`` orphan (crash mid-worker).
   NOTE: the daemon clears this label, NOT this module — single writer
   invariant (C1).
6. else — not dispatched; fresh frontier.

Single ``_run`` subprocess seam (module-local) makes all ``git``/``gh``
calls patchable in tests (spike finding F8).

Worktree-scan layout assumption:
    The worktree orphan-GC scan (``scan_orphan_worktrees`` and
    ``_parse_worktree_list``) assumes the standard
    ``.symphony/worktrees/<N>`` layout with ``baton/<N>``-prefixed
    branch names (see ``WorkspaceManager`` /
    ``_BATON_BRANCH_PREFIX``).  Worktrees that do not follow this
    layout (non-standard branch names, manually-created worktrees,
    detached HEADs) are silently skipped — they are neither counted
    as orphans nor reported as errors.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    gh_env,
)
from baton_harness.chain.escalation import alert
from baton_harness.chain.subproc import run_cmd

if TYPE_CHECKING:
    from baton_harness.chain.runlog import RunLog

_log = logging.getLogger(__name__)

# Exact trailer written by merge.py at merge time (§11.5 / B-I2).
_PROVENANCE_PREFIX = "Baton-Harness-Merge: issue-"

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
    return run_cmd(cmd, capture=True, text=True, env=env, check=False)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryResult:
    """Classified recovery state for a work unit's membership.

    Each set is mutually exclusive: a given issue number appears in
    exactly one set (or in none, meaning it is a fresh frontier member).

    Attributes:
        done: Issues confirmed done via daemon-provenance merge commit +
            ``agent-merged`` label (rule 1).  The daemon calls
            ``scheduler.mark_done`` for each.
        parked_seed: Issues directly blocked (``blocked`` label).  The
            daemon calls ``scheduler.mark_parked`` for each, which
            handles transitive dependent parking.
        ci_gate_reentry: Issues that need CI-gate re-entry without
            re-running ``_run_worker`` (rules 3a).
        redispatch: Issues that are ``agent-in-progress`` orphans whose
            label the daemon must clear before re-dispatching (rule 3b).
            NOTE: the daemon clears ``agent-in-progress``; this module
            does NOT mutate labels (single writer = daemon, C1).
    """

    done: set[int] = field(default_factory=set)
    parked_seed: set[int] = field(default_factory=set)
    ci_gate_reentry: set[int] = field(default_factory=set)
    redispatch: set[int] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Internal signal gatherers
# ---------------------------------------------------------------------------


def _fetch_provenance_merges(repo_root: Path, feature_branch: str) -> set[int]:
    """Return issue numbers whose daemon-provenance merge commits exist.

    Parses ``git log <feature_branch> --merges --format=%H%x1f%B%x1e``
    and extracts issue numbers from the exact trailer line
    ``Baton-Harness-Merge: issue-<N> ci=green``.

    Args:
        repo_root: Absolute path to the local repository checkout.
        feature_branch: The feature branch to inspect.

    Returns:
        The set of issue numbers found in daemon-provenance merge commits.
        Returns an empty set if the git command fails or no merges exist.
    """
    proc = _run(
        [
            "git",
            "-C",
            str(repo_root),
            "log",
            feature_branch,
            "--merges",
            "--format=%H%x1f%B%x1e",
        ]
    )
    if proc.returncode != 0:
        _log.debug(
            "recovery: git log failed for %s (exit %d): %s",
            feature_branch,
            proc.returncode,
            proc.stderr,
        )
        return set()

    found: set[int] = set()
    # Records are separated by the record-separator (\x1e).
    records = proc.stdout.split("\x1e")
    for record in records:
        record = record.strip()
        if not record:
            continue
        # Each record is sha + \x1f + body.
        parts = record.split("\x1f", 1)
        if len(parts) < 2:
            continue
        body = parts[1]
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(_PROVENANCE_PREFIX):
                # Parse: "Baton-Harness-Merge: issue-<N> ci=green"
                rest = line[len(_PROVENANCE_PREFIX) :]
                # rest is "<N> ci=green"
                token = rest.split()[0] if rest.split() else ""
                try:
                    issue_num = int(token)
                    found.add(issue_num)
                except ValueError:
                    _log.debug(
                        "recovery: could not parse issue number from: %r",
                        line,
                    )
    return found


def _fetch_labels(
    owner: str,
    repo: str,
    issue: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> set[str]:
    """Fetch the current labels for an issue (lowercase).

    Calls ``gh issue view <N> --json labels`` and returns a set of
    lowercase label names.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, the ``gh`` subprocess uses
            a per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated.

    Returns:
        A set of lowercase label name strings.  Returns an empty set on
        error (best-effort; a failed label fetch is not fatal for recovery).
    """
    env = gh_env(installation_token) if installation_token else None
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
        env=env,
    )
    if proc.returncode != 0:
        _log.debug(
            "recovery: gh issue view failed for #%d (exit %d): %s",
            issue,
            proc.returncode,
            proc.stderr,
        )
        return set()
    try:
        data = json.loads(proc.stdout)
        return {lbl["name"].lower() for lbl in data.get("labels", [])}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        _log.debug("recovery: label parse error for #%d: %s", issue, exc)
        return set()


def _fetch_open_prs(
    owner: str,
    repo: str,
    *,
    installation_token: InstallationTokenSource = "",
) -> list[str]:
    """Fetch the head-ref names of all open PRs.

    Calls ``gh pr list --state open --json number,headRefName`` and
    returns the list of ``headRefName`` strings.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, the ``gh`` subprocess uses
            a per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated.

    Returns:
        A list of head-ref names for open PRs.  Returns an empty list on
        error (best-effort).
    """
    env = gh_env(installation_token) if installation_token else None
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
            "number,headRefName",
            "--limit",
            "100",
        ],
        env=env,
    )
    if proc.returncode != 0:
        _log.debug(
            "recovery: gh pr list failed (exit %d): %s",
            proc.returncode,
            proc.stderr,
        )
        return []
    try:
        prs = json.loads(proc.stdout)
        return [str(pr.get("headRefName", "")) for pr in prs]
    except (json.JSONDecodeError, TypeError) as exc:
        _log.debug("recovery: pr list parse error: %s", exc)
        return []


def _has_open_pr(issue: int, open_pr_heads: list[str]) -> bool:
    """Return True if any open PR's head branch matches issue N.

    Mirrors ``tracker.check_pr_exists`` heuristic: matches on branch
    names starting with ``baton/`` and ending with ``-<N>``.

    Args:
        issue: The issue number to check.
        open_pr_heads: List of head-ref names from ``_fetch_open_prs``.

    Returns:
        ``True`` if a ``baton/*-<N>`` branch is in the open PR list.
    """
    suffix = f"-{issue}"
    return any(
        h.startswith("baton/") and h.endswith(suffix) for h in open_pr_heads
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconstruct(
    repo_root: Path,
    owner: str,
    repo: str,
    feature_branch: str,
    membership: frozenset[int],
    *,
    installation_token: InstallationTokenSource = "",
) -> RecoveryResult:
    """Reconstruct the scheduler state for a work unit's membership.

    For each issue N in ``membership``, gathers signals from git history
    and GitHub labels, then classifies it in precedence order (§11.5).

    The four classification sets are mutually exclusive; each issue
    appears in at most one set.  Issues in none of the four sets are
    fresh-frontier members that the scheduler will dispatch.

    NOTE: this function does NOT mutate any GitHub labels.  The daemon
    is the single label writer (C1); ``redispatch`` members indicate
    that the daemon must clear ``agent-in-progress`` before re-dispatching.

    Args:
        repo_root: Absolute path to the local repository checkout.
        owner: The GitHub repository owner.
        repo: The repository name.
        feature_branch: The feature branch to inspect for merge history.
        membership: The frozenset of issue numbers in the current work
            unit.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, forwarded to
            ``_fetch_open_prs`` and ``_fetch_labels`` so each ``gh``
            subprocess call uses a per-call env copy with ``GH_TOKEN``
            overridden — ``os.environ`` is never mutated.

    Returns:
        A ``RecoveryResult`` with the four classification sets populated.
    """
    # Gather provenance merges and open PRs once for the whole unit.
    provenance_merges = _fetch_provenance_merges(repo_root, feature_branch)
    open_pr_heads = _fetch_open_prs(
        owner, repo, installation_token=installation_token
    )

    done: set[int] = set()
    parked_seed: set[int] = set()
    ci_gate_reentry: set[int] = set()
    redispatch: set[int] = set()

    for n in membership:
        labels = _fetch_labels(
            owner, repo, n, installation_token=installation_token
        )
        has_provenance_merge = n in provenance_merges
        has_agent_merged = "agent-merged" in labels
        has_blocked = "blocked" in labels
        has_agent_done = "agent-done" in labels
        has_agent_in_progress = "agent-in-progress" in labels
        has_open_pr = _has_open_pr(n, open_pr_heads)

        # Rule 1: done (highest precedence).
        if has_provenance_merge and has_agent_merged:
            done.add(n)
            continue

        # Rule 2: ci_gate_reentry — provenance merge but label missing.
        if has_provenance_merge and not has_agent_merged:
            ci_gate_reentry.add(n)
            continue

        # Rule 3: parked_seed from blocked label.
        if has_blocked:
            parked_seed.add(n)
            continue

        # Rule 4: ci_gate_reentry — agent-done + open PR, no merge.
        if has_agent_done and has_open_pr:
            ci_gate_reentry.add(n)
            continue

        # Rule 5: redispatch — agent-in-progress orphan.
        if has_agent_in_progress:
            redispatch.add(n)
            continue

        # Rule 6: fresh frontier — no action needed.
        _log.debug(
            "recovery: issue #%d has no special state; fresh frontier",
            n,
        )

    return RecoveryResult(
        done=done,
        parked_seed=parked_seed,
        ci_gate_reentry=ci_gate_reentry,
        redispatch=redispatch,
    )


# ---------------------------------------------------------------------------
# Worktree orphan-GC (IS-5 detect-first) — P1 / #33
# ---------------------------------------------------------------------------

# Label that marks an issue as actively in-flight (IS-5 predicate b).
_AGENT_IN_PROGRESS_LABEL = "agent-in-progress"

# Branch prefix written by WorkspaceManager for issue worktrees
# (e.g. "refs/heads/baton/42").
_BATON_BRANCH_PREFIX = "refs/heads/baton/"


def _parse_worktree_list(
    porcelain: str,
) -> list[tuple[str, int]]:
    """Parse ``git worktree list --porcelain`` output.

    Extracts ``(worktree_path, issue_number)`` pairs from each block.
    Issue number is parsed from branch lines matching
    ``refs/heads/baton/<N>``.  Blocks without a matching branch are
    silently skipped (main worktree, detached HEADs).

    Args:
        porcelain: Raw stdout from ``git worktree list --porcelain``.

    Returns:
        A list of ``(worktree_path, issue_number)`` pairs for worktrees
        whose branch name encodes an issue number.  Only blocks that
        resolved a non-``None`` issue number are included (see the
        flush guards below), so every ``issue_number`` in the
        returned list is a genuine ``int``.
    """
    results: list[tuple[str, int]] = []
    current_path: str | None = None
    current_issue: int | None = None

    for raw_line in porcelain.splitlines():
        line = raw_line.strip()
        if line.startswith("worktree "):
            # Flush previous block if it had a path + issue.
            if current_path is not None and current_issue is not None:
                results.append((current_path, current_issue))
            current_path = line[len("worktree ") :]
            current_issue = None
        elif line.startswith("branch "):
            branch = line[len("branch ") :]
            if branch.startswith(_BATON_BRANCH_PREFIX):
                tail = branch[len(_BATON_BRANCH_PREFIX) :]
                try:
                    current_issue = int(tail)
                except ValueError:
                    current_issue = None

    # Flush the final block.
    if current_path is not None and current_issue is not None:
        results.append((current_path, current_issue))

    return results


def _is_worktree_live(worktree_path: str) -> bool:
    """Return True if the worktree has uncommitted or unpushed changes.

    IS-5 predicate (c): a worktree is live (not reclaimable) if:
    - ``git -C <path> status --porcelain`` returns non-empty output
      (dirty working tree), OR
    - ``git -C <path> log @{u}.. --oneline`` returns non-empty output
      (unpushed commits), OR
    - the upstream check returns non-zero (no upstream configured →
      treat as unpushed, i.e. live).

    Args:
        worktree_path: Absolute path to the worktree directory.

    Returns:
        ``True`` if the worktree has local work that must be preserved.
        ``False`` only when both the tree is clean AND all commits are
        pushed to an upstream branch.
    """
    # Check for uncommitted changes.
    status_proc = _run(["git", "-C", worktree_path, "status", "--porcelain"])
    if status_proc.returncode == 0 and status_proc.stdout.strip():
        return True  # Dirty tree → live.

    # Check for unpushed commits (non-zero exit = no upstream → live).
    log_proc = _run(["git", "-C", worktree_path, "log", "@{u}..", "--oneline"])
    if log_proc.returncode != 0:
        # No upstream configured → treat as unpushed → live.
        return True
    if log_proc.stdout.strip():
        # Unpushed commits present → live.
        return True

    return False


def _fetch_issue_state_and_labels(
    owner: str,
    repo: str,
    issue: int,
) -> tuple[str | None, set[str]]:
    """Fetch state and labels for an issue in a single gh call.

    Calls ``gh issue view <N> --json state,labels`` and returns a
    ``(state, labels)`` tuple.  The ``state`` is the raw GitHub string
    (``"CLOSED"`` or ``"OPEN"``); labels are lowercase strings.

    On any failure (non-zero returncode, parse error) returns
    ``(None, set())`` — callers treat ``None`` state as NOT terminal
    (conservative/live).

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.

    Returns:
        A ``(state, labels)`` tuple.  ``state`` is ``"CLOSED"``,
        ``"OPEN"``, or ``None`` on failure.  ``labels`` is a set of
        lowercase label name strings (empty on failure).
    """
    proc = _run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "state,labels",
        ]
    )
    if proc.returncode != 0:
        _log.debug(
            "recovery: gh issue view (state+labels) failed for"
            " #%d (exit %d): %s",
            issue,
            proc.returncode,
            proc.stderr,
        )
        return None, set()
    try:
        data = json.loads(proc.stdout)
        state: str | None = data.get("state")
        labels = {lbl["name"].lower() for lbl in data.get("labels", [])}
        return state, labels
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        _log.debug("recovery: state+label parse error for #%d: %s", issue, exc)
        return None, set()


async def scan_orphan_worktrees(
    owner: str,
    repo: str,
    *,
    running_issues: frozenset[int],
    worktree_gc: Literal["detect", "reclaim"] = "detect",
    cleanup_worktree: (Callable[[int], Awaitable[None]] | None) = None,
    runlog: RunLog | None = None,
) -> set[int]:
    """Scan for orphaned worktrees and optionally reclaim them.

    Reads ``git worktree list --porcelain`` from the daemon's CWD,
    fetches each worktree's issue state from GitHub, applies the IS-5
    liveness predicate, and returns confirmed orphan issue numbers.

    Terminal-ness is determined per-worktree by fetching issue state
    with ``gh issue view --json state,labels``:

    - ``state == "CLOSED"`` → terminal (orphan-eligible, subject to
      live predicates).
    - ``state == "OPEN"`` → NOT terminal → live (conservative).  This
      includes ``agent-done`` + OPEN (Rule-4 ci_gate_reentry in-flight
      case — worktree still needed).
    - Fetch failure (non-zero returncode or parse error) → NOT terminal
      → live (conservative: unknown state is safe).

    A worktree is a confirmed orphan only when ALL hold:

    1. Issue state is ``"CLOSED"`` (terminal).
    2. Issue is NOT in ``running_issues`` (IS-5 predicate a).
    3. Issue does NOT carry the ``agent-in-progress`` label
       (IS-5 predicate b — label is returned by the same gh call).
    4. The worktree is clean: no uncommitted changes and no unpushed
       commits (IS-5 predicate c).

    The conservatism guarantee: any worktree that is OPEN, fetch-failed,
    running, in-progress-labelled, dirty, or has unpushed commits is
    kept.

    Args:
        owner: GitHub repository owner (for ``gh issue view``).
        repo: GitHub repository name (for ``gh issue view``).
        running_issues: Issue numbers currently in the running or
            membership set; these are never classified as orphans.
        worktree_gc: ``"detect"`` (default) — report orphans, no
            cleanup; ``"reclaim"`` — also call ``cleanup_worktree``
            for each confirmed orphan.
        cleanup_worktree: Async callable that removes the worktree for
            a given issue number.  Only called in ``"reclaim"`` mode.
        runlog: Optional ``RunLog`` handle for best-effort emission
            of ``orphan_worktree`` events.  When ``None``, emission
            is skipped.

    Returns:
        A ``set[int]`` of confirmed orphan issue numbers.  An empty
        set is returned when the git command fails (sweep is guarded —
        callers never see an exception).
    """
    orphans: set[int] = set()

    try:
        list_proc = _run(["git", "worktree", "list", "--porcelain"])
        if list_proc.returncode != 0:
            _log.debug(
                "recovery: git worktree list failed (exit %d): %s",
                list_proc.returncode,
                list_proc.stderr,
            )
            return orphans

        worktrees = _parse_worktree_list(list_proc.stdout)

        for wt_path, issue_num in worktrees:
            # IS-5 predicate (a): issue in running/membership set → live.
            if issue_num in running_issues:
                continue

            # Fetch issue state + labels in one gh call.
            # OPEN or fetch-failure → NOT terminal → live (conservative).
            state, labels = _fetch_issue_state_and_labels(
                owner, repo, issue_num
            )
            if state != "CLOSED":
                continue

            # IS-5 predicate (b): agent-in-progress label → live.
            # Labels already returned by the state+labels fetch above.
            if _AGENT_IN_PROGRESS_LABEL in labels:
                continue

            # IS-5 predicate (c): dirty or unpushed → live.
            if _is_worktree_live(wt_path):
                continue

            # All live predicates clear → confirmed orphan.
            orphans.add(issue_num)

            alert(
                owner,
                repo,
                issue_num,
                (
                    f"Orphaned worktree detected for issue #{issue_num}"
                    f" at {wt_path}; terminal and no live work present."
                ),
                severity="warn",
                kind="debug",
                runlog=runlog,
            )

            if runlog is not None:
                try:
                    # best-effort: emit failures must never break the scan.
                    runlog.emit(
                        {
                            "event": "orphan_worktree",
                            "issue": issue_num,
                            "worktree": wt_path,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

            # Reclaim only in explicit opt-in mode and only confirmed
            # orphans — never in detect mode (IS-5 GC-vs-redispatch
            # guarantee).
            if worktree_gc == "reclaim" and cleanup_worktree is not None:
                await cleanup_worktree(issue_num)

    except Exception as exc:  # noqa: BLE001
        _log.debug(
            "recovery: scan_orphan_worktrees failed: %s; returning empty set",
            exc,
        )

    return orphans
