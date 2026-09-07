"""Dependency-free subprocess runner shared by the ``_run`` wrappers.

Every ``chain/`` module that shells out (``branches``, ``merge``,
``escalation``, ``gh_deps``, ``recovery``, ``daemon``) previously defined
its own module-local ``_run`` helper with a near-identical
``subprocess.run(..., encoding="utf-8")`` body. :func:`run_cmd` is the
single place that Windows cp1252-vs-UTF-8 decoding guard now lives.

This module is intentionally stdlib-only (Principle 3, #268 plan) so it
can be imported from any layer of the package without risking an import
cycle. Module-specific concerns — such as ``branches``' default of
``env_for(Identity.WORKER)`` when ``env`` is ``None``, or ``merge``'s
"inherit ``os.environ`` unchanged" default — are **not** encoded here;
each caller's thin wrapper is responsible for its own default and keeps
its own module-local ``_run``/``_run_gh`` symbol so existing
``mock.patch("...<module>._run")`` test doubles keep working unchanged.
"""

from __future__ import annotations

import subprocess


def run_cmd(
    cmd: list[str],
    *,
    capture: bool = True,
    text: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run an external command and return its completed process.

    Centralises subprocess invocation so every ``chain/`` caller shares
    one ``encoding="utf-8"`` guard (Windows defaults to cp1252, which
    silently mangles non-ASCII ``git``/``gh`` output) instead of each
    module reimplementing it.

    Args:
        cmd: Command and arguments to execute (no shell interpolation).
        capture: When ``True`` (the default), captures stdout/stderr via
            ``subprocess.run(capture_output=True)``. Pass ``False`` to
            let the child inherit the parent's stdout/stderr and stream
            directly to the terminal.
        text: When ``True`` (the default), decode stdout/stderr as text
            using the ``encoding="utf-8"`` guard. Pass ``False`` to
            receive raw ``bytes``.
        env: Optional subprocess environment. ``None`` means "no
            override" — the exact meaning of that (inherit
            ``os.environ`` unchanged, or substitute a computed default)
            is a caller-side decision made by each wrapper, not by this
            function.
        timeout: Optional deadline in seconds forwarded to
            ``subprocess.run``. ``None`` (the default) means no
            deadline. Raises ``subprocess.TimeoutExpired`` if the
            deadline elapses.
        check: When ``True`` (the default), raises
            ``subprocess.CalledProcessError`` on a non-zero exit code.
            Every current caller of this module passes ``check=False``
            explicitly to preserve its pre-migration behavior — callers
            that want ``subprocess.run``'s own default must ask for it
            explicitly, not rely on this function's default silently
            matching theirs.

    Returns:
        A ``subprocess.CompletedProcess`` with the command's result.
        Callers inspect ``returncode`` themselves unless ``check=True``
        raises first.
    """
    if text:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=timeout,
            check=check,
        )
    # Bytes mode: not exercised by any Phase 1 caller (all migrated
    # wrappers use text=True); kept for the stream-family migration
    # (#268 plan Phase 2), which is out of scope for this change.
    return subprocess.run(  # type: ignore[return-value]
        cmd,
        capture_output=capture,
        env=env,
        timeout=timeout,
        check=check,
    )
