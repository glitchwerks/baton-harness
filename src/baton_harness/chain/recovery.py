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
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

# Exact trailer written by merge.py at merge time (§11.5 / B-I2).
_PROVENANCE_PREFIX = "Baton-Harness-Merge: issue-"

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


def _fetch_labels(owner: str, repo: str, issue: int) -> set[str]:
    """Fetch the current labels for an issue (lowercase).

    Calls ``gh issue view <N> --json labels`` and returns a set of
    lowercase label names.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.

    Returns:
        A set of lowercase label name strings.  Returns an empty set on
        error (best-effort; a failed label fetch is not fatal for recovery).
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
            "labels",
        ]
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


def _fetch_open_prs(owner: str, repo: str) -> list[str]:
    """Fetch the head-ref names of all open PRs.

    Calls ``gh pr list --state open --json number,headRefName`` and
    returns the list of ``headRefName`` strings.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.

    Returns:
        A list of head-ref names for open PRs.  Returns an empty list on
        error (best-effort).
    """
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
        ]
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

    Returns:
        A ``RecoveryResult`` with the four classification sets populated.
    """
    # Gather provenance merges and open PRs once for the whole unit.
    provenance_merges = _fetch_provenance_merges(repo_root, feature_branch)
    open_pr_heads = _fetch_open_prs(owner, repo)

    done: set[int] = set()
    parked_seed: set[int] = set()
    ci_gate_reentry: set[int] = set()
    redispatch: set[int] = set()

    for n in membership:
        labels = _fetch_labels(owner, repo, n)
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
