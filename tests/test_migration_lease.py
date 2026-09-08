"""Real cooperative writer exclusion and conservative observations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codereeve.migration.lease import (
    LeaseError,
    WriterLease,
    probe_writer_lease,
)
from codereeve.migration.model import EvidenceState


def test_same_process_contention_release_and_noncreating_probe(
    tmp_path: Path,
) -> None:
    """A held descriptor excludes writers; a leftover file does not."""
    path = tmp_path / ".codereeve-migration.lock"
    assert probe_writer_lease(path) is EvidenceState.UNKNOWN
    assert not path.exists()
    path.write_bytes(b"")
    before = path.read_bytes()
    with WriterLease.acquire(path, purpose="daemon"):
        assert probe_writer_lease(path) is EvidenceState.BLOCKED
        with pytest.raises(LeaseError, match="migration"):
            WriterLease.acquire(path, purpose="migration")
    assert path.read_bytes() == before
    assert probe_writer_lease(path) is EvidenceState.CLEAR
    with WriterLease.acquire(path, purpose="migration"):
        pass


def test_child_process_contends_then_acquires(tmp_path: Path) -> None:
    """The OS lock excludes a distinct Python process immediately."""
    path = tmp_path / ".codereeve-migration.lock"
    code = """
import sys
from pathlib import Path
from codereeve.migration.lease import WriterLease, LeaseError
try:
    with WriterLease.acquire(Path(sys.argv[1]), purpose="child"):
        pass
except LeaseError:
    sys.exit(7)
"""
    with WriterLease.acquire(path, purpose="daemon"):
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], timeout=10, check=False
        )
        assert result.returncode == 7
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)], timeout=10, check=False
    )
    assert result.returncode == 0


def test_borrow_checks_ownership_and_does_not_release(tmp_path: Path) -> None:
    """A daemon can borrow only a live lease for its exact project."""
    path = tmp_path / "lock"
    with WriterLease.acquire(path, purpose="cli") as lease:
        with WriterLease.hold(path, purpose="daemon", lease=lease):
            assert probe_writer_lease(path) is EvidenceState.BLOCKED
        assert probe_writer_lease(path) is EvidenceState.BLOCKED
        with pytest.raises(LeaseError):
            with WriterLease.hold(
                tmp_path / "other", purpose="daemon", lease=lease
            ):
                pass
    with pytest.raises(LeaseError):
        with WriterLease.hold(path, purpose="daemon", lease=lease):
            pass


def test_unsafe_lock_and_backend_error_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Special paths and backend failures cannot expose exception values."""
    path = tmp_path / "lock"
    path.mkdir()
    assert probe_writer_lease(path) is EvidenceState.UNKNOWN
    with pytest.raises(LeaseError):
        WriterLease.acquire(path, purpose="daemon")

    def fail(fd: int, *, unlock: bool = False) -> None:
        """Simulate an unavailable OS lock implementation."""
        raise OSError("TOP_SECRET")

    monkeypatch.setattr("codereeve.migration.lease._lock", fail)
    with pytest.raises(LeaseError) as failure:
        WriterLease.acquire(tmp_path / "new", purpose="daemon")
    assert "TOP_SECRET" not in str(failure.value)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_platform_backends_use_nonblocking_lock_and_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """Both OS seams use exclusive nonblocking acquisition and release."""
    import os
    import types

    from codereeve.migration import lease as lease_mod

    calls: list[tuple[int, ...]] = []
    path = tmp_path / "lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    module = types.ModuleType("backend")
    if platform == "win32":
        module.LK_NBLCK = 2
        module.LK_UNLCK = 0
        module.locking = lambda *args: calls.append(args)
        name = "msvcrt"
    else:
        module.LOCK_EX = 2
        module.LOCK_NB = 4
        module.LOCK_UN = 8
        module.flock = lambda *args: calls.append(args)
        name = "fcntl"
    try:
        with monkeypatch.context() as patcher:
            patcher.setitem(sys.modules, name, module)
            patcher.setattr(sys, "platform", platform)
            lease_mod._lock(fd)
            lease_mod._lock(fd, unlock=True)
    finally:
        os.close(fd)
    assert calls == (
        [(fd, 2, 1), (fd, 0, 1)] if platform == "win32" else [(fd, 6), (fd, 8)]
    )
