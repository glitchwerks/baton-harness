"""Tests for #130: _build_claude_args must deny PR-merge tools.

Frozen contract: the arg list returned by Worker._build_claude_args MUST
always contain a ``--disallowed-tools`` flag whose value tokens include:
  - ``Bash(gh pr merge*)``   (deny Bash sub-command pattern)
  - ``mcp__github__merge_pull_request``  (deny MCP tool)

This denial applies regardless of permission_mode, because ``--disallowed-
tools`` deny rules win even under ``--dangerously-skip-permissions``.

These tests fail until the #130 fix is applied to worker.py; they must
not be edited by the implementer.

Coverage map:
- test_disallowed_tools_flag_present_bypass:
    flag present with permission_mode="bypassPermissions"
- test_disallowed_tools_flag_present_accept_edits:
    flag present with permission_mode="acceptEdits"
- test_disallowed_tools_flag_present_none_mode:
    flag present when permission_mode is None/unset (falsy branch)
- test_bash_merge_pattern_in_deny_list (parametrized over all modes):
    Bash(gh pr merge*) is in the deny-list values
- test_mcp_merge_tool_in_deny_list (parametrized over all modes):
    mcp__github__merge_pull_request is in the deny-list values
- test_disallowed_tools_values_follow_flag:
    deny tokens appear as individual argv elements after the flag
"""

from __future__ import annotations

import pytest

from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.worker import Worker

# ---------------------------------------------------------------------------
# Constants — the exact deny tokens the #130 fix must produce.
# ---------------------------------------------------------------------------

DENY_BASH_MERGE = "Bash(gh pr merge*)"
DENY_MCP_MERGE = "mcp__github__merge_pull_request"

# All permission_mode values the method branches on or ignores.
ALL_MODES = [
    pytest.param("bypassPermissions", id="bypass"),
    pytest.param("acceptEdits", id="accept_edits"),
    pytest.param(None, id="no_mode"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _worker(permission_mode: str | None = "acceptEdits") -> Worker:
    """Return a Worker with a minimal WorkflowConfig.

    Args:
        permission_mode: Value to assign to WorkflowConfig.permission_mode.
            Pass None to exercise the falsy branch inside _build_claude_args.

    Returns:
        A Worker instance ready to call _build_claude_args.
    """
    config = WorkflowConfig(
        agent_command="claude",
        permission_mode=permission_mode,  # type: ignore[arg-type]
    )
    return Worker(config=config)


def _args(permission_mode: str | None) -> list[str]:
    """Call _build_claude_args with a minimal prompt/cwd/skills.

    Args:
        permission_mode: Forwarded to _worker().

    Returns:
        The list[str] produced by _build_claude_args.
    """
    return _worker(permission_mode)._build_claude_args(
        prompt="Do the work.",
        cwd="/tmp/fake",
        issue_skills=[],
    )


def _deny_values(args: list[str]) -> list[str]:
    """Extract all value tokens that follow ``--disallowed-tools`` in args.

    Collects every argv element after ``--disallowed-tools`` up to the next
    flag (element starting with ``-``) or end of list.

    Args:
        args: The full argv list from _build_claude_args.

    Returns:
        The list of deny-token strings; empty if the flag is absent.
    """
    try:
        idx = args.index("--disallowed-tools")
    except ValueError:
        return []

    values: list[str] = []
    for token in args[idx + 1 :]:
        if token.startswith("-"):
            break
        values.append(token)
    return values


# ---------------------------------------------------------------------------
# 1. Flag presence — one test per permission_mode
# ---------------------------------------------------------------------------


def test_disallowed_tools_flag_present_bypass() -> None:
    """--disallowed-tools flag is in args for bypassPermissions mode.

    This is the highest-risk mode (--dangerously-skip-permissions is also
    added); the deny-list must be present here above all else.
    """
    args = _args("bypassPermissions")
    assert "--disallowed-tools" in args, (
        f"--disallowed-tools missing from bypassPermissions args: {args}"
    )


def test_disallowed_tools_flag_present_accept_edits() -> None:
    """--disallowed-tools flag is in args for permission_mode acceptEdits."""
    args = _args("acceptEdits")
    assert "--disallowed-tools" in args, (
        f"--disallowed-tools missing from acceptEdits args: {args}"
    )


def test_disallowed_tools_flag_present_none_mode() -> None:
    """--disallowed-tools flag is in args when permission_mode is None.

    The falsy branch in _build_claude_args skips the permission flag
    entirely; the deny-list must still be appended.
    """
    args = _args(None)
    assert "--disallowed-tools" in args, (
        f"--disallowed-tools missing from no-mode args: {args}"
    )


# ---------------------------------------------------------------------------
# 2. Bash merge pattern present — parametrized over all modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES)
def test_bash_merge_pattern_in_deny_list(mode: str | None) -> None:
    """Bash(gh pr merge*) appears in the --disallowed-tools value tokens.

    Args:
        mode: permission_mode value for this parametrize run.
    """
    args = _args(mode)
    values = _deny_values(args)
    assert DENY_BASH_MERGE in values, (
        f"'{DENY_BASH_MERGE}' not found in --disallowed-tools values "
        f"(mode={mode!r}). deny values={values}, full args={args}"
    )


# ---------------------------------------------------------------------------
# 3. MCP merge tool present — parametrized over all modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES)
def test_mcp_merge_tool_in_deny_list(mode: str | None) -> None:
    """mcp__github__merge_pull_request appears in --disallowed-tools values.

    Args:
        mode: permission_mode value for this parametrize run.
    """
    args = _args(mode)
    values = _deny_values(args)
    assert DENY_MCP_MERGE in values, (
        f"'{DENY_MCP_MERGE}' not found in --disallowed-tools values "
        f"(mode={mode!r}). deny values={values}, full args={args}"
    )


# ---------------------------------------------------------------------------
# 4. Shape sanity — deny tokens are well-formed argv elements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES)
def test_disallowed_tools_values_follow_flag_immediately(
    mode: str | None,
) -> None:
    """Deny tokens are argv elements directly after --disallowed-tools.

    Claude Code CLI expects: ``--disallowed-tools <tok1> <tok2> ...``
    (each token a separate argv element).  Asserts that both required
    tokens appear in the contiguous block after the flag with no
    intervening flags between them and the ``--disallowed-tools`` marker.

    Args:
        mode: permission_mode value for this parametrize run.
    """
    args = _args(mode)
    values = _deny_values(args)
    assert DENY_BASH_MERGE in values, (
        f"Shape check: '{DENY_BASH_MERGE}' not an argv element after "
        f"--disallowed-tools (mode={mode!r}). values={values}, args={args}"
    )
    assert DENY_MCP_MERGE in values, (
        f"Shape check: '{DENY_MCP_MERGE}' not an argv element after "
        f"--disallowed-tools (mode={mode!r}). values={values}, args={args}"
    )
    # The flag itself must not appear in the values block (no duplication).
    assert "--disallowed-tools" not in values, (
        "Malformed args: --disallowed-tools appears in its own value block"
    )
