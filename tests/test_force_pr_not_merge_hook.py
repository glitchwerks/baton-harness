"""Slice 3b — force_pr_not_merge PreToolUse hook unit tests.

Drives main() with stdin payloads modelled on Claude Code's PreToolUse
schema. The hook MUST do two things on a merge-pattern match:

  1. Exit non-zero with stderr beginning ``BH_WORKER_TRIED_MERGE:``
  2. Drop a sentinel file at ``${PWD}/.bh-state/worker-tried-merge``

The sentinel is the load-bearing signal that ``after_run._classify()``
reads — the stderr marker is live-tail debugging only.
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from baton_harness.hooks import force_pr_not_merge
from baton_harness.hooks.force_pr_not_merge import main as hook_main


@pytest.fixture
def in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the hook with cwd set to tmp_path so sentinel paths are isolated."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _run(payload: dict) -> tuple[int, str]:
    """Invoke main() with the given payload on stdin, capture (rc, stderr)."""
    stdin = io.StringIO(json.dumps(payload))
    stderr_buf = io.StringIO()
    stdout_buf = io.StringIO()
    with redirect_stderr(stderr_buf), redirect_stdout(stdout_buf):
        rc = hook_main(stdin=stdin)
    return rc, stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Block cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Direct gh pr merge — all flag arrangements.
        "gh pr merge 42",
        "gh pr merge --auto 42",
        "gh pr merge 42 --squash",
        # Flag-first form (gh api).
        "gh api -X PUT repos/o/r/pulls/42/merge",
        "gh api --method PUT repos/o/r/pulls/42/merge",
        # URL-first form (C3 regression — original ordered-regex missed these).
        "gh api repos/o/r/pulls/42/merge -X PUT",
        "gh api repos/o/r/pulls/42/merge --method PUT",
        # curl direct API.
        "curl -X PUT https://api.github.com/repos/o/r/pulls/42/merge",
        "curl https://api.github.com/repos/o/r/pulls/42/merge -X PUT",
        # Piped / chained forms.
        "something | gh pr merge 42",
        "gh pr merge 42 && echo ok",
    ],
)
def test_blocks_merge_attempts(in_tmp_cwd: Path, command: str) -> None:
    """Every known merge invocation form is blocked with non-zero exit."""
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, f"expected block for: {command!r}"
    assert stderr.startswith("BH_WORKER_TRIED_MERGE:"), (
        f"stderr marker missing for {command!r}: {stderr!r}"
    )
    # Sentinel must exist — load-bearing signal for after_run (B2).
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists(), f"sentinel missing for: {command!r}"


# ---------------------------------------------------------------------------
# Allow cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --draft --base feature/x --title T --body B",
        "gh pr view 42",
        "gh pr list",
        "gh pr status",
        "gh issue comment 42 --body hi",
        "git push -u origin HEAD",
        # gh api call without /merge — must NOT block.
        "gh api repos/o/r/pulls/42",
        # gh pr merge with help flag (non-mutating, should pass).
        "gh pr merge --help",
    ],
)
def test_allows_legitimate_commands(in_tmp_cwd: Path, command: str) -> None:
    """Legitimate gh/git commands that do not merge are passed through."""
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc == 0, f"unexpectedly blocked: {command!r}\nstderr={stderr}"
    assert stderr == "", f"unexpected stderr for {command!r}: {stderr!r}"
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert not sentinel.exists(), f"sentinel wrongly created for: {command!r}"


# ---------------------------------------------------------------------------
# Non-Bash tool pass-through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "Grep"])
def test_passes_through_non_bash_tools(in_tmp_cwd: Path, tool: str) -> None:
    """PreToolUse payloads for non-Bash tools exit 0 with no side-effects."""
    rc, stderr = _run({"tool_name": tool, "tool_input": {"file_path": "/x"}})
    assert rc == 0
    assert stderr == ""
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_malformed_stdin_is_safe_default_allow(in_tmp_cwd: Path) -> None:
    """If stdin is not JSON, hook fails-open (exit 0, no sentinel).

    The ruleset is the actual boundary; a parser bug here must NOT block
    legitimate work.
    """
    stdin = io.StringIO("not-json")
    stderr_buf = io.StringIO()
    with redirect_stderr(stderr_buf):
        rc = hook_main(stdin=stdin)
    assert rc == 0
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# Sentinel directory auto-creation
# ---------------------------------------------------------------------------


def test_sentinel_dir_is_auto_created(in_tmp_cwd: Path) -> None:
    """The .bh-state/ directory is created automatically if absent."""
    assert not (in_tmp_cwd / ".bh-state").exists()
    _run({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 42"}})
    assert (in_tmp_cwd / ".bh-state").is_dir()
    assert (in_tmp_cwd / ".bh-state" / "worker-tried-merge").exists()


# ---------------------------------------------------------------------------
# Stderr marker format
# ---------------------------------------------------------------------------


def test_stderr_marker_contains_required_tokens(in_tmp_cwd: Path) -> None:
    """Block stderr marker contains tool=Bash, matched_pattern=, command=."""
    command = "gh pr merge 42"
    _, stderr = _run({"tool_name": "Bash", "tool_input": {"command": command}})
    assert "tool=Bash" in stderr, f"missing tool=Bash in: {stderr!r}"
    assert "matched_pattern=" in stderr, (
        f"missing matched_pattern= in: {stderr!r}"
    )
    assert "command=" in stderr, f"missing command= in: {stderr!r}"


# ---------------------------------------------------------------------------
# C3 regression: URL-first form explicitly
# ---------------------------------------------------------------------------


def test_c3_url_first_gh_api_method_put_is_blocked(in_tmp_cwd: Path) -> None:
    """``gh api <url> --method PUT`` (URL before flag) MUST block.

    C3 regression — the original anchored regex required the PUT flag to
    appear before the URL and silently allowed this form.
    """
    command = "gh api repos/o/r/pulls/42/merge --method PUT"
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, "URL-first --method PUT form must be blocked (C3)"
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists(), "sentinel must be written for C3 URL-first form"


def test_c3_url_first_gh_api_x_put_is_blocked(in_tmp_cwd: Path) -> None:
    """``gh api <url> -X PUT`` (URL before short flag) MUST block (C3)."""
    command = "gh api repos/o/r/pulls/42/merge -X PUT"
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, "URL-first -X PUT form must be blocked (C3)"
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists()


# ---------------------------------------------------------------------------
# Compound-command bypass regression (C4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "separator",
    [
        ";",
        "&&",
        "||",
        "|",
        "\n",
    ],
)
def test_compound_command_bypass_does_not_evade_block(
    in_tmp_cwd: Path, separator: str
) -> None:
    """Compound commands with a safe first segment must NOT bypass the block.

    C4 regression — ``gh pr merge --help; gh pr merge 42`` previously allowed
    because ``_is_gh_pr_merge_safe()`` only inspected the first ``gh pr merge``
    segment and returned True, causing ``_match()`` to return None (allow).

    Every separator form must produce: non-zero exit, BH_WORKER_TRIED_MERGE:
    on stderr, and sentinel written.
    """
    command = f"gh pr merge --help {separator} gh pr merge 42"
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, (
        f"compound bypass must be blocked for separator "
        f"{separator!r}: {command!r}"
    )
    assert stderr.startswith("BH_WORKER_TRIED_MERGE:"), (
        f"stderr marker missing for separator {separator!r}: {stderr!r}"
    )
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists(), (
        f"sentinel missing for separator {separator!r}: {command!r}"
    )


def test_compound_safe_first_then_api_put_still_blocks(
    in_tmp_cwd: Path,
) -> None:
    """Compound: safe ``gh pr merge --help`` then ``gh api PUT merge`` blocks.

    Segment 1 is a safe ``gh pr merge --help`` (would be allowed alone).
    Segment 2 is a ``gh api ... PUT ... pulls/N/merge`` attack pattern caught
    independently by the C3 regex.  The combined command must block because
    segment 2 matches the gh-api-pulls-merge pattern.
    """
    command = "gh pr merge --help && gh api -X PUT repos/o/r/pulls/42/merge"
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, f"safe-first + api-PUT compound must block: {command!r}"
    assert stderr.startswith("BH_WORKER_TRIED_MERGE:"), (
        f"stderr marker missing: {stderr!r}"
    )
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists(), (
        "sentinel missing for safe-first+api-PUT compound"
    )


# ---------------------------------------------------------------------------
# P2-B: equals-form flag separator regression (codex review PR #158)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # gh api: equals form, flag before URL.
        "gh api --method=PUT repos/o/r/pulls/42/merge",
        # gh api: equals form, flag after URL (any-order).
        "gh api repos/o/r/pulls/42/merge --method=PUT",
        # curl: equals form with --request.
        "curl --request=PUT https://api.github.com/repos/o/r/pulls/42/merge",
        # curl: equals form with short -X.
        "curl -X=PUT https://api.github.com/repos/o/r/pulls/42/merge",
    ],
)
def test_equals_form_method_flag_is_blocked(
    in_tmp_cwd: Path, command: str
) -> None:
    """P2-B: ``--method=PUT`` / ``--request=PUT`` / ``-X=PUT`` must block.

    The codex review (PR #158) found that the original patterns only
    accepted whitespace-separated flags (``--method PUT``) and allowed
    the equals form (``--method=PUT``) as a bypass of the worker stop
    signal.  After the P2-B fix, all equals forms must produce: non-zero
    exit, ``BH_WORKER_TRIED_MERGE:`` on stderr, and sentinel written.
    """
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, f"expected block for equals-form command: {command!r}"
    assert stderr.startswith("BH_WORKER_TRIED_MERGE:"), (
        f"stderr marker missing for {command!r}: {stderr!r}"
    )
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists(), f"sentinel missing for: {command!r}"


def test_whitespace_form_still_blocked_after_p2b_fix(
    in_tmp_cwd: Path,
) -> None:
    r"""P2-B regression: whitespace form (``--method PUT``) still blocks.

    Ensures the widened ``[=\s]+`` pattern does not break the original
    whitespace-separated form that was already covered.
    """
    command = "gh api --method PUT repos/o/r/pulls/42/merge"
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, "whitespace-form --method PUT must still be blocked"
    assert stderr.startswith("BH_WORKER_TRIED_MERGE:"), (
        f"stderr marker missing: {stderr!r}"
    )
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists(), "sentinel must exist for whitespace-form block"


@pytest.mark.parametrize("pattern", ["_RE_PUT_METHOD", "_RE_CURL_PUT"])
def test_put_method_patterns_do_not_carry_dead_start_anchor(
    pattern: str,
) -> None:
    r"""Regex tripwire patterns must not carry an inert ``^`` alternation.

    Issue #159 flagged the old ``(?:^|\\s)`` branch as dead without
    ``re.MULTILINE``. Keep the pattern honest by asserting either a true
    multiline anchor or, as implemented here, no ``^`` branch at all.
    """
    compiled = getattr(force_pr_not_merge, pattern)
    assert "^" not in compiled.pattern, (
        f"{pattern} should not contain a dead start-anchor branch: "
        f"{compiled.pattern!r}"
    )
    assert not (compiled.flags & re.MULTILINE), (
        f"{pattern} unexpectedly relies on re.MULTILINE: "
        f"flags={compiled.flags!r}"
    )
