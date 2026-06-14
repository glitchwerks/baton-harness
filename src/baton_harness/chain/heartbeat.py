"""Decoupled heartbeat coroutine and in-daemon stall detection.

This module provides two concerns that are deliberately kept separate:

1. **Liveness** — ``_write_heartbeat`` writes a timestamp file each tick
   so an external dead-man's-switch monitor can verify the daemon is
   alive.  ``os.replace`` is used for an atomic-ish overwrite: on POSIX
   this is guaranteed atomic; on Windows it is NOT guaranteed atomic —
   a partial or absent file after a crash is possible.  External monitors
   must treat a missing or partial heartbeat file as *stale*, not as an
   error.

2. **Stall detection** — ``heartbeat_monitor`` tracks the per-issue
   ``agent-in-progress`` state via ``LivenessState`` and fires a
   ``severity="critical"`` alert when an issue has been in-progress
   longer than ``obs.heartbeat_stall_s`` seconds.  The alert is
   debounced: it fires **once per episode** and resets only when
   ``LivenessState.clear()`` or ``LivenessState.mark_in_progress()`` is
   called.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baton_harness.chain.escalation import alert
from baton_harness.chain.obs_config import ObsConfig
from baton_harness.chain.runlog import RunLog

_log = logging.getLogger(__name__)

# Fixed heartbeat cadence — shorter than the daemon poll interval so liveness
# updates arrive well before any external stall threshold triggers.
_DEFAULT_HEARTBEAT_CADENCE_S: float = 30.0


# ---------------------------------------------------------------------------
# LivenessState
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class LivenessState:
    """Mutable record of the currently-in-progress issue.

    Shared between the daemon (writer) and ``heartbeat_monitor`` (reader).
    All mutations are synchronous and executed on the single asyncio
    thread — no locking needed.

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
    _stall_alerted: bool = dataclasses.field(
        default=False, repr=False
    )

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
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".heartbeat-tmp-"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(timestamp)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any error (including CancelledError).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Heartbeat monitor coroutine
# ---------------------------------------------------------------------------


async def heartbeat_monitor(  # noqa: C901
    obs: ObsConfig,
    state: LivenessState,
    *,
    runlog: RunLog | None = None,
    interval_s: float = _DEFAULT_HEARTBEAT_CADENCE_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the background heartbeat and stall-detection loop.

    Designed to be launched as a single background ``asyncio.Task``
    (the ONLY long-lived task in the daemon — does NOT violate the
    B-I3 serial contract because this task touches no repo HEAD).

    Per-iteration semantics (in order):

    1. Capture ``t = now()``.
    2. Call ``_write_heartbeat(obs.heartbeat_file, t.isoformat())`` —
       the liveness signal.  Failures are logged and swallowed.
    3. Emit a ``{"event": "heartbeat", ...}`` runlog event (best-effort,
       if *runlog* is provided).
    4. Check for a stall condition:
       - ``state.in_progress_since`` is set,
       - ``(t - state.in_progress_since).total_seconds()``
         is strictly greater than ``obs.heartbeat_stall_s``, and
       - not already debounced (``state._stall_alerted`` is ``False``).
       If all three hold: call ``alert(...)`` with
       ``severity="critical"``, emit a ``{"event": "stall", ...}``
       runlog event, and set the debounce flag.
    5. ``await sleep(interval_s)`` — yields control to the event loop.
    6. If ``stop_event`` is set, break and return.

    The coroutine is **fully guarded**: any exception from
    ``_write_heartbeat``, runlog emission, or ``alert`` (except
    ``asyncio.CancelledError``) is logged at WARNING and swallowed —
    the loop always continues.  ``asyncio.CancelledError`` from
    ``sleep`` is caught and the coroutine returns cleanly without
    propagating.

    Args:
        obs: Observability configuration (heartbeat_file, heartbeat_stall_s).
        state: Shared liveness state mutated by the daemon.
        runlog: Optional run-log handle for best-effort event emission.
        interval_s: Seconds between heartbeat writes.  Defaults to
            ``_DEFAULT_HEARTBEAT_CADENCE_S`` (30 s).
        sleep: Async sleep callable (injectable for tests).
        now: UTC ``datetime`` factory (injectable for tests).
        stop_event: Optional event; when set, the loop exits cleanly
            after the current iteration completes.  ``None`` means
            "run until cancelled".
    """
    while True:
        try:
            # ---- Step 1 & 2: liveness write (fully guarded). ----------------
            t = now()
            try:
                _write_heartbeat(obs.heartbeat_file, t.isoformat())
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "heartbeat_monitor: _write_heartbeat failed: %s", exc
                )

            # ---- Step 3: runlog heartbeat event (best-effort). --------------
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
                        "heartbeat_monitor: runlog.emit(heartbeat)"
                        " failed: %s",
                        exc,
                    )

            # ---- Step 4: stall detection (debounced). -----------------------
            if (
                state.in_progress_since is not None
                and not state._stall_alerted
            ):
                elapsed = (t - state.in_progress_since).total_seconds()
                if elapsed > obs.heartbeat_stall_s:
                    try:
                        alert(
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
                        _log.warning(
                            "heartbeat_monitor: alert() failed: %s", exc
                        )
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
                                "heartbeat_monitor: runlog.emit(stall)"
                                " failed: %s",
                                exc,
                            )

        except Exception as exc:  # noqa: BLE001
            # Outer guard: any unexpected exception from the per-iteration
            # work (including now(), stop_event interactions, or stall
            # logic) is logged and swallowed so the loop survives.
            # asyncio.CancelledError is a BaseException, not Exception, so
            # it will NOT be caught here — cancellation still propagates
            # cleanly from the sleep handler below.
            _log.warning(
                "heartbeat_monitor: unhandled per-iteration exception: %s",
                exc,
            )

        # ---- Step 5: sleep (yields to event loop). ----------------------
        try:
            await sleep(interval_s)
        except asyncio.CancelledError:
            return

        # ---- Step 6: stop-event check. ----------------------------------
        if stop_event is not None and stop_event.is_set():
            return
