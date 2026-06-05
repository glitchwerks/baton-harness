"""Hook: after_create — per-worktree dependency setup.

Invoked by Baton immediately after a new worktree is created (before the
agent's first run).  Responsible for any per-worktree dependency installation
that cannot be shared across worktrees (e.g. ``npm install``, ``pip install``
for project-local packages).

Entry point: ``bh-after-create`` (defined in ``pyproject.toml``).

WORKFLOW.md hook line (issue #5)::

    after_create: bh-after-create

Context:
    The hook runs with ``$PWD`` set to the newly created worktree directory.
    The issue number is inferred from ``basename($PWD)`` via
    ``baton_harness._cli.resolve_issue_number`` (spike finding F2: Baton
    passes no env-var context to hooks).

TODO(#2): Implement per-worktree dependency installation logic.
"""

from __future__ import annotations

import sys

from baton_harness._cli import log, resolve_issue_number

#: Short name used in log/err prefixes.
_HOOK = "after-create"


def main() -> int:
    """Entry point for the ``bh-after-create`` console script.

    Resolves the issue number from the current working directory and logs a
    placeholder message.  The real dependency-installation logic is
    implemented in issue #2.

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

    # TODO(#2): Replace this stub with actual per-worktree dependency setup
    # (npm install / pip install as appropriate for the project type).
    log(_HOOK, issue, "not yet implemented — see issue #2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
