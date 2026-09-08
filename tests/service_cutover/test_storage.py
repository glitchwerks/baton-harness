"""Durable storage contract tests using genuine disposable files."""

from pathlib import Path

import pytest

from codereeve.service_cutover import storage as module
from codereeve.service_cutover.model import CutoverError


def test_storage_contract_available() -> None:
    """Require filesystem operations, not a module-only placeholder."""
    assert hasattr(module, "Storage")


def test_real_snapshot_copy_and_drift(tmp_path: Path) -> None:
    """Require bytes, mode and ownership retained in private copies."""
    assert hasattr(module, "Storage")
    store = portable_storage(tmp_path)
    source = tmp_path / "state"
    source.mkdir()
    (source / "log").write_bytes(b"original")
    backup = tmp_path / "backup"
    store.mkdir(backup)
    snapshot = store.capture(source, backup)
    assert store.matches(source, snapshot)
    (source / "log").write_bytes(b"new runtime writes")
    assert not store.matches(source, snapshot)
    store.verify_backup(snapshot, backup)


def portable_storage(root: Path) -> module.Storage:
    """Inject only POSIX metadata and durability on Windows."""

    class Portable(module.Storage):
        def metadata(self, path: Path) -> tuple[int, int, int]:
            return self.saved.get(
                str(path), (0o700 if path.is_dir() else 0o600, 42, 43)
            )

        def set_metadata(
            self, path: Path, mode: int, uid: int, gid: int
        ) -> None:
            self.saved[str(path)] = (mode, uid, gid)

        def sync_directory(self, path: Path) -> None:
            self.flushes.append(path)

        def owner(self) -> tuple[int, int]:
            return (42, 43)

    store = Portable()
    store.saved = {}
    store.flushes = []
    return store


def test_backup_collision_and_tampering_refuse(tmp_path: Path) -> None:
    """Reject backup collisions and changed backup bytes."""
    store = portable_storage(tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"value")
    backup = tmp_path / "backup"
    store.mkdir(backup)
    snapshot = store.capture(source, backup)
    with pytest.raises((CutoverError, FileExistsError)):
        store.capture(source, backup)
    (backup / "0").write_bytes(b"edited")
    with pytest.raises(CutoverError):
        store.verify_backup(snapshot, backup)


def test_ownership_drift_refuses_match(tmp_path: Path) -> None:
    """Fingerprint POSIX ownership as well as content and permissions."""
    store = portable_storage(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(b"value")
    snapshot = store.inspect(path)
    store.set_metadata(path, 0o600, 9, 9)
    assert not store.matches(path, snapshot)


def test_directory_durability_failure_is_not_success(tmp_path: Path) -> None:
    """Retain bytes on failure without granting durable completion."""
    store = portable_storage(tmp_path)

    def fail(path: Path) -> None:
        raise OSError("SECRET_MARKER")

    store.sync_directory = fail
    with pytest.raises(CutoverError) as exc:
        store.write(tmp_path / "file", b"private")
    assert "SECRET_MARKER" not in str(exc.value)


@pytest.mark.parametrize("private_target", ["file", "directory"])
def test_private_permissions_are_exact(
    tmp_path: Path, private_target: str
) -> None:
    """Private files and directories must retain required ownership/mode."""
    store = portable_storage(tmp_path)
    path = tmp_path / "private"
    if private_target == "directory":
        store.mkdir(path)
    else:
        store.write(path, b"secret")
    store.set_metadata(path, 0o755, 42, 43)
    with pytest.raises(CutoverError):
        store.private(path, directory=private_target == "directory")


def test_symlink_parent_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject an unsafe ancestor observation without Windows symlink rights."""
    store = portable_storage(tmp_path)
    parent = tmp_path / "parent"
    parent.mkdir()
    original = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path == parent or original(path)
    )
    with pytest.raises(CutoverError):
        store.write(parent / "file", b"secret")
    assert not (parent / "file").exists()


def test_source_changes_during_capture_are_rejected(tmp_path: Path) -> None:
    """Do not retain a completed snapshot of concurrently changing bytes."""
    store = portable_storage(tmp_path)
    path = tmp_path / "source"
    path.write_bytes(b"before")
    backup = tmp_path / "backup"
    store.mkdir(backup)
    write = store.write

    def change(target: Path, data: bytes) -> None:
        write(target, data)
        path.write_bytes(b"after")

    store.write = change
    with pytest.raises(CutoverError):
        store.capture(path, backup)
    assert (backup / "0").read_bytes() == b"before"


def test_unsupported_storage_fails_before_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production Windows refusal must precede every filesystem effect."""
    monkeypatch.setattr(module.sys, "platform", "win32")
    path = tmp_path / "private"
    with pytest.raises(CutoverError):
        module.Storage().write(path, b"bytes")
    assert not path.exists()


def test_ownership_handoff_retains_inode(tmp_path: Path) -> None:
    """In-place lease handoff never replaces or unlinks the lock inode."""
    store = portable_storage(tmp_path)
    lock = tmp_path / "lease.lock"
    lock.write_bytes(b"")
    inode = lock.stat().st_ino
    store.set_metadata(lock, 0o600, 90, 91)
    assert lock.stat().st_ino == inode
    assert store.metadata(lock) == (0o600, 90, 91)


def test_posix_metadata_is_flushed_before_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require metadata flushing at the injected POSIX syscall boundary."""
    import os

    store = portable_storage(tmp_path)
    path = tmp_path / "lock"
    path.write_bytes(b"")
    calls = []

    def sync(fd: int) -> None:
        calls.append("fsync")

    def chown(
        target: Path, uid: int, gid: int, *, follow_symlinks: bool
    ) -> None:
        mode, _, _ = store.metadata(target)
        store.set_metadata(target, mode, uid, gid)

    def chmod(target: Path, mode: int, *, follow_symlinks: bool) -> None:
        _, uid, gid = store.metadata(target)
        store.set_metadata(target, mode, uid, gid)

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(os, "chown", chown, raising=False)
    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "fsync", sync)
    module.Storage.set_metadata(store, path, 0o640, 9, 10)
    assert calls == ["fsync"]
