"""Hook: after_run — outcome classification and GitHub label reconciliation.

Invoked by Baton after each agent run turn completes.  Responsible for:

1. Classifying the run outcome into one of the states defined in
   ``harness-design.md §5``:

   - ``uncommitted-changes`` — agent left changes but did not commit.
   - ``no-commits`` — agent ran but produced no changes.
   - ``committed-no-pr`` — commits were made but no PR was opened.
   - ``pr-opened`` — a PR is open for the worktree branch (success path).

2. Reconciling GitHub labels on the issue to a single state label
   (``agent-ready``, ``agent-done``, or ``blocked``), enforcing the
   invariant that exactly one state label is present at any time.

Entry point: ``bh-after-run`` (defined in ``pyproject.toml``).

WORKFLOW.md hook line (issue #5)::

    after_run: bh-after-run

Context:
    The hook runs with ``$PWD`` set to the worktree directory.  The issue
    number is inferred from ``basename($PWD)`` via
    ``baton_harness._cli.resolve_issue_number`` (spike finding F2).

    GitHub API calls use ``gh --json`` output parsed via ``json.loads``,
    never shell-grepped (addresses the pattern flagged in PR #9; see the
    language-decision rationale in ``harness-design.md``).

    All subprocess calls must use ``encoding="utf-8"`` explicitly (Windows
    cp1252 footgun — see Python skill notes).

    This hook must finish under the 60 s timeout enforced by Baton
    (spike finding F11).

TODO(#3): Implement outcome classification and label-reconciliation logic.
"""

from __future__ import annotations

import sys

from baton_harness._cli import log, resolve_issue_number

#: Short name used in log/err prefixes.
_HOOK = "after-run"


def main() -> int:
    """Entry point for the ``bh-after-run`` console script.

    Resolves the issue number from the current working directory and logs a
    placeholder message.  The real outcome-classification and
    label-reconciliation logic is implemented in issue #3.

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

    # TODO(#3): Replace this stub with outcome classification + GitHub label
    # reconciliation.  Must parse `gh --json` output via json.loads (not grep),
    # apply the H1 fix (enforce single state label), and finish within 60 s.
    log(_HOOK, issue, "not yet implemented — see issue #3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
