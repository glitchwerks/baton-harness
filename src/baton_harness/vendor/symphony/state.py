"""symphony/state.py — In-memory orchestrator state with JSON persistence."""

from __future__ import annotations

import json
import logging  # VENDOR-PATCH VP-6: needed for load() corruption warning
import os
import tempfile  # VENDOR-PATCH VP-6: needed for atomic persist() via tempfile
import time
from dataclasses import dataclass, field

log = logging.getLogger("symphony")  # VENDOR-PATCH VP-6: module-level logger


@dataclass
class IssueState:
    """In-memory snapshot of a single issue's orchestration progress.

    Attributes:
        issue_number: The GitHub issue number being worked.
        identifier: Human-readable identifier for the issue (usually the
            issue number as a string).
        title: The issue's title, as fetched from GitHub.
        state: Current orchestration state label (e.g. ``"running"``).
        turn: The current turn count within the agent's run.
        max_turns: The maximum number of turns allowed for this issue.
        started_at: Unix timestamp when the issue's run started.
        last_event: Description of the most recent orchestration event,
            if any.
        last_event_at: Unix timestamp of the most recent event, if any.
        error: Error message from the most recent failure, if any.
    """

    issue_number: int
    identifier: str
    title: str
    state: str
    turn: int
    max_turns: int
    started_at: float = field(default_factory=time.time)
    last_event: str | None = None
    last_event_at: float | None = None
    error: str | None = None


@dataclass
class RetryEntry:
    """A pending retry attempt queued after a transient run failure.

    Attributes:
        issue_number: The GitHub issue number to retry.
        identifier: Human-readable identifier for the issue.
        attempt: The retry attempt number (1-based).
        due_at: Unix timestamp after which the retry becomes eligible to
            run.
        error: The error message from the failure that triggered this
            retry, if any.
    """

    issue_number: int
    identifier: str
    attempt: int
    due_at: float
    error: str | None = None


class OrchestratorState:
    """Tracks claimed, running, retrying, and completed issues in memory.

    Attributes:
        max_concurrent: The maximum number of issues that may run
            concurrently.
        running: Mapping of issue number to its in-flight ``IssueState``.
        claimed: Set of issue numbers currently claimed by this
            orchestrator (running or awaiting retry).
        retry_queue: Mapping of issue number to its pending
            ``RetryEntry``.
        completed: Set of issue numbers that have finished successfully.
    """

    def __init__(self, max_concurrent: int = 3) -> None:
        """Initialize empty state with the given concurrency limit.

        Args:
            max_concurrent: The maximum number of issues that may run
                concurrently.
        """
        self.max_concurrent = max_concurrent
        self.running: dict[int, IssueState] = {}
        self.claimed: set[int] = set()
        self.retry_queue: dict[int, RetryEntry] = {}
        self.completed: set[int] = set()

    @property
    def running_count(self) -> int:
        """int: The number of issues currently running."""
        return len(self.running)

    @property
    def available_slots(self) -> int:
        """int: The number of additional issues that may start running."""
        return max(self.max_concurrent - self.running_count, 0)

    def is_claimed(self, issue_number: int) -> bool:
        """Check whether *issue_number* is currently claimed.

        Args:
            issue_number: The GitHub issue number to check.

        Returns:
            True if the issue is claimed (running or retry-queued).
        """
        return issue_number in self.claimed

    def claim(self, issue_number: int) -> None:
        """Mark *issue_number* as claimed by this orchestrator.

        Args:
            issue_number: The GitHub issue number to claim.
        """
        self.claimed.add(issue_number)

    def release(self, issue_number: int) -> None:
        """Release *issue_number*, clearing its claim/running/retry state.

        Args:
            issue_number: The GitHub issue number to release.
        """
        self.claimed.discard(issue_number)
        self.running.pop(issue_number, None)
        self.retry_queue.pop(issue_number, None)

    def add_running(self, issue_number: int, state: IssueState) -> None:
        """Record *issue_number* as running with the given state.

        Args:
            issue_number: The GitHub issue number now running.
            state: The ``IssueState`` snapshot to record.
        """
        self.running[issue_number] = state
        self.claimed.add(issue_number)
        self.retry_queue.pop(issue_number, None)

    def remove_running(self, issue_number: int) -> IssueState | None:
        """Remove and return the running state for *issue_number*, if any.

        Args:
            issue_number: The GitHub issue number to remove.

        Returns:
            The removed ``IssueState``, or None if the issue was not
            running.
        """
        return self.running.pop(issue_number, None)

    def schedule_retry(
        self,
        issue_number: int,
        attempt: int,
        error: str | None = None,
        delay_ms: int = 1000,
    ) -> None:
        """Queue a retry for *issue_number* after *delay_ms* milliseconds.

        Args:
            issue_number: The GitHub issue number to retry.
            attempt: The retry attempt number (1-based).
            error: The error message from the failure that triggered
                this retry, if any.
            delay_ms: Delay, in milliseconds, before the retry becomes
                due.
        """
        self.retry_queue[issue_number] = RetryEntry(
            issue_number=issue_number,
            identifier=str(issue_number),
            attempt=attempt,
            due_at=time.time() + delay_ms / 1000,
            error=error,
        )

    def due_retries(self) -> list[RetryEntry]:
        """Return all retry entries whose due time has passed.

        Returns:
            The list of ``RetryEntry`` objects currently eligible to run.
        """
        now = time.time()
        return [r for r in self.retry_queue.values() if r.due_at <= now]

    def persist(self, path: str) -> None:
        """Atomically write state to *path* via a sibling tempfile.

        Writing to a temporary file first ensures that a crash or exception
        mid-write never leaves a partial/corrupt file at *path*.  The original
        file content is preserved on failure.

        Args:
            path: Destination file path for the serialised state JSON.

        Raises:
            OSError: If the directory cannot be created, the temp file cannot
                be written, or ``os.replace`` fails.  Any partially-written
                temp file is cleaned up before re-raising.
        """  # VENDOR-PATCH VP-6: method fully replaced for atomic write
        data = {
            "running": [
                {
                    "issue_number": s.issue_number,
                    "identifier": s.identifier,
                    "title": s.title,
                    "state": s.state,
                    "turn": s.turn,
                    "max_turns": s.max_turns,
                    "started_at": s.started_at,
                    "last_event": s.last_event,
                    "error": s.error,
                }
                for s in self.running.values()
            ],
            "retrying": [
                {
                    "issue_number": r.issue_number,
                    "identifier": r.identifier,
                    "attempt": r.attempt,
                    "due_at": r.due_at,
                    "error": r.error,
                }
                for r in self.retry_queue.values()
            ],
            "claimed": list(self.claimed),
            "completed_count": len(self.completed),
        }
        dir_path = os.path.dirname(path)  # VENDOR-PATCH VP-6: atomic write
        os.makedirs(dir_path, exist_ok=True)  # VENDOR-PATCH VP-6: atomic write
        fd, tmp = tempfile.mkstemp(  # VENDOR-PATCH VP-6: atomic write
            dir=dir_path, prefix=".state.", suffix=".tmp"
        )
        try:  # VENDOR-PATCH VP-6: atomic write
            with os.fdopen(fd, "w") as f:  # VENDOR-PATCH VP-6: atomic write
                json.dump(data, f, indent=2)  # VENDOR-PATCH VP-6: atomic write
            os.replace(tmp, path)  # VENDOR-PATCH VP-6: atomic rename
        except Exception:  # VENDOR-PATCH VP-6: atomic write cleanup on failure
            try:  # VENDOR-PATCH VP-6: atomic write cleanup on failure
                os.unlink(tmp)  # VENDOR-PATCH VP-6: atomic write cleanup
            except OSError:  # VENDOR-PATCH VP-6: atomic write cleanup
                pass  # VENDOR-PATCH VP-6: atomic write cleanup
            raise  # VENDOR-PATCH VP-6: atomic write cleanup on failure

    def load(self, path: str) -> None:
        """Restore running, retry_queue, and claimed from a state file.

        Missing file is a no-op (first-ever startup).  Malformed JSON logs a
        WARNING and leaves state empty (safe fresh-start rather than crash).
        ``last_event_at`` is always ``None`` after load because the field is
        not included in the persisted JSON format.

        Args:
            path: Path to a ``state.json`` previously written by
                :meth:`persist`.
        """  # VENDOR-PATCH VP-6: new method — load state from disk
        try:  # VENDOR-PATCH VP-6: load()
            with open(path, encoding="utf-8") as f:  # VENDOR-PATCH VP-6
                data = json.load(f)  # VENDOR-PATCH VP-6: load()
        except FileNotFoundError:  # VENDOR-PATCH VP-6: missing file → no-op
            return  # VENDOR-PATCH VP-6: missing file → no-op
        except (json.JSONDecodeError, OSError) as exc:  # VENDOR-PATCH VP-6
            log.warning(  # VENDOR-PATCH VP-6: corruption fallback
                "state.json is malformed or unreadable — starting fresh: %s",
                exc,
            )
            # VENDOR-PATCH VP-6: corruption fallback — state stays empty
            return

        for entry in data.get("running", []):  # VENDOR-PATCH VP-6: load()
            issue_st = IssueState(  # VENDOR-PATCH VP-6: rebuild IssueState
                issue_number=entry["issue_number"],
                identifier=entry["identifier"],
                title=entry["title"],
                state=entry["state"],
                turn=entry["turn"],
                max_turns=entry["max_turns"],
                started_at=entry["started_at"],
                last_event=entry.get("last_event"),
                # last_event_at intentionally omitted from persist() output;
                # defaults to None per IssueState field default.  # VP-6
                error=entry.get("error"),
            )
            self.running[issue_st.issue_number] = issue_st  # VENDOR-PATCH VP-6

        for entry in data.get("retrying", []):  # VENDOR-PATCH VP-6: load()
            retry = RetryEntry(  # VENDOR-PATCH VP-6: rebuild RetryEntry
                issue_number=entry["issue_number"],
                identifier=entry["identifier"],
                attempt=entry["attempt"],
                due_at=entry["due_at"],
                error=entry.get("error"),
            )
            self.retry_queue[retry.issue_number] = retry  # VENDOR-PATCH VP-6

        for num in data.get("claimed", []):  # VENDOR-PATCH VP-6: load claimed
            self.claimed.add(int(num))  # VENDOR-PATCH VP-6: load claimed
