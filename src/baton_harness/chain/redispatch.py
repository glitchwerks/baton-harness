"""Durable re-dispatch-loop detection for the baton-harness daemon.

Provides two public symbols:

``exceeds_threshold``
    A pure, stateless helper that decides whether a list of tick-mark
    timestamps contains enough marks inside a sliding window to declare a
    re-dispatch loop.

``RedispatchTally``
    A durable, file-backed tally that records per-issue re-dispatch events
    and survives daemon restarts.  State is persisted to a JSON file so
    that a crash-restart cycle -- the exact failure mode this module
    targets -- accumulates marks rather than resetting them.

Environment variables
---------------------
None -- configuration is supplied by the caller via ``__init__`` arguments
(``window_ticks``, ``max_count``) which are read from ``ObsConfig`` by
the daemon.

JSON file schema
----------------
.. code-block:: json

    {
        "tick": <int>,
        "issues": {
            "<issue-number-as-string>": [<tick-mark>, ...]
        }
    }

Issue keys are **strings** in the JSON representation (JSON requires string
object keys) even though issue numbers are ``int`` in Python.  The tally
normalises on load and on write.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def exceeds_threshold(
    tick_marks: list[int],
    current_tick: int,
    window_ticks: int,
    max_count: int,
) -> bool:
    """Return True when more than max_count marks fall in the window.

    The window is the half-open interval
    ``(current_tick - window_ticks, current_tick]`` -- the lower bound is
    **excluded**, the upper bound is **included**.

    Semantics: ``BH_REDISPATCH_MAX = N`` permits N redispatches and
    breaches on the (N+1)th attempt.  ``record_and_check`` is called
    before each dispatch, so:

    * ``count == max_count`` -> ``False``  (the Nth redispatch is allowed)
    * ``count >  max_count`` -> ``True``   (the configured maximum has been
      exceeded; park the issue)

    This function is pure: it does not mutate ``tick_marks`` or perform
    any I/O.

    Args:
        tick_marks: List of integer tick values previously recorded for an
            issue.  The list is not mutated.
        current_tick: The current global tick counter value.
        window_ticks: Width of the sliding window in ticks.
        max_count: The maximum number of redispatches allowed inside the
            window.  Returns ``True`` only when the in-window count
            *exceeds* this value (i.e. ``count > max_count``).

    Returns:
        ``True`` if the number of marks in
        ``(current_tick - window_ticks, current_tick]`` is
        ``> max_count`` (i.e. the configured maximum has been exceeded),
        ``False`` otherwise.
    """
    lower = current_tick - window_ticks  # exclusive
    count = sum(1 for m in tick_marks if lower < m <= current_tick)
    return count > max_count


# ---------------------------------------------------------------------------
# Durable tally
# ---------------------------------------------------------------------------


class RedispatchTally:
    """Durable per-issue re-dispatch tally backed by a JSON file.

    Tracks a global tick counter and per-issue lists of tick marks.  State
    is persisted to disk after every mutation so that a daemon restart
    inherits the accumulated history -- enabling detection of crash-restart
    loops that an in-process counter would miss.

    Write failures are best-effort: an ``OSError`` during ``_persist`` is
    logged and swallowed.  The in-memory state remains intact, so the tally
    continues to function for the lifetime of the current process.

    Attributes:
        path: Resolved ``Path`` to the backing JSON file.
        window_ticks: Width of the sliding window in ticks.
        max_count: Threshold for ``exceeds_threshold``.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        window_ticks: int,
        max_count: int,
    ) -> None:
        """Initialise and load persisted state from disk.

        Missing or corrupt/unreadable files are silently treated as empty
        (tick=0, no marks) -- this function NEVER raises.

        Args:
            path: Path (``Path`` or ``str``) to the backing JSON file.
            window_ticks: Width of the sliding window passed to
                ``exceeds_threshold``.
            max_count: Threshold passed to ``exceeds_threshold``.
        """
        self.path: Path = Path(path)
        self.window_ticks: int = window_ticks
        self.max_count: int = max_count

        # In-memory state -- populated from disk below.
        self._tick: int = 0
        self._issues: dict[int, list[int]] = {}

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def advance_tick(self) -> int:
        """Increment the global tick counter, persist, return new value.

        Returns:
            The new tick value after incrementing.
        """
        self._tick += 1
        self._persist()
        return self._tick

    def record_and_check(self, issue: int) -> bool:
        """Record a re-dispatch event for ``issue`` and check the threshold.

        Appends the current tick to the issue's mark list, prunes marks
        outside the window ``(current_tick - window_ticks, current_tick]``,
        persists the updated state, and returns whether the threshold is
        breached.

        Args:
            issue: The GitHub issue number being re-dispatched.

        Returns:
            ``True`` if ``exceeds_threshold`` returns ``True`` for the
            issue's marks after recording and pruning; ``False`` otherwise.
        """
        marks = self._issues.get(issue, [])
        marks.append(self._tick)
        # Prune marks outside the window.
        lower = self._tick - self.window_ticks
        marks = [m for m in marks if m > lower]
        self._issues[issue] = marks
        self._persist()
        return exceeds_threshold(
            marks,
            current_tick=self._tick,
            window_ticks=self.window_ticks,
            max_count=self.max_count,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load state from the backing file, tolerating all errors.

        Missing file -> empty state (no raise).
        Corrupt / unreadable file -> empty state (no raise).
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
            data: object = json.loads(raw)
            if not isinstance(data, dict):
                return
            tick = data.get("tick", 0)
            if isinstance(tick, int):
                self._tick = tick
            issues_raw = data.get("issues", {})
            if isinstance(issues_raw, dict):
                for k, v in issues_raw.items():
                    if isinstance(v, list):
                        try:
                            self._issues[int(k)] = [
                                m for m in v if isinstance(m, int)
                            ]
                        except (ValueError, TypeError):
                            pass
        except Exception:  # noqa: BLE001
            # Missing file, permission error, corrupt JSON -- treat as empty.
            self._tick = 0
            self._issues = {}

    def _persist(self) -> None:
        """Write current state to the backing file atomically (best-effort).

        Creates the parent directory if required.  Writes JSON to a
        temporary sibling file in the same directory, fsyncs it, then
        atomically replaces the real file via ``os.replace``.  This
        ensures the original file is never truncated if the write fails
        mid-way (e.g. disk full, process killed).

        Any exception -- including OS errors, disk-full conditions, or
        failures in the temp-write step -- is swallowed so that write
        failures are never surfaced to the caller.  If the replace step
        does not complete, a best-effort cleanup of the temp file is
        attempted; a failure during cleanup is also swallowed.
        """
        # Deterministic temp sibling: starts with path.name so the
        # test's ``startswith(path.name)`` patch guard intercepts it,
        # and lives in the same directory for an atomic os.replace.
        tmp_path: Path = self.path.with_name(self.path.name + ".tmp")
        replaced: bool = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "tick": self._tick,
                "issues": {str(k): v for k, v in self._issues.items()},
            }
            # Open via builtins.open so the test-injected patch can
            # intercept the write and simulate a mid-write failure without
            # the real file ever being opened for truncation.
            with open(  # noqa: WPS515
                tmp_path, "w", encoding="utf-8", newline="\n"
            ) as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
            replaced = True
        except Exception:  # noqa: BLE001
            _log.debug(
                "redispatch: persist failed for %s (best-effort; continuing)",
                self.path,
            )
            if not replaced:
                try:
                    os.unlink(tmp_path)
                except Exception:  # noqa: BLE001
                    pass
