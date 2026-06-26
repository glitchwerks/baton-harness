"""Slice 3b — ``force-pr-not-merge`` Claude Code PreToolUse hook.

Reads a Claude Code PreToolUse payload from stdin and:

  - On a merge-pattern match: drops sentinel file
    ``${PWD}/.bh-state/worker-tried-merge`` AND exits 2 (block) with a
    ``BH_WORKER_TRIED_MERGE:`` stderr marker.
  - Otherwise: exits 0 (allow).

The sentinel file is the LOAD-BEARING signal — ``after_run._classify()``
inspects it as its first step. The stderr marker is live-tail debugging
only.

Defense-in-depth Layer 5 mechanism per ``docs/architecture-spec.md``
§3.5; the GitHub Repository Ruleset is the actual boundary.

Payload shape (per Anthropic Docs, fetched 2026-06-23):
    {"tool_name": "Bash", "tool_input": {"command": "<the command>"}}
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TextIO

_SENTINEL_DIR = ".bh-state"
_SENTINEL_NAME = "worker-tried-merge"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Cheap direct match: `gh pr merge` in any form.
_RE_GH_PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")

# Safe (non-mutating) flags that follow `gh pr merge` — when ALL tokens after
# the subcommand are from this set, the invocation is reconnaissance only.
# Regex: optional whitespace + (--help | --dry-run) repeated, then end-of-
# string or a shell separator (|, &, ;, newline).
_RE_GH_PR_MERGE_SAFE_SUFFIX = re.compile(
    r"\bgh\s+pr\s+merge\b((?:\s+(?:--help|--dry-run))*)\s*(?:$|[|&;\n])",
)

# Three independent substring checks for the gh-api family.  Composing them
# in any-order beats a single-regex that requires flags BEFORE the URL
# (C3 — `gh api repos/o/r/pulls/42/merge --method PUT` evaded the old form).
_RE_GH_API = re.compile(r"\bgh\s+api\b")
_RE_PULLS_MERGE = re.compile(r"\bpulls/\d+/merge\b")
_RE_PUT_METHOD = re.compile(r"(?:-X[=\s]+PUT|--method[=\s]+PUT)")

# curl direct API — same any-order composition.
_RE_CURL = re.compile(r"\bcurl\b")
_RE_CURL_PUT = re.compile(r"(?:-X[=\s]+PUT|--request[=\s]+PUT)")

#: Maximum command length echoed in the marker (avoid runaway stderr).
_MAX_ECHO_LEN: int = 200


def _sanitise(command: str) -> str:
    """Strip control characters and truncate long commands.

    Args:
        command: Raw command string from the payload.

    Returns:
        A printable, length-capped representation.
    """
    s = re.sub(r"[\x00-\x1f]+", " ", command)
    if len(s) > _MAX_ECHO_LEN:
        s = s[:_MAX_ECHO_LEN] + "…"
    return s


def _is_gh_pr_merge_safe(command: str) -> bool:
    """Return True if the ``gh pr merge`` invocation carries ONLY safe flags.

    "Safe" means non-mutating reconnaissance flags (``--help``, ``--dry-run``)
    with no PR number or action flags that would actually trigger a merge.

    A compound command containing MORE THAN ONE ``gh pr merge`` occurrence is
    never safe — the safe-suffix gate only evaluates the first match, so a
    compound like ``gh pr merge --help; gh pr merge 42`` would otherwise evade
    the block (C4 bypass).

    Args:
        command: Full Bash command string.

    Returns:
        True when the only tokens after ``gh pr merge`` are safe flags and the
        command contains exactly one ``gh pr merge`` invocation.
    """
    # Option A (C4 fix): more than one `gh pr merge` occurrence → not safe.
    if len(_RE_GH_PR_MERGE.findall(command)) > 1:
        return False
    m = _RE_GH_PR_MERGE_SAFE_SUFFIX.search(command)
    if m is None:
        return False
    suffix_tokens = m.group(1).split()
    safe = {"--help", "--dry-run"}
    return all(t in safe for t in suffix_tokens)


def _match(command: str) -> str | None:
    """Return a short label identifying the matched pattern, or None.

    Args:
        command: The Bash command string extracted from the PreToolUse payload.

    Returns:
        A short label string (e.g. ``"gh-pr-merge"``) if the command matches
        a known merge pattern, otherwise ``None``.
    """
    if _RE_GH_PR_MERGE.search(command):
        # Allow non-mutating reconnaissance invocations (--help, --dry-run),
        # but only if the entire command is safe.  If safe, fall through to
        # check the other merge patterns (e.g. a compound command may have a
        # safe ``gh pr merge --help`` segment followed by ``gh api ... PUT``).
        if not _is_gh_pr_merge_safe(command):
            return "gh-pr-merge"
    if (
        _RE_GH_API.search(command)
        and _RE_PULLS_MERGE.search(command)
        and _RE_PUT_METHOD.search(command)
    ):
        return "gh-api-pulls-merge"
    if (
        _RE_CURL.search(command)
        and _RE_PULLS_MERGE.search(command)
        and _RE_CURL_PUT.search(command)
    ):
        return "curl-pulls-merge"
    return None


def _drop_sentinel() -> None:
    """Create ``${PWD}/.bh-state/worker-tried-merge`` as an empty file.

    Failure is swallowed (logged to stderr but not fatal) — exiting 2 is
    still the contract that blocks the tool call; the sentinel is a belt-
    and-braces signal for the daemon's after_run hook.
    """
    try:
        sentinel_dir = Path.cwd() / _SENTINEL_DIR
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        (sentinel_dir / _SENTINEL_NAME).touch()
    except OSError as exc:
        print(
            f"force-pr-not-merge: WARNING: could not write sentinel: {exc}",
            file=sys.stderr,
        )


def main(*, stdin: TextIO | None = None) -> int:
    """Read PreToolUse payload from stdin; block on merge attempts.

    Args:
        stdin: Optional stdin override (used by tests). Defaults to
            ``sys.stdin``.

    Returns:
        ``0`` to allow tool invocation; ``2`` to block. The non-zero
        return + stderr text is the contract Claude Code uses to block.
    """
    src = stdin if stdin is not None else sys.stdin
    raw = src.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Fail-open: a parser bug here must NOT block legitimate work.
        # The ruleset is the real boundary. Log to stderr for debugging.
        print(
            "force-pr-not-merge: WARNING: stdin was not JSON — allowing",
            file=sys.stderr,
        )
        return 0

    tool = payload.get("tool_name", "")
    if tool != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str):
        return 0

    label = _match(command)
    if label is None:
        return 0

    _drop_sentinel()
    print(
        f"BH_WORKER_TRIED_MERGE: tool=Bash matched_pattern={label} "
        f"command={_sanitise(command)}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
