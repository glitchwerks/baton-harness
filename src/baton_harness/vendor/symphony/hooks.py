"""symphony/hooks.py — Shell hook executor with timeout."""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("symphony")


async def run_hook(
    name: str,
    script: str | None,
    cwd: str,
    timeout_ms: int = 60000,
    # VENDOR-PATCH VP-1: run_hook env= threading (merged into os.environ)
    env: dict[str, str] | None = None,
) -> bool:
    """Run a shell hook script. Returns True on success, False on failure."""
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

        if proc.returncode != 0:
            log.error(
                f"hook:{name} failed (rc={proc.returncode}): "
                f"{stderr.decode()[:500]}"
            )
            return False

        log.info(f"hook:{name} completed")
        return True

    except asyncio.TimeoutError:
        log.error(f"hook:{name} timed out after {timeout_ms}ms")
        proc.kill()
        return False
    except Exception as e:
        log.error(f"hook:{name} error: {e}")
        return False
