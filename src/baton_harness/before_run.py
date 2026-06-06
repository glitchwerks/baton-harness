"""Hook: before_run — sync the worktree branch onto ``main``.

Invoked by Baton before each agent run turn.  Fetches the latest
``origin/main`` and rebases the current worktree branch onto it so the
agent always operates on a fresh baseline.

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

import subprocess
import sys

from baton_harness._cli import err, log, resolve_issue_number

#: Short name used in log/err prefixes.
_HOOK = "before-run"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command and return its CompletedProcess.

    Streams stdout/stderr to the terminal so Baton's log captures git
    output in real time.  Always uses ``encoding="utf-8"`` to avoid
    Windows cp1252 mangling of non-ASCII branch or commit names.

    Args:
        cmd: The command and arguments to execute.

    Returns:
        The :class:`subprocess.CompletedProcess` result with ``returncode``
        populated.
    """
    return subprocess.run(
        cmd,
        encoding="utf-8",
        check=False,
    )


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    """Entry point for the ``bh-before-run`` console script.

    Performs a two-step branch sync:

    1. ``git fetch origin main`` — brings the remote ref up to date.
    2. ``git rebase origin/main`` — fast-forwards or replays the current
       branch on top of ``origin/main``.

    If rebase succeeds (exit 0 — including the already-up-to-date case),
    the hook returns 0.  If rebase fails (conflict or other error),
    ``git rebase --abort`` is called to restore clean state, then the hook
    returns non-zero so Baton sees the failure.

    Args:
        argv: Unused; accepted for interface symmetry with other hooks.

    Returns:
        ``0`` on success; ``1`` when the issue number cannot be resolved;
        non-zero when fetch or rebase fails.
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

    # Step 1: fetch latest main from remote.
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

    # Step 2: rebase current branch onto origin/main.
    rebase_cmd = ["git", "rebase", "origin/main"]
    log(_HOOK, issue, f"running {' '.join(rebase_cmd)}")
    rebase_result = _run(rebase_cmd)

    if rebase_result.returncode == 0:
        log(_HOOK, issue, "branch is up to date with origin/main")
        return 0

    # Rebase failed — abort to restore clean state, then report.
    err(
        _HOOK,
        issue,
        f"rebase onto origin/main failed (exit {rebase_result.returncode})"
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
