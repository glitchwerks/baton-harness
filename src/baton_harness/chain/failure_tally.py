"""Durable per-issue failure counts for the baton-harness daemon."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)


class FailureTally:
    """Track durable per-issue failure counts.

    Counts, and a per-issue "already alerted" flag (see ``set_alerted``
    / ``has_alerted``), are persisted after every mutation. Load and
    persistence failures are tolerated so tally bookkeeping never
    interrupts daemon operation.

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
        self._alerted: set[int] = set()
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

        Also clears the issue's "already alerted" flag (see
        ``set_alerted``) so a future failure streak on the same issue
        can raise a fresh alert.

        Args:
            issue: GitHub issue number to reset.
        """
        self._issues.pop(issue, None)
        self._alerted.discard(issue)
        self._persist()

    def peek(self, issue: int) -> int:
        """Return an issue's current count without changing state.

        Args:
            issue: GitHub issue number to inspect.

        Returns:
            The current count, or zero when the issue is unknown.
        """
        return self._issues.get(issue, 0)

    def set_alerted(self, issue: int) -> None:
        """Durably mark an issue as having already been alerted.

        Used by the (future, T4b) silently-excluded ``agent-failed``
        issue alert so that alert fires at most once per issue, even
        across a daemon restart.

        Args:
            issue: GitHub issue number to mark as alerted.
        """
        self._alerted.add(issue)
        self._persist()

    def has_alerted(self, issue: int) -> bool:
        """Return whether ``set_alerted`` has been called for an issue.

        Args:
            issue: GitHub issue number to inspect.

        Returns:
            ``True`` if ``set_alerted`` has recorded this issue and it
            has not since been ``reset``; ``False`` otherwise.
        """
        return issue in self._alerted

    def _load(self) -> None:
        """Load counts and alerted flags, treating any failure as empty."""
        try:
            raw = self.path.read_text(encoding="utf-8")
            data: object = json.loads(raw)
            if not isinstance(data, dict):
                return
            issues = data.get("issues", {})
            if isinstance(issues, dict):
                for key, count in issues.items():
                    if isinstance(count, int):
                        self._issues[int(key)] = count
            alerted = data.get("alerted", [])
            if isinstance(alerted, list):
                for key in alerted:
                    try:
                        self._alerted.add(int(key))
                    except (TypeError, ValueError):
                        continue
        except Exception:  # noqa: BLE001
            self._issues = {}
            self._alerted = set()

    def _persist(self) -> None:
        """Persist counts and alerted flags, swallowing write failures."""
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        replaced = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "issues": {
                    str(issue): count for issue, count in self._issues.items()
                },
                "alerted": [str(issue) for issue in sorted(self._alerted)],
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
            self._fsync_parent_dir()
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

    def _fsync_parent_dir(self) -> None:
        """Fsync the parent directory so the rename itself is durable.

        ``os.replace`` in ``_persist`` makes the new file's *contents*
        durable, but on POSIX the directory-entry update (the rename)
        is not itself guaranteed durable across a crash until the
        containing directory's file descriptor is fsynced too.  This
        is best-effort bookkeeping: platforms that cannot open a
        directory via ``os.open`` (e.g. Windows) and any OS-level
        failure are swallowed silently so a durability nicety never
        interrupts daemon operation.
        """
        if os.name != "posix":
            return
        dir_fd: int | None = None
        try:
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            os.fsync(dir_fd)
        except (OSError, NotImplementedError):
            pass
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
