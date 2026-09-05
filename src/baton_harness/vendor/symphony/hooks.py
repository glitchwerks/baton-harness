"""symphony/hooks.py — Shell hook executor with timeout."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import NamedTuple

from baton_harness.chain.identity import Identity, env_for
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
) -> HookResult:
    """Run a shell hook script.

    Args:
        name: Hook name. Only "before_run" may receive an explicit
            worker PAT override.
        script: The shell script to run. A missing or whitespace-only
            script is treated as a no-op success.
        cwd: Working directory for the hook subprocess.
        timeout_ms: Hook timeout in milliseconds.
        env: Optional overrides merged into ``os.environ`` before worker
            identity filtering. Explicit GH_TOKEN/GITHUB_TOKEN overrides
            are restored only for the before_run hook.

    Returns:
        A ``HookResult`` carrying whether the hook succeeded, its real
        returncode, and a redacted stderr tail. An empty/whitespace-only
        ``script`` short-circuits to a success ``HookResult`` (``ok=True,
        returncode=None, stderr_tail=""``) without spawning a subprocess.
    """
    if not script or not script.strip():
        return HookResult(ok=True, returncode=None, stderr_tail="")

    # Filter after merging so overrides cannot restore daemon authority.
    # PATH/HOME and other non-privileged settings remain available (#42).
    merged_env = env_for(
        Identity.WORKER, base_env={**os.environ, **(env or {})}
    )
    if name == "before_run":
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            if env and key in env:
                merged_env[key] = env[key]

    # VENDOR-PATCH VP-9 (#362, CodeRabbit follow-up): capture the vault
    # token ONCE, here, at the same moment merged_env is built — this is
    # the ambient value to redact defensively even though it is filtered
    # out of the subprocess environment. Reading
    # os.environ.get("BWS_ACCESS_TOKEN", "") again later (after
    # communicate()) is a TOCTOU race: if the parent rotates the token
    # mid-run, a fresh late read redacts the NEW value while the
    # hook diagnostics may still contain the OLD one. Both redaction call
    # sites below must reuse this single captured value.
    bws_access_token = os.environ.get("BWS_ACCESS_TOKEN", "")

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
        # VENDOR-PATCH VP-9 (#362): also redact BWS_ACCESS_TOKEN, which
        # lives in the daemon's ambient os.environ and is never threaded
        # through `env=` (hook_env only ever carries GH_TOKEN/GITHUB_TOKEN).
        # Reuse the value captured at merged_env construction time (see
        # above) rather than re-reading os.environ here — avoids the
        # TOCTOU race described there.
        stderr_tail = redact_secrets(
            stderr.decode(errors="replace"),
            extra_values=[
                *(env or {}).values(),
                bws_access_token,
            ],
        )[-_STDERR_TAIL_MAX_CHARS:]

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
                str(e),
                extra_values=[
                    *(env or {}).values(),
                    bws_access_token,
                ],
            )[-_STDERR_TAIL_MAX_CHARS:],
        )
