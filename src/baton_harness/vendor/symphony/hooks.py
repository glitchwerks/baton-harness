"""symphony/hooks.py — Shell hook executor with timeout."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import NamedTuple

from baton_harness.redact import redact_secrets

log = logging.getLogger("symphony")

# VENDOR-PATCH VP-8 (#351 T3): run_hook returns diagnostics, not a bare bool.
_STDERR_TAIL_MAX_CHARS = 500


class HookResult(NamedTuple):
    """Outcome of a single ``run_hook`` invocation.

    Attributes:
        ok: Whether the hook exited with returncode 0.
        returncode: The subprocess exit code, or ``None`` if the hook
            never ran to completion (e.g. timeout or spawn error).
        stderr_tail: The hook's stderr, redacted of known secret
            patterns and any caller-supplied ``env`` values, truncated
            to the last ``_STDERR_TAIL_MAX_CHARS`` characters.
    """

    ok: bool
    returncode: int | None
    stderr_tail: str


async def run_hook(
    name: str,
    script: str | None,
    cwd: str,
    timeout_ms: int = 60000,
    # VENDOR-PATCH VP-1: run_hook env= threading (merged into os.environ)
    env: dict[str, str] | None = None,
) -> bool | HookResult:
    """Run a shell hook script.

    Args:
        name: Hook name, used only for logging (e.g. "before_run").
        script: The shell script to run. A missing or whitespace-only
            script is treated as a no-op success.
        cwd: Working directory for the hook subprocess.
        timeout_ms: Hook timeout in milliseconds.
        env: Optional overrides merged into ``os.environ`` for the
            hook subprocess.

    Returns:
        ``True`` if ``script`` is empty/whitespace-only (no subprocess
        spawned). Otherwise a ``HookResult`` carrying whether the hook
        succeeded, its real returncode, and a redacted stderr tail.
    """
    if not script or not script.strip():
        return True

    # VENDOR-PATCH VP-1: run_hook env= threading (merged into os.environ)
    # Merge caller-supplied overrides INTO os.environ so that PATH, HOME, and
    # every other inherited var remain accessible to git/gh inside the hook.
    # NEVER pass an overrides-only dict — that strips PATH/HOME and makes
    # git/gh unresolvable (CONCERN-1 in issue #42).
    merged_env: dict[str, str] = {**os.environ, **(env or {})}

    log.info(f"hook:{name} starting in {cwd}")
    try:
        # VENDOR-PATCH VP-7: non-login shell ("-c", not "-lc") — a login
        # shell (-l) forces /etc/profile + ~/.bashrc to run before the hook
        # script, which can clobber daemon-injected env vars (e.g. GH_TOKEN)
        # ahead of the hook ever reading them (issue #215).
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            script,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,  # VENDOR-PATCH VP-1: pass merged env
        )
        timeout_s = max(timeout_ms / 1000, 1)
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )

        # VENDOR-PATCH VP-8 (#351 T3): redact BEFORE truncating — truncating
        # first can split a secret token across the cut, defeating the
        # pattern-based redaction pass. Use the raw `env` overrides (not
        # merged_env, which is dominated by unrelated os.environ noise) as
        # the extra_values pass so an injected token that lacks a known
        # prefix (e.g. echoed into a remote URL) is still caught (F5).
        stderr_tail = redact_secrets(
            stderr.decode(errors="replace"),
            extra_values=(env or {}).values(),
        )[:_STDERR_TAIL_MAX_CHARS]

        if proc.returncode != 0:
            log.error(
                f"hook:{name} failed (rc={proc.returncode}): {stderr_tail}"
            )
            return HookResult(
                ok=False,
                returncode=proc.returncode,
                stderr_tail=stderr_tail,
            )

        log.info(f"hook:{name} completed")
        return HookResult(
            ok=True, returncode=proc.returncode, stderr_tail=stderr_tail
        )

    except asyncio.TimeoutError:
        log.error(f"hook:{name} timed out after {timeout_ms}ms")
        proc.kill()
        return HookResult(
            ok=False,
            returncode=None,
            stderr_tail=f"hook:{name} timed out after {timeout_ms}ms",
        )
    except Exception as e:
        log.error(f"hook:{name} error: {e}")
        return HookResult(
            ok=False,
            returncode=None,
            stderr_tail=redact_secrets(
                str(e), extra_values=(env or {}).values()
            )[:_STDERR_TAIL_MAX_CHARS],
        )
