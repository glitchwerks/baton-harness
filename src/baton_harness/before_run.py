"""Hook: before_run — sync the worktree branch onto the chain base.

Invoked by Baton once, before the first turn of each run.  Fetches the latest
ref and rebases the current worktree branch onto it so the agent always
operates on a fresh baseline.

The rebase target is controlled by the ``CHAIN_BASE_BRANCH`` environment
variable (default ``origin/main``).  The daemon threads this variable when
running per-issue branches inside a milestone work unit, so that the branch
rebases onto ``feature/<slug>`` rather than ``main``.  The ref is resolved
to a concrete SHA at entry (``git rev-parse <ref>``) to avoid moving-target
problems on ``--no-ff`` feature branches (B-I1, §3.7 of the chain spec).

On rebase conflict the hook calls ``git rebase --abort`` to restore the
worktree to a clean state before returning non-zero.  Baton sees the
non-zero exit and can surface the failure rather than leaving the
worktree in a mid-rebase limbo.

Entry point: ``bh-before-run`` (defined in ``pyproject.toml``).

WORKFLOW.md hook line (issue #5)::

    before_run: bh-before-run

Context:
    The hook runs with ``$PWD`` set to the worktree directory.  The issue
    number is inferred from ``basename($PWD)`` via
    ``baton_harness._cli.resolve_issue_number`` (spike finding F2).
    Baton names worktrees ``<repo>/.symphony/worktrees/<issue>`` (a bare
    integer); the harness's own convention is ``<repo>/.worktrees/<branch>``
    (``<prefix>-<issue>[-<slug>]``).  Both forms are accepted.

    All subprocess calls use ``encoding="utf-8"`` explicitly to avoid
    Windows cp1252 mangling of non-ASCII git output.
"""

from __future__ import annotations

import os
import subprocess
import sys

from baton_harness._cli import err, log, resolve_issue_number

#: Environment variable controlling the rebase target.  Set by the daemon
#: to ``feature/<slug>`` for milestone work units; defaults to
#: ``origin/main`` for flat (N=1 DAG / un-milestoned) runs.
_ENV_CHAIN_BASE_BRANCH = "CHAIN_BASE_BRANCH"
_DEFAULT_BASE = "origin/main"

#: Short name used in log/err prefixes.
_HOOK = "before-run"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command and return its CompletedProcess.

    Streams stdout/stderr to the terminal so Baton's log captures git
    output in real time.  Always uses ``encoding="utf-8"`` to avoid
    Windows cp1252 mangling of non-ASCII branch or commit names.

    Note: ``.stdout`` is always ``None`` on the returned result because
    output is not captured — callers must only inspect ``.returncode``.
    Use :func:`_run_capture` when ``.stdout`` is needed.

    Args:
        cmd: The command and arguments to execute.

    Returns:
        The :class:`subprocess.CompletedProcess` result with ``returncode``
        populated and ``.stdout`` always ``None`` (streaming, not captured).
    """
    return subprocess.run(
        cmd,
        encoding="utf-8",
        check=False,
    )


def _run_capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with captured stdout/stderr.

    Unlike :func:`_run`, this helper captures stdout so callers can read
    ``.stdout``.  Used for commands whose output must be consumed by the
    hook (e.g. ``git rev-parse`` to obtain a SHA).  Always uses
    ``encoding="utf-8"`` to avoid Windows cp1252 mangling of non-ASCII
    branch or commit names.

    Args:
        cmd: The command and arguments to execute.

    Returns:
        The :class:`subprocess.CompletedProcess` result with
        ``returncode``, ``stdout``, and ``stderr`` all populated.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    """Entry point for the ``bh-before-run`` console script.

    Performs a branch sync in up to three steps:

    1. ``git fetch origin main`` — brings the remote ref up to date.
       **Skipped** when ``CHAIN_BASE_BRANCH`` is set: the daemon has already
       prepared a concrete local cut-point (spec §3.7), so fetching
       ``main`` is both wrong (it is not the base) and unnecessary.
    2. ``git rev-parse <CHAIN_BASE_BRANCH>`` — resolves the base ref to a
       concrete SHA at entry.  This prevents moving-target problems on
       ``--no-ff`` feature branches (B-I1, chain spec §3.7).
       ``CHAIN_BASE_BRANCH`` defaults to ``origin/main`` when unset.
    3. ``git rebase <resolved-sha>`` — fast-forwards or replays the current
       branch on top of the frozen base SHA.

    If rebase succeeds (exit 0 — including the already-up-to-date case),
    the hook returns 0.  If rebase fails (conflict or other error),
    ``git rebase --abort`` is called to restore clean state, then the hook
    returns non-zero so Baton sees the failure.

    Args:
        argv: Unused; accepted for interface symmetry with other hooks.

    Returns:
        ``0`` on success; ``1`` when the issue number cannot be resolved or
        the base ref cannot be resolved; non-zero when fetch or rebase fails.
    """
    issue = resolve_issue_number()
    if issue is None:
        print(
            f"[{_HOOK}] error: could not derive issue number from cwd — "
            "expected a bare integer (Baton: .symphony/worktrees/<issue>) "
            "or <prefix>-<issue>[-<slug>] (harness: .worktrees/<branch>)",
            file=sys.stderr,
            flush=True,
        )
        return 1

    # Determine the base ref from CHAIN_BASE_BRANCH (default origin/main).
    base_ref = os.environ.get(_ENV_CHAIN_BASE_BRANCH, _DEFAULT_BASE)
    log(_HOOK, issue, f"chain base ref: {base_ref!r}")

    # Step 1: fetch latest main from remote — only on the flat path.
    #
    # When CHAIN_BASE_BRANCH is UNSET (flat / un-milestoned run, base is
    # origin/main): fetch so the remote ref is up to date before resolving.
    #
    # When CHAIN_BASE_BRANCH IS set (chain path): the daemon has already
    # prepared a concrete cut-point SHA/ref that is local (spec §3.7).
    # Fetching origin main is both wrong (it is not the base) and
    # unnecessary; skip it entirely.
    chain_base_set = _ENV_CHAIN_BASE_BRANCH in os.environ
    if not chain_base_set:
        fetch_cmd = ["git", "fetch", "origin", "main"]
        log(_HOOK, issue, f"running {' '.join(fetch_cmd)}")
        fetch_result = _run(fetch_cmd)
        if fetch_result.returncode != 0:
            err(
                _HOOK,
                issue,
                f"git fetch failed (exit {fetch_result.returncode})",
            )
            return fetch_result.returncode
    else:
        log(
            _HOOK,
            issue,
            "CHAIN_BASE_BRANCH is set — skipping git fetch origin main "
            "(base is a local cut-point SHA prepared by the daemon)",
        )

    # Step 2: resolve base ref to a concrete SHA.
    # Uses _run_capture (not _run) so that .stdout contains the SHA.
    # _run streams to terminal and always returns .stdout=None — reading
    # .stdout from a streaming result yields AttributeError (issue #63).
    rev_parse_cmd = ["git", "rev-parse", base_ref]
    log(_HOOK, issue, f"running {' '.join(rev_parse_cmd)}")
    rev_parse_result = _run_capture(rev_parse_cmd)
    if rev_parse_result.returncode != 0:
        err(
            _HOOK,
            issue,
            f"git rev-parse {base_ref!r} failed "
            f"(exit {rev_parse_result.returncode}) — "
            "ensure CHAIN_BASE_BRANCH names a reachable ref",
        )
        return rev_parse_result.returncode
    base_sha = rev_parse_result.stdout.strip()
    log(_HOOK, issue, f"resolved {base_ref!r} → {base_sha}")

    # Step 3: rebase current branch onto the resolved (frozen) SHA.
    rebase_cmd = ["git", "rebase", base_sha]
    log(_HOOK, issue, f"running {' '.join(rebase_cmd)}")
    rebase_result = _run(rebase_cmd)

    if rebase_result.returncode == 0:
        log(
            _HOOK,
            issue,
            f"branch is up to date with {base_ref!r} ({base_sha})",
        )
        return 0

    # Rebase failed — abort to restore clean state, then report.
    err(
        _HOOK,
        issue,
        f"rebase onto {base_ref!r} ({base_sha}) failed "
        f"(exit {rebase_result.returncode})"
        " — aborting to restore clean state",
    )
    abort_cmd = ["git", "rebase", "--abort"]
    log(_HOOK, issue, f"running {' '.join(abort_cmd)}")
    _run(abort_cmd)

    err(
        _HOOK,
        issue,
        "branch sync failed — manual intervention required",
    )
    return rebase_result.returncode


if __name__ == "__main__":
    sys.exit(main())
