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

import re
import subprocess
from pathlib import Path

from baton_harness.chain.identity import Identity, env_for

# Conservative pattern for a valid git ref component used as a branch slug.
# Allows alphanumerics, dots, hyphens, and forward-slashes (for sub-paths).
# Rejects: empty strings, leading hyphens, leading slashes, whitespace, and
# any character outside [A-Za-z0-9._/-].
_VALID_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

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
        env: Optional subprocess environment. Defaults to the worker
            identity env so local git commands inherit PATH-like vars
            without inheriting privileged GitHub auth keys.

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr.
        Callers inspect ``returncode`` themselves.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env if env is not None else env_for(Identity.WORKER),
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

    Slug validation (FIX 6):
        The slug must be a non-empty string matching ``[A-Za-z0-9._/-]+``
        and must not start with ``-`` or ``/``.  These are the characters
        safe for use as a git ref component.  Providing an invalid slug
        raises ``ValueError`` before any git command is issued.

    Args:
        slug: The milestone slug (kebab-case title or explicit identifier).
            Must be a valid git ref component: non-empty, no leading ``-``
            or ``/``, no whitespace, only ``[A-Za-z0-9._/-]`` characters.
        issue: The un-milestoned issue number (must be a positive integer).

    Returns:
        The full feature branch name including the ``feature/`` prefix.

    Raises:
        ValueError: If both ``slug`` and ``issue`` are provided, or neither
            is provided, or if ``issue`` is not a positive integer, or if
            ``slug`` contains characters invalid in a git ref component.
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
    # slug is not None at this point — validate before constructing ref.
    _validate_slug(slug)  # type: ignore[arg-type]
    return f"feature/{slug}"


def _validate_slug(slug: str) -> None:
    """Validate that ``slug`` is safe to use as a git ref component.

    A slug must be non-empty, must not start with ``-`` or ``/``, must
    contain no whitespace, and may only contain ``[A-Za-z0-9._/-]``.

    Args:
        slug: The slug string to validate.

    Raises:
        ValueError: If the slug is empty, starts with ``-`` or ``/``,
            contains whitespace, or contains characters outside the allowed
            set.
    """
    if not slug:
        raise ValueError(
            "slug must be a non-empty string; got an empty string."
        )
    if not _VALID_SLUG_RE.match(slug):
        raise ValueError(
            f"slug {slug!r} is not a valid git ref component.  A slug must"
            " start with an alphanumeric character and may only contain"
            " [A-Za-z0-9._/-].  Leading '-' or '/' and whitespace are not"
            " allowed."
        )


def create_feature_branch(
    repo_root: Path,
    branch_name: str,
    exist_ok: bool = False,
) -> None:
    """Create a feature branch in the given repo, honouring resume semantics.

    On first run:
        Creates ``branch_name`` off ``origin/main`` via
        ``git -C <repo_root> branch <branch_name> origin/main``.

    On resume (``exist_ok=True``):
        1. If the local branch already exists, return immediately (idempotent).
        2. If ``origin/<branch_name>`` exists (checked via
           ``git ls-remote --heads origin <branch_name>``), create the local
           branch FROM the remote, preserving integration history (FIX 3 /
           §11.5 resume idempotency):
           ``git -C <repo_root> branch <branch_name> origin/<branch_name>``
        3. Otherwise create from ``origin/main`` as usual.

    The caller's shell cwd is irrelevant — the repo_root is passed explicitly
    via the ``-C`` flag on every git command.

    First fetches ``origin`` to ensure remote refs are up to date before
    checking or branching.

    Args:
        repo_root: Absolute path to the local repository checkout.
        branch_name: The fully-qualified branch name (e.g.
            ``"feature/my-milestone"`` as returned by
            ``feature_branch_name``).
        exist_ok: If ``True``, silently succeed when the branch already
            exists locally, or track the remote branch if it exists.  If
            ``False`` (default), raise ``RuntimeError`` on any git failure
            including a pre-existing branch.

    Raises:
        RuntimeError: If the git branch creation fails and ``exist_ok`` is
            ``False``, or if it fails for a reason other than the branch
            already existing when ``exist_ok`` is ``True``.
    """
    # Fetch origin to ensure remote refs are current before any probe.
    fetch_proc = _run(["git", "-C", str(repo_root), "fetch", "origin"])
    if fetch_proc.returncode != 0:
        raise RuntimeError(
            f"git fetch origin failed (exit {fetch_proc.returncode}):"
            f" {fetch_proc.stderr}"
        )

    # When exist_ok=True, check for a remote branch BEFORE attempting to
    # create from origin/main.  On a restart in a fresh clone/worktree the
    # local branch may be absent while origin/<branch_name> carries the
    # integration history we must preserve (FIX 3 / §11.5 resume idempotency).
    if exist_ok:
        ls_proc = _run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-remote",
                "--heads",
                "origin",
                branch_name,
            ]
        )
        if ls_proc.returncode == 0 and ls_proc.stdout.strip():
            # Remote branch exists — check if local also exists.
            local_check = _run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "rev-parse",
                    "--verify",
                    branch_name,
                ]
            )
            if local_check.returncode == 0:
                # Both local and remote exist — already tracked; idempotent.
                return
            # Local absent, remote present — create local tracking remote.
            track_proc = _run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "branch",
                    branch_name,
                    f"origin/{branch_name}",
                ]
            )
            if track_proc.returncode != 0:
                raise RuntimeError(
                    f"git branch {branch_name!r} from"
                    f" origin/{branch_name!r} failed"
                    f" (exit {track_proc.returncode}):"
                    f" {track_proc.stderr}"
                )
            return

    # No remote branch (or exist_ok=False) — attempt to create from
    # origin/main.
    branch_proc = _run(
        ["git", "-C", str(repo_root), "branch", branch_name, "origin/main"]
    )
    if branch_proc.returncode != 0:
        if exist_ok and "already exists" in branch_proc.stderr:
            # Local branch already exists — idempotent resume; not an error.
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
