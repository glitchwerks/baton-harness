"""symphony/state.py — In-memory orchestrator state with JSON persistence."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field


@dataclass
class IssueState:
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
    issue_number: int
    identifier: str
    attempt: int
    due_at: float
    error: str | None = None


class OrchestratorState:
    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.running: dict[int, IssueState] = {}
        self.claimed: set[int] = set()
        self.retry_queue: dict[int, RetryEntry] = {}
        self.completed: set[int] = set()

    @property
    def running_count(self) -> int:
        return len(self.running)

    @property
    def available_slots(self) -> int:
        return max(self.max_concurrent - self.running_count, 0)

    def is_claimed(self, issue_number: int) -> bool:
        return issue_number in self.claimed

    def claim(self, issue_number: int) -> None:
        self.claimed.add(issue_number)

    def release(self, issue_number: int) -> None:
        self.claimed.discard(issue_number)
        self.running.pop(issue_number, None)
        self.retry_queue.pop(issue_number, None)

    def add_running(self, issue_number: int, state: IssueState) -> None:
        self.running[issue_number] = state
        self.claimed.add(issue_number)
        self.retry_queue.pop(issue_number, None)

    def remove_running(self, issue_number: int) -> IssueState | None:
        return self.running.pop(issue_number, None)

    def schedule_retry(
        self,
        issue_number: int,
        attempt: int,
        error: str | None = None,
        delay_ms: int = 1000,
    ) -> None:
        self.retry_queue[issue_number] = RetryEntry(
            issue_number=issue_number,
            identifier=str(issue_number),
            attempt=attempt,
            due_at=time.time() + delay_ms / 1000,
            error=error,
        )

    def due_retries(self) -> list[RetryEntry]:
        now = time.time()
        return [r for r in self.retry_queue.values() if r.due_at <= now]

    def persist(self, path: str) -> None:
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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
