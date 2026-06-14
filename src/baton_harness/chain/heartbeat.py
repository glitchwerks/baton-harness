"""Thread-based heartbeat and in-daemon stall detection.

This module provides two concerns that are deliberately kept separate:

1. **Liveness** — ``_write_heartbeat`` writes a timestamp file each tick
   so an external dead-man's-switch monitor can verify the daemon is
   alive.  ``os.replace`` is used for an atomic-ish overwrite: on POSIX
   this is guaranteed atomic; on Windows it is NOT guaranteed atomic —
   a partial or absent file after a crash is possible.  External monitors
   must treat a missing or partial heartbeat file as *stale*, not as an
   error.

2. **Stall detection** — ``_heartbeat_tick`` tracks the per-issue
   ``agent-in-progress`` state via ``LivenessState`` and fires a
   ``severity="critical"`` alert when an issue has been in-progress
   longer than ``obs.heartbeat_stall_s`` seconds (strictly greater than).
   The alert is debounced: it fires **once per episode** and resets only
   when ``LivenessState.clear()`` or ``LivenessState.mark_in_progress()``
   is called.

3. **Thread loop** — ``run_heartbeat_loop`` is the daemon OS-thread
   target.  It calls ``_heartbeat_tick`` on each iteration, then blocks
   on ``stop_event.wait(interval_s)`` (a ``threading.Event`` — truly
   interruptible, unlike ``asyncio.sleep``).  This means the heartbeat
   beats independently even while the asyncio event loop is blocked
   inside the synchronous CI gate (``merge.py`` ``time.sleep``).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from baton_harness.chain.escalation import alert
from baton_harness.chain.obs_config import ObsConfig
from baton_harness.chain.runlog import RunLog

_log = logging.getLogger(__name__)

# Fixed heartbeat cadence — shorter than the daemon poll interval so
# liveness updates arrive well before any external stall threshold
# triggers.
_DEFAULT_HEARTBEAT_CADENCE_S: float = 30.0


# ---------------------------------------------------------------------------
# LivenessState
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class LivenessState:
    """Mutable record of the currently-in-progress issue.

    Shared between the daemon (writer, asyncio thread) and
    ``run_heartbeat_loop`` (reader, monitor OS thread).  Field assignments
    are atomic under the GIL and this is best-effort liveness, so no
    explicit lock is required.

    Attributes:
        in_progress_owner: GitHub owner of the in-progress issue, or
            ``None`` when idle.
        in_progress_repo: GitHub repo of the in-progress issue, or
            ``None`` when idle.
        in_progress_issue: Issue number currently in progress, or
            ``None`` when idle.
        in_progress_since: UTC-aware datetime when the issue was marked
            in progress, or ``None`` when idle.
    """

    in_progress_owner: str | None = None
    in_progress_repo: str | None = None
    in_progress_issue: int | None = None
    in_progress_since: datetime | None = None
    _stall_alerted: bool = dataclasses.field(default=False, repr=False)

    def mark_in_progress(
        self,
        owner: str,
        repo: str,
        issue: int,
        now: datetime,
    ) -> None:
        """Record that ``issue`` is now in progress.

        Resets the stall-debounce flag so a new stall episode can fire
        an alert.

        Args:
            owner: GitHub repository owner.
            repo: GitHub repository name.
            issue: Issue number now in progress.
            now: UTC-aware datetime at which in-progress began.
        """
        self.in_progress_owner = owner
        self.in_progress_repo = repo
        self.in_progress_issue = issue
        self.in_progress_since = now
        self._stall_alerted = False

    def clear(self) -> None:
        """Clear the in-progress state.

        Nulls all four public fields and resets the stall-debounce flag.
        After ``clear()``, a subsequent ``mark_in_progress`` starts a
        fresh episode that can fire a stall alert.
        """
        self.in_progress_owner = None
        self.in_progress_repo = None
        self.in_progress_issue = None
        self.in_progress_since = None
        self._stall_alerted = False


# ---------------------------------------------------------------------------
# Filesystem write seam
# ---------------------------------------------------------------------------


def _write_heartbeat(path: Path, timestamp: str) -> None:
    """Write *timestamp* to *path* via a temp-file-then-replace strategy.

    This is the **sole filesystem write surface** for liveness signals.
    Tests can patch this single symbol to intercept all heartbeat writes.

    The write strategy is: create a sibling temp file in the same
    directory, write the timestamp, then call ``os.replace`` to
    overwrite *path*.  On POSIX, ``os.replace`` is atomic (rename
    syscall).  On Windows it is NOT guaranteed atomic — a crash between
    the write and the replace may leave an absent or partial file.
    External dead-man's-switch monitors must treat such files as stale,
    not as errors.

    Args:
        path: Target path for the heartbeat file.
        timestamp: ISO-8601 UTC timestamp string to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".heartbeat-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(timestamp)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any error (including KeyboardInterrupt).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Per-tick work (deterministic, no sleeping, fully guarded)
# ---------------------------------------------------------------------------


def _heartbeat_tick(
    obs: ObsConfig,
    state: LivenessState,
    *,
    runlog: RunLog | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
) -> None:
    """Execute one heartbeat iteration — deterministic, no sleeping.

    Fully guarded: this function never raises.  Each step is wrapped
    independently so a failure in one step does not prevent later steps
    from executing.

    Per-call semantics (in order):

    1. Capture ``t = now()``.
    2. Call ``_write_heartbeat(obs.heartbeat_file, t.isoformat())`` —
       the liveness signal.  Exceptions are logged and swallowed.
    3. Emit a ``{"event": "heartbeat", ...}`` runlog event (best-effort,
       if *runlog* is provided).
    4. Check for a stall condition:
       - ``state.in_progress_since`` is set,
       - ``(t - state.in_progress_since).total_seconds()``
         is **strictly greater than** ``obs.heartbeat_stall_s``, and
       - not already debounced (``state._stall_alerted`` is ``False``).
       If all three hold: call ``alert(...)`` with
       ``severity="critical"`` and ``kind="debug"``, emit a
       ``{"event": "stall", ...}`` runlog event, and set the debounce
       flag.

    Args:
        obs: Observability configuration (heartbeat_file, stall_s).
        state: Shared liveness state written by the daemon.
        runlog: Optional run-log handle for best-effort event emission.
        now: UTC ``datetime`` factory (injectable for tests).
    """
    # ---- Step 1 & 2: capture time, write liveness signal (guarded). ----
    try:
        t = now()
    except Exception as exc:  # noqa: BLE001
        _log.warning("_heartbeat_tick: now() failed: %s", exc)
        return

    try:
        _write_heartbeat(obs.heartbeat_file, t.isoformat())
    except Exception as exc:  # noqa: BLE001
        _log.warning("_heartbeat_tick: _write_heartbeat failed: %s", exc)

    # ---- Step 3: runlog heartbeat event (best-effort). -----------------
    if runlog is not None:
        try:
            runlog.emit(
                {
                    "ts": t.isoformat(),
                    "event": "heartbeat",
                    "issue": state.in_progress_issue,
                    "outcome": None,
                    "severity": "info",
                    "detail": "heartbeat",
                    "tick_id": None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "_heartbeat_tick: runlog.emit(heartbeat) failed: %s", exc
            )

    # ---- Step 4: stall detection (debounced). --------------------------
    if state.in_progress_since is not None and not state._stall_alerted:
        try:
            elapsed = (t - state.in_progress_since).total_seconds()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "_heartbeat_tick: elapsed calculation failed: %s", exc
            )
            return

        if elapsed > obs.heartbeat_stall_s:
            delivered = False
            try:
                delivered = alert(
                    state.in_progress_owner or "",
                    state.in_progress_repo or "",
                    state.in_progress_issue,
                    (
                        f"Issue #{state.in_progress_issue} has been"
                        f" agent-in-progress for"
                        f" {elapsed:.0f}s (threshold:"
                        f" {obs.heartbeat_stall_s:.0f}s) —"
                        " possible stall."
                    ),
                    severity="critical",
                    kind="debug",
                    runlog=runlog,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("_heartbeat_tick: alert() failed: %s", exc)

            if delivered:
                state._stall_alerted = True

                if runlog is not None:
                    try:
                        runlog.emit(
                            {
                                "ts": t.isoformat(),
                                "event": "stall",
                                "issue": state.in_progress_issue,
                                "outcome": None,
                                "severity": "critical",
                                "detail": (
                                    f"stall detected after {elapsed:.0f}s"
                                ),
                                "tick_id": None,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "_heartbeat_tick: runlog.emit(stall) failed: %s",
                            exc,
                        )


# ---------------------------------------------------------------------------
# Thread target: interruptible heartbeat loop
# ---------------------------------------------------------------------------


def run_heartbeat_loop(
    obs: ObsConfig,
    state: LivenessState,
    stop_event: threading.Event,
    *,
    runlog: RunLog | None = None,
    interval_s: float = _DEFAULT_HEARTBEAT_CADENCE_S,
    now: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
) -> None:
    """Daemon OS-thread target: tick then interruptible-sleep loop.

    Designed to run as a ``daemon=True`` ``threading.Thread``.  Because
    it uses ``threading.Event.wait`` (not ``asyncio.sleep``) for its
    inter-tick sleep, it beats independently even while the asyncio event
    loop is blocked inside a synchronous call such as ``time.sleep`` in
    ``merge.py``.

    Loop semantics:

    1. Call ``_heartbeat_tick(obs, state, runlog=runlog, now=now)``.
    2. Call ``stop_event.wait(interval_s)``.  This blocks for up to
       *interval_s* seconds but returns immediately (``True``) when
       ``stop_event`` is set by the caller.  On return value ``True``,
       the loop exits cleanly.

    The entire loop body is guarded: any unexpected exception is logged
    and the loop continues.  The ``stop_event.wait`` path is the sole
    clean-exit mechanism.

    Args:
        obs: Observability configuration (heartbeat_file, stall_s).
        state: Shared liveness state written by the daemon.
        stop_event: ``threading.Event`` — set by the daemon's
            ``finally`` block to signal the thread to exit.
        runlog: Optional run-log handle for best-effort event emission.
        interval_s: Seconds between heartbeat ticks.  Defaults to
            ``_DEFAULT_HEARTBEAT_CADENCE_S`` (30 s).
        now: UTC ``datetime`` factory (injectable for tests).
    """
    while True:
        try:
            _heartbeat_tick(obs, state, runlog=runlog, now=now)
        except Exception as exc:  # noqa: BLE001
            # _heartbeat_tick is itself guarded and should never raise,
            # but add a belt-and-suspenders catch here so the thread
            # survives any unexpected exception.
            _log.warning(
                "run_heartbeat_loop: unexpected exception from tick: %s",
                exc,
            )

        # Interruptible sleep: returns True immediately when stop_event
        # is set, so the thread exits promptly on shutdown.
        if stop_event.wait(interval_s):
            return
