"""Feature-branch lifecycle management for the always-on daemon.

Owns creating, checking out, and recording the cut-point SHA for the
``feature/<slug>`` branches that hold per-work-unit integration state.

Design decisions (§3.4 / §3.7 / OQ-1):
    - Feature-branch naming: ``feature/<milestone-slug>`` for a milestone
      work unit; ``feature/issue-<N>`` for an un-milestoned N=1 work unit.
      The issue number (not the title slug) is the collision-free key.
    - Branch creation is off ``origin/main`` (the stable base).
    - ``create_feature_branch`` is idempotent: if the branch already exists,
      and ``exist_ok=True`` is passed, the function does not raise.  This
      enables resume semantics when the daemon restarts mid-DAG.
    - ``checkout_feature_branch`` issues ``git -C <repo_root> checkout
      feature/<slug>`` so the repo-root HEAD is the feature branch before
      each ``_run_worker`` call (BLOCKING-1, §3.4).  The ``-C`` flag is
      mandatory — this module NEVER uses a bare ``git checkout`` that would
      rely on the ambient shell cwd.
    - ``record_cut_point`` captures the current tip SHA of the feature branch
      via ``git -C <repo_root> rev-parse feature/<slug>``.  This SHA is the
      worker's cut-point base and is passed to hooks as ``CHAIN_BASE_BRANCH``
      (§3.7) so ``before_run`` and ``after_run`` measure against the correct
      frozen base rather than the live feature tip.

Subprocess style follows the ``chain/gh_deps.py`` pattern: a single
module-local ``_run`` function is the only subprocess seam, making it
trivially patchable in tests (spike finding F8).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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
# Public API
# ---------------------------------------------------------------------------


def feature_branch_name(
    slug: str | None = None,
    issue: int | None = None,
) -> str:
    """Return the canonical feature-branch name for a work unit.

    Exactly one of ``slug`` or ``issue`` must be supplied.

    - Milestone work unit: ``feature_branch_name(slug="my-milestone")``
      → ``"feature/my-milestone"``
    - Un-milestoned single issue:
      ``feature_branch_name(issue=44)`` → ``"feature/issue-44"``

    Args:
        slug: The milestone slug (kebab-case title or explicit identifier).
        issue: The un-milestoned issue number (must be a positive integer).

    Returns:
        The full feature branch name including the ``feature/`` prefix.

    Raises:
        ValueError: If both ``slug`` and ``issue`` are provided, or neither
            is provided, or if ``issue`` is not a positive integer.
    """
    if slug is not None and issue is not None:
        raise ValueError("Provide exactly one of 'slug' or 'issue', not both.")
    if slug is None and issue is None:
        raise ValueError("Provide exactly one of 'slug' or 'issue'.")
    if issue is not None:
        if issue <= 0:
            raise ValueError(
                f"Issue number must be a positive integer, got: {issue!r}"
            )
        return f"feature/issue-{issue}"
    # slug is not None at this point
    return f"feature/{slug}"


def create_feature_branch(
    repo_root: Path,
    branch_name: str,
    exist_ok: bool = False,
) -> None:
    """Create a feature branch off ``origin/main`` in the given repo.

    Uses ``git -C <repo_root> branch <branch_name> origin/main`` so the
    caller's shell cwd is irrelevant — the repo_root is passed explicitly
    via the ``-C`` flag.

    First fetches ``origin/main`` to ensure the local ref is up to date
    before branching.

    Args:
        repo_root: Absolute path to the local repository checkout.
        branch_name: The fully-qualified branch name (e.g.
            ``"feature/my-milestone"`` as returned by
            ``feature_branch_name``).
        exist_ok: If ``True``, silently succeed when the branch already
            exists (resume semantics for daemon restart).  If ``False``
            (default), raise ``RuntimeError`` on any git failure including
            a pre-existing branch.

    Raises:
        RuntimeError: If the git branch creation fails and ``exist_ok`` is
            ``False``, or if it fails for a reason other than the branch
            already existing when ``exist_ok`` is ``True``.
    """
    # Fetch origin/main to ensure the local ref is current.
    fetch_proc = _run(["git", "-C", str(repo_root), "fetch", "origin", "main"])
    if fetch_proc.returncode != 0:
        raise RuntimeError(
            f"git fetch origin main failed (exit {fetch_proc.returncode}):"
            f" {fetch_proc.stderr}"
        )

    branch_proc = _run(
        ["git", "-C", str(repo_root), "branch", branch_name, "origin/main"]
    )
    if branch_proc.returncode != 0:
        if exist_ok and "already exists" in branch_proc.stderr:
            # Branch already exists — idempotent resume; not an error.
            return
        raise RuntimeError(
            f"git branch {branch_name!r} failed"
            f" (exit {branch_proc.returncode}): {branch_proc.stderr}"
        )


def checkout_feature_branch(repo_root: Path, slug: str) -> None:
    """Check out ``feature/<slug>`` as the repo-root HEAD.

    This MUST be called immediately before each ``_run_worker`` invocation
    (BLOCKING-1, §3.4).  Symphony's ``WorkspaceManager.ensure_worktree``
    creates the per-issue worktree with ``git worktree add -b <branch>
    <path> HEAD``; having HEAD on the feature branch ensures the worker
    branch is cut from the correct base.

    The command is ``git -C <repo_root> checkout feature/<slug>``.  The
    ``-C`` flag is mandatory — this function NEVER relies on the ambient
    shell cwd.

    Args:
        repo_root: Absolute path to the local repository checkout.
        slug: The work-unit slug (milestone slug or ``"issue-<N>"``, without
            the ``feature/`` prefix).

    Raises:
        RuntimeError: If git checkout fails (e.g. the branch does not exist
            or there are uncommitted changes in the working tree).
    """
    branch = f"feature/{slug}"
    proc = _run(["git", "-C", str(repo_root), "checkout", branch])
    if proc.returncode != 0:
        raise RuntimeError(
            f"git checkout {branch!r} failed"
            f" (exit {proc.returncode}): {proc.stderr}"
        )


def record_cut_point(repo_root: Path, slug: str) -> str:
    """Return the current tip SHA of ``feature/<slug>`` (the cut-point base).

    Runs ``git -C <repo_root> rev-parse feature/<slug>`` and returns the
    resulting SHA string, stripped of trailing whitespace.

    This SHA is the worker's cut-point merge-base (§3.7).  The daemon passes
    it to hooks via ``CHAIN_BASE_BRANCH`` so ``before_run`` rebases onto the
    correct frozen base and ``after_run`` classifies against it rather than
    the live feature tip.

    Call this AFTER ``checkout_feature_branch`` so the SHA reflects the
    branch tip at the moment the worker is about to be launched.

    Args:
        repo_root: Absolute path to the local repository checkout.
        slug: The work-unit slug (without the ``feature/`` prefix).

    Returns:
        The full 40-character SHA string of the current ``feature/<slug>``
        tip.

    Raises:
        RuntimeError: If git rev-parse fails (e.g. the branch does not
            exist).
    """
    branch = f"feature/{slug}"
    proc = _run(["git", "-C", str(repo_root), "rev-parse", branch])
    if proc.returncode != 0:
        raise RuntimeError(
            f"git rev-parse {branch!r} failed"
            f" (exit {proc.returncode}): {proc.stderr}"
        )
    return proc.stdout.strip()
