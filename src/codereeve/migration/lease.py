"""Nonblocking lifetime writer leases; observations never prove shutdown."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

from codereeve.migration.model import EvidenceState
from codereeve.paths import PathConflictError, validate_safe_file_path
from codereeve.redact import redact_secrets


class LeaseError(RuntimeError):
    """A cooperative writer lease is held, unsafe, or unavailable."""


def _lock(fd: int, *, unlock: bool = False) -> None:
    """Lock one byte on Windows or the file on POSIX without waiting."""
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(
            fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB
        )


def _open_lock(path: Path, *, create: bool) -> int:
    """Open a regular lock file without truncating it or following links."""
    validate_safe_file_path(path, label="writer lease")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        actual = os.fstat(fd)
        named = path.lstat()
        if not stat.S_ISREG(actual.st_mode) or (
            actual.st_dev,
            actual.st_ino,
        ) != (named.st_dev, named.st_ino):
            raise OSError("unsafe lease")
        return fd
    except BaseException:
        os.close(fd)
        raise


class WriterLease:
    """Own an exclusive OS descriptor until close, never unlink its path.

    Attributes:
        path: Absolute project lock location.
        purpose: Safe caller-supplied activity label.
    """

    def __init__(self, path: Path, purpose: str, fd: int) -> None:
        """Retain an already acquired descriptor; use acquire to construct."""
        self.path = path.absolute()
        self.purpose = purpose
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, path: Path, *, purpose: str) -> WriterLease:
        """Acquire immediately or raise a value-safe LeaseError.

        Args:
            path: Shared project writer lock file.
            purpose: Activity label used in diagnostics.

        Returns:
            An owned context manager with a retained locked descriptor.

        Raises:
            LeaseError: If inspection, opening, or locking fails.
        """
        fd: int | None = None
        try:
            fd = _open_lock(path, create=True)
            _lock(fd)
            return cls(path, purpose, fd)
        except (OSError, PathConflictError):
            if fd is not None:
                os.close(fd)
            raise LeaseError(
                "writer lease unavailable: "
                f"{redact_secrets(str(path))} ({redact_secrets(purpose)})"
            ) from None

    @classmethod
    @contextmanager
    def hold(
        cls, path: Path, *, purpose: str, lease: WriterLease | None = None
    ) -> Iterator[WriterLease]:
        """Acquire a lease or borrow a live lease for this exact path.

        Args:
            path: Shared project writer lock file.
            purpose: Activity label for new ownership.
            lease: Optional caller-owned lease retained by the caller.

        Yields:
            A live lease; borrowed ownership is never closed here.

        Raises:
            LeaseError: If ownership is invalid or acquisition fails.
        """
        if lease is not None:
            if lease._fd is None or lease.path != path.absolute():
                raise LeaseError("invalid borrowed writer lease")
            lease.verify_identity()
            yield lease
        else:
            with cls.acquire(path, purpose=purpose) as owned:
                yield owned

    def verify_identity(self) -> int:
        """Prove the retained descriptor still owns the named regular inode."""
        if self._fd is None:
            raise LeaseError("closed writer lease identity")
        try:
            actual = os.fstat(self._fd)
            named = self.path.lstat()
            if not stat.S_ISREG(named.st_mode) or (
                actual.st_dev,
                actual.st_ino,
            ) != (named.st_dev, named.st_ino):
                raise LeaseError("writer lease identity changed")
            return actual.st_ino
        except OSError:
            raise LeaseError("writer lease identity unavailable") from None

    def close(self) -> None:
        """Release ownership even when the explicit unlock operation fails."""
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                _lock(fd, unlock=True)
            finally:
                os.close(fd)

    def __enter__(self) -> WriterLease:
        """Enter a live lifetime lease."""
        if self._fd is None:
            raise LeaseError("closed writer lease")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release on graceful and exceptional exits."""
        self.close()


def probe_writer_lease(path: Path) -> EvidenceState:
    """Observe an existing lock without creating or changing file contents.

    Args:
        path: Shared project lock location.

    Returns:
        CLEAR only after locking an existing file; BLOCKED for contention;
        UNKNOWN for missing, unsafe, or otherwise unobservable files.
        This observation never proves a legacy writer has stopped.
    """
    import errno

    try:
        fd = _open_lock(path, create=False)
    except (OSError, PathConflictError):
        return EvidenceState.UNKNOWN
    try:
        try:
            _lock(fd)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return EvidenceState.BLOCKED
            return EvidenceState.UNKNOWN
        _lock(fd, unlock=True)
        return EvidenceState.CLEAR
    except OSError:
        return EvidenceState.UNKNOWN
    finally:
        os.close(fd)
