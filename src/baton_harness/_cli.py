"""Shared CLI helpers used by all three hook entry points.

This module provides two utilities that every hook needs:

1. ``resolve_issue_number`` — derives the GitHub issue number from the
   current worktree directory path.  Baton does not pass env-var context
   to hooks (spike finding F2), so the issue number is inferred from
   ``basename(cwd)``, which follows the harness worktree naming convention
   ``<prefix>-<issue>-<slug>`` (e.g. ``feat-10-python-scaffold``).

2. ``log`` / ``err`` — emit a prefixed line to stdout/stderr in the style
   used by ``bin/run.sh`` and consistent with the shell scripts it replaced:
   ``[<hook> #<issue>] <message>``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Pattern: a word-character prefix, a dash, one-or-more digits, then either
# end-of-string or a dash followed by anything.
# Matches: feat-10-python-scaffold, fix-42-auth-bug, chore-7-cleanup
_ISSUE_RE = re.compile(r"^[a-zA-Z][\w]*-(\d+)(?:-.*)?$")


def resolve_issue_number(path: Path | None = None) -> int | None:
    """Derive the GitHub issue number from a worktree directory path.

    Baton passes no environment variables to hook scripts (spike finding F2),
    so the issue number is inferred from the worktree directory name, which
    follows the convention ``<prefix>-<issue>[-<slug>]``.

    Examples::

        feat-10-python-scaffold → 10
        fix-42-auth-bug         → 42
        chore-7-cleanup         → 7

    Args:
        path: Directory whose ``basename`` is examined.  When ``None``,
            the process's current working directory is used.

    Returns:
        The integer issue number extracted from the directory name, or
        ``None`` if the name does not match the expected convention.
    """
    target = path if path is not None else Path.cwd()
    basename = target.name
    match = _ISSUE_RE.match(basename)
    if match is None:
        return None
    return int(match.group(1))


def log(hook: str, issue: int, message: str) -> None:
    """Write a prefixed informational line to stdout.

    The format mirrors the logging style used by ``bin/run.sh``::

        [<hook> #<issue>] <message>

    Args:
        hook: Short hook identifier (e.g. ``"after-run"``).
        issue: GitHub issue number for the current worktree.
        message: Human-readable message body.
    """
    print(f"[{hook} #{issue}] {message}", flush=True)


def err(hook: str, issue: int, message: str) -> None:
    """Write a prefixed error line to stderr.

    The format is identical to ``log`` but targets ``sys.stderr``::

        [<hook> #<issue>] <message>

    Args:
        hook: Short hook identifier (e.g. ``"after-run"``).
        issue: GitHub issue number for the current worktree.
        message: Human-readable error message body.
    """
    print(f"[{hook} #{issue}] {message}", file=sys.stderr, flush=True)
