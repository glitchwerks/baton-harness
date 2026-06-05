"""Hook: before_run — sync the worktree branch onto ``main``.

Invoked by Baton before each agent run turn.  Responsible for ensuring the
worktree's branch is up to date with ``main`` so the agent always operates
on a fresh baseline.

Entry point: ``bh-before-run`` (defined in ``pyproject.toml``).

WORKFLOW.md hook line (issue #5)::

    before_run: bh-before-run

Context:
    The hook runs with ``$PWD`` set to the worktree directory.  The issue
    number is inferred from ``basename($PWD)`` via
    ``baton_harness._cli.resolve_issue_number`` (spike finding F2).

    Branch sync is performed via ``git`` subprocesses.  All subprocess calls
    must use ``encoding="utf-8"`` explicitly to avoid Windows cp1252 mangling
    of non-ASCII output.

TODO(#2): Implement branch-sync logic (git fetch + rebase/merge onto main).
"""

from __future__ import annotations

import sys

from baton_harness._cli import log, resolve_issue_number

#: Short name used in log/err prefixes.
_HOOK = "before-run"


def main() -> int:
    """Entry point for the ``bh-before-run`` console script.

    Resolves the issue number from the current working directory and logs a
    placeholder message.  The real branch-sync logic is implemented in
    issue #2.

    Returns:
        Exit code: ``0`` on success (including stub path), ``1`` on failure
        to resolve the issue number.
    """
    issue = resolve_issue_number()
    if issue is None:
        print(
            f"[{_HOOK}] error: could not derive issue number from cwd — "
            "worktree name must match <prefix>-<issue>[-<slug>]",
            file=sys.stderr,
            flush=True,
        )
        return 1

    # TODO(#2): Replace this stub with actual branch-sync logic.
    # Implementation should: git fetch origin main, then rebase the current
    # branch onto origin/main.  Must finish well under the 60 s hook timeout
    # (spike finding F11).
    log(_HOOK, issue, "not yet implemented — see issue #2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
