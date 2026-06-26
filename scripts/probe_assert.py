"""Assertion helpers for the merge-denial probe (slice 3c, #160).

Called by ``bin/probe-merge-denial.sh`` via ``python -c`` or as a
module.  All functions print a single JSON line to stdout so the
calling Bash script can parse them with portable shell tools.

Output format per function::

    {"ok": true|false, "reason": "<human-readable string>"}

Exit code mirrors ``ok``: 0 when ``ok`` is true, 1 when false.

Usage from Bash::

    result=$(python -m scripts.probe_assert check_exit_code 2 "$actual_exit")
    python -m scripts.probe_assert check_http_403 "$response_body"
    python -m scripts.probe_assert check_sentinel "/path/to/.bh-state"
    python -m scripts.probe_assert check_stderr_marker "$stderr_output"
    python -m scripts.probe_assert summarise 7 7 0  # total pass fail

All functions are importable for unit testing without subprocess
overhead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path  # noqa: F401 — used in check_sentinel

# ---------------------------------------------------------------------------
# Low-level pass/fail emitter
# ---------------------------------------------------------------------------


def _emit(ok: bool, reason: str) -> int:
    """Print a JSON result line and return the appropriate exit code.

    Args:
        ok: Whether the assertion passed.
        reason: Human-readable explanation for the result.

    Returns:
        0 if ok is True, 1 otherwise.
    """
    print(json.dumps({"ok": ok, "reason": reason}))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Individual assertion functions (return int exit code)
# ---------------------------------------------------------------------------


def check_exit_code(expected: int, actual: int) -> int:
    """Assert that *actual* exit code is non-zero (denied).

    For the probe we only care that the command was DENIED (exit != 0).
    The ``expected`` param is kept for documentation / future exact
    checks but the assertion is intentionally loose: any non-zero exit
    is a denial.

    Args:
        expected: The exit code we expect (typically 2 for hook-blocked
            vectors, or 1 for curl/ruleset-denied vectors).
        actual: The exit code the command produced.

    Returns:
        0 if actual != 0 (correctly denied), 1 if actual == 0 (merge
        succeeded — a probe FAIL).
    """
    if actual == 0:
        return _emit(
            False,
            f"command exited 0 (merge NOT denied); expected non-zero"
            f" (wanted {expected})",
        )
    return _emit(
        True,
        f"exit code {actual} (non-zero = denied; expected ~{expected})",
    )


def check_http_403(response_body: str) -> int:
    """Assert that *response_body* contains a 403 denial indicator.

    The GitHub Rulesets API returns HTTP 403 with a JSON body that
    includes the ruleset name when a protected operation is blocked.
    The probe checks for both ``403`` (as a substring) and the
    canonical ruleset name ``harness-main-no-merge``.

    Args:
        response_body: The combined stdout+stderr from the gh/curl
            invocation.

    Returns:
        0 if both markers are present (correctly denied by ruleset),
        1 otherwise.
    """
    has_403 = "403" in response_body
    has_ruleset = "harness-main-no-merge" in response_body
    if has_403 and has_ruleset:
        return _emit(True, "response contains 403 and ruleset name")
    missing: list[str] = []
    if not has_403:
        missing.append("403")
    if not has_ruleset:
        missing.append("harness-main-no-merge")
    return _emit(
        False,
        f"response missing: {', '.join(missing)}"
        f" (body snippet: {response_body[:120]!r})",
    )


def check_sentinel(state_dir: str) -> int:
    """Assert that the hook sentinel file exists under *state_dir*.

    The ``force-pr-not-merge`` hook writes
    ``<cwd>/.bh-state/worker-tried-merge`` when it fires.  The probe
    runs each hook-covered vector from a temp directory and checks for
    the sentinel there.

    Args:
        state_dir: Path to the ``.bh-state`` directory to check (the
            probe passes ``<tmpdir>/.bh-state`` for each vector).

    Returns:
        0 if the sentinel file exists, 1 otherwise.
    """
    sentinel = Path(state_dir) / "worker-tried-merge"
    if sentinel.exists():
        return _emit(True, f"sentinel found: {sentinel}")
    return _emit(False, f"sentinel NOT found at: {sentinel}")


def check_stderr_marker(stderr_output: str) -> int:
    """Assert stderr contains the hook's BH_WORKER_TRIED_MERGE marker.

    The ``force-pr-not-merge`` hook prints
    ``BH_WORKER_TRIED_MERGE:`` to stderr when it fires.

    Args:
        stderr_output: Captured stderr from the hook-covered command.

    Returns:
        0 if the marker is present, 1 otherwise.
    """
    marker = "BH_WORKER_TRIED_MERGE:"
    if marker in stderr_output:
        return _emit(True, "BH_WORKER_TRIED_MERGE marker found in stderr")
    return _emit(
        False,
        f"BH_WORKER_TRIED_MERGE marker NOT found in stderr"
        f" (snippet: {stderr_output[:120]!r})",
    )


def summarise(total: int, passed: int, failed: int) -> int:
    """Emit a probe summary and return 0 (all pass) or 1 (any fail).

    Args:
        total: Total number of vectors attempted.
        passed: Count of vectors that passed their assertion.
        failed: Count of vectors that failed their assertion.

    Returns:
        0 if failed == 0, 1 otherwise.
    """
    ok = failed == 0
    reason = (
        f"{passed}/{total} vectors denied as expected"
        if ok
        else f"{failed}/{total} vectors FAILED (unexpected success or"
        f" wrong response shape)"
    )
    return _emit(ok, reason)


# ---------------------------------------------------------------------------
# CLI dispatch (called by the Bash probe via `python -m scripts.probe_assert`)
# ---------------------------------------------------------------------------

_COMMANDS: dict[str, object] = {
    "check_exit_code": check_exit_code,
    "check_http_403": check_http_403,
    "check_sentinel": check_sentinel,
    "check_stderr_marker": check_stderr_marker,
    "summarise": summarise,
}


def _usage() -> None:
    """Print usage and exit 2."""
    print(
        "Usage: python -m scripts.probe_assert <command> [args...]\n"
        "Commands:\n"
        "  check_exit_code <expected:int> <actual:int>\n"
        "  check_http_403  <response_body:str>\n"
        "  check_sentinel  <state_dir:str>\n"
        "  check_stderr_marker <stderr_output:str>\n"
        "  summarise <total:int> <passed:int> <failed:int>",
        file=sys.stderr,
    )
    sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — dispatch to the named assertion function.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code from the dispatched function.
    """
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        _usage()
    cmd = args[0]
    fn_args = args[1:]
    if cmd not in _COMMANDS:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        _usage()
    # Convert positional args to appropriate types per command.
    if cmd == "check_exit_code":
        if len(fn_args) != 2:  # noqa: PLR2004
            _usage()
        return check_exit_code(int(fn_args[0]), int(fn_args[1]))
    if cmd == "check_http_403":
        if len(fn_args) != 1:
            _usage()
        return check_http_403(fn_args[0])
    if cmd == "check_sentinel":
        if len(fn_args) != 1:
            _usage()
        return check_sentinel(fn_args[0])
    if cmd == "check_stderr_marker":
        if len(fn_args) != 1:
            _usage()
        return check_stderr_marker(fn_args[0])
    if cmd == "summarise":
        if len(fn_args) != 3:  # noqa: PLR2004
            _usage()
        return summarise(int(fn_args[0]), int(fn_args[1]), int(fn_args[2]))
    _usage()
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
