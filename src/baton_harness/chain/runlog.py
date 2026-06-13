"""JSONL run-record substrate for the baton-harness daemon.

Provides a best-effort structured log of daemon lifecycle events.  All
filesystem I/O is funnelled through the module-level ``_write_line`` seam
so that tests can patch a single symbol (mirrors the ``escalation._run``
seam pattern at ``src/baton_harness/chain/escalation.py:31``).

Event schema keys
-----------------
All events are plain ``dict[str, Any]`` objects.  Callers are expected
to populate the keys relevant to each event type:

ts : str
    ISO-8601 UTC timestamp string, e.g.
    ``"2026-06-13T12:00:00.000000+00:00"``.
event : str
    Event discriminator, e.g. ``"daemon_start"``, ``"dispatch"``,
    ``"outcome"``.
issue : int | None
    GitHub issue number associated with the event, or ``None`` for
    daemon-level events not tied to a specific issue.
outcome : str | None
    String outcome for ``"outcome"`` events (e.g. ``"pr_created"``,
    ``"no_pr"``), or ``None`` for other event types.
severity : str
    Severity level string (e.g. ``"info"``, ``"warning"``, ``"error"``).
detail : str
    Human-readable detail message.
tick_id : str | None
    Identifier for the poll tick that generated this event, or ``None``.

Implementation notes
--------------------
* ``RunLog.emit`` writes the caller-supplied dict **verbatim** — no keys
  are injected or dropped.  Callers are responsible for populating ``ts``
  and other schema keys.
* ``emit`` is fully guarded: it NEVER raises.  Any exception from
  ``_write_line`` is caught, logged at WARNING, and swallowed.
* ``RunLog.__init__`` attempts to create the log file's parent directory
  with ``mkdir(parents=True, exist_ok=True)`` but swallows failures so
  that construction itself NEVER raises.
* Windows ``os.replace`` non-atomicity caveat is NOT relevant here
  (plain append mode only); heartbeat-file logic is deferred to a later
  phase.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem I/O seam (sole write surface; patch this in tests)
# ---------------------------------------------------------------------------


def _write_line(path: Path, line: str) -> None:
    r"""Append *line* verbatim to *path* in UTF-8 append mode.

    This is the sole filesystem I/O surface for ``RunLog``.  Patching
    this single symbol in tests intercepts all writes without touching
    the real filesystem (mirrors the ``escalation._run`` seam pattern).

    Args:
        path: Absolute (or relative) path to the JSONL log file.
        line: A complete JSONL line including its trailing newline
            character.  Callers must supply the ``\n``.
    """
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


# ---------------------------------------------------------------------------
# RunLog
# ---------------------------------------------------------------------------


class RunLog:
    """Append-only JSONL log for daemon lifecycle events.

    Each ``emit`` call appends exactly one JSON object (one JSONL line)
    to the log file.  All I/O is best-effort — neither construction nor
    emission raises under any circumstances.

    Attributes:
        path: Resolved ``Path`` to the JSONL log file.
    """

    def __init__(self, path: str | Path) -> None:
        """Initialise a ``RunLog`` pointing at *path*.

        Attempts to create the parent directory tree via
        ``mkdir(parents=True, exist_ok=True)``.  If directory creation
        fails for any reason, the failure is logged at WARNING and
        suppressed; a subsequent ``emit`` that hits the missing directory
        will likewise be swallowed.

        Args:
            path: Path to the JSONL log file (the file itself need not
                exist yet; it is created on first write).
        """
        self.path: Path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "runlog: could not create parent directory %r: %s",
                str(self.path.parent),
                exc,
            )

    def emit(self, event: dict[str, Any]) -> None:
        """Append *event* as a single JSONL line to the log file.

        The dict is serialised verbatim via ``json.dumps`` — no keys are
        injected, mutated, or removed.  A trailing newline is appended
        automatically.

        If any exception occurs (I/O error, serialisation failure, etc.)
        it is caught, logged at WARNING, and swallowed.  This method
        NEVER raises.

        Args:
            event: The event dict to serialise.  Expected to follow the
                schema documented at the top of this module, but no
                validation is performed.
        """
        try:
            line = json.dumps(event) + "\n"
            _write_line(self.path, line)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "runlog: failed to emit event %r: %s",
                event.get("event"),
                exc,
            )
        return
