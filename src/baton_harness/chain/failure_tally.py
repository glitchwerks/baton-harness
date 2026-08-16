"""Durable per-issue failure counts for the baton-harness daemon."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)


class FailureTally:
    """Track durable per-issue failure counts.

    Counts are persisted after every mutation. Load and persistence
    failures are tolerated so tally bookkeeping never interrupts daemon
    operation.

    Attributes:
        path: Path to the backing JSON file.
        max_count: Count at which an issue is exhausted.
    """

    def __init__(self, path: str | Path, max_count: int) -> None:
        """Initialize the tally and load persisted counts.

        Args:
            path: Path to the backing JSON file.
            max_count: Count at which an issue is exhausted.
        """
        self.path = Path(path)
        self.max_count = max_count
        self._issues: dict[int, int] = {}
        self._load()

    def record_and_check(self, issue: int) -> tuple[int, bool]:
        """Increment an issue's count and report whether it is exhausted.

        Args:
            issue: GitHub issue number to record.

        Returns:
            The new count and whether it is at least ``max_count``.
        """
        count = self._issues.get(issue, 0) + 1
        self._issues[issue] = count
        self._persist()
        return count, count >= self.max_count

    def reset(self, issue: int) -> None:
        """Remove an issue's failure count and persist the deletion.

        Args:
            issue: GitHub issue number to reset.
        """
        self._issues.pop(issue, None)
        self._persist()

    def peek(self, issue: int) -> int:
        """Return an issue's current count without changing state.

        Args:
            issue: GitHub issue number to inspect.

        Returns:
            The current count, or zero when the issue is unknown.
        """
        return self._issues.get(issue, 0)

    def _load(self) -> None:
        """Load counts from disk, treating every failure as empty state."""
        try:
            raw = self.path.read_text(encoding="utf-8")
            data: object = json.loads(raw)
            if not isinstance(data, dict):
                return
            issues = data.get("issues", {})
            if not isinstance(issues, dict):
                return
            for key, count in issues.items():
                if isinstance(count, int):
                    self._issues[int(key)] = count
        except Exception:  # noqa: BLE001
            self._issues = {}

    def _persist(self) -> None:
        """Persist counts atomically, swallowing every write failure."""
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        replaced = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "issues": {
                    str(issue): count for issue, count in self._issues.items()
                }
            }
            with open(
                tmp_path,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as file_handle:
                json.dump(data, file_handle)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(tmp_path, self.path)
            replaced = True
        except Exception:  # noqa: BLE001
            _log.debug(
                "failure tally persist failed for %s; continuing",
                self.path,
            )
            if not replaced:
                try:
                    os.unlink(tmp_path)
                except Exception:  # noqa: BLE001
                    pass
