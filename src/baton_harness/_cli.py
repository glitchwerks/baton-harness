"""Shared CLI helpers used by all three hook entry points.

This module provides two utilities that every hook needs:

1. ``resolve_issue_number`` — derives the GitHub issue number from the
   current worktree directory path.  Baton does not pass env-var context
   to hooks (spike finding F2), so the issue number is inferred from
   ``basename(cwd)``.  Two naming forms are accepted:

   * **Baton (symphony) form** — the directory name is a bare integer,
     e.g. ``.symphony/worktrees/2``.  This is Baton's default: it names
     worktrees after the plain issue number.
   * **Harness prefixed form** — ``<prefix>-<issue>[-<slug>]``, e.g.
     ``.worktrees/feat-10-python-scaffold`` or ``.worktrees/chore-7``.
     Used by this project's own worktree convention.

2. ``log`` / ``err`` — emit a prefixed line to stdout/stderr in the style
   used by ``bin/run.sh`` and consistent with the shell scripts it replaced:
   ``[<hook> #<issue>] <message>``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Pattern: optional word-character prefix+dash, one-or-more digits (captured),
# then either end-of-string or a dash followed by anything.
#
# Accepted forms:
#   "2"                       → Baton bare-issue (symphony worktree)
#   "12345"                   → Baton bare-issue (multi-digit)
#   "feat-10-python-scaffold" → harness prefixed form with slug
#   "fix-42-auth-bug"         → harness prefixed form with slug
#   "chore-7"                 → harness prefixed form, no slug
_ISSUE_RE = re.compile(r"^(?:[a-zA-Z][\w]*-)?(\d+)(?:-.*)?$")


def resolve_issue_number(path: Path | None = None) -> int | None:
    """Derive the GitHub issue number from a worktree directory path.

    Baton passes no environment variables to hook scripts (spike finding F2),
    so the issue number is inferred from the worktree directory name.

    Two naming forms are accepted:

    * **Baton (symphony) form** — the directory name is a bare integer.
      Baton names worktrees ``<repo>/.symphony/worktrees/<issue>``, so
      ``basename(path)`` is just the issue number (e.g. ``"2"``).
    * **Harness prefixed form** — ``<prefix>-<issue>[-<slug>]``.  Used by
      this project's own ``.worktrees/<branch>`` convention.

    Examples::

        "2"                       → 2   (Baton bare-issue)
        "feat-10-python-scaffold" → 10  (harness prefixed + slug)
        "fix-42-auth-bug"         → 42  (harness prefixed + slug)
        "chore-7"                 → 7   (harness prefixed, no slug)

    Args:
        path: Directory whose ``basename`` is examined.  When ``None``,
            the process's current working directory is used.

    Returns:
        The integer issue number extracted from the directory name, or
        ``None`` if the name does not match either accepted form.
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
