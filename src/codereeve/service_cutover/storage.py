"""Private, no-follow filesystem snapshots with explicit durability seams."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

from codereeve.service_cutover.model import CutoverError

_P = ParamSpec("_P")
_R = TypeVar("_R")


def fixed_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Prevent OS exception values from escaping the storage boundary."""

    @wraps(function)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Retain artifacts and report only a fixed diagnostic."""
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, TypeError, KeyError):
            raise CutoverError("filesystem operation incomplete") from None

    return guarded


@dataclass(frozen=True)
class Node:
    """Exact file representation; contents remain in private backup blobs."""

    name: str
    kind: str
    mode: int
    uid: int
    gid: int
    digest: str


@dataclass(frozen=True)
class FileSnapshot:
    """One original path including absence and an exact directory tree."""

    path: str
    nodes: tuple[Node, ...]

    def metadata(self) -> dict[str, object]:
        """Return public metadata without original file contents."""
        return {"path": self.path, "nodes": [asdict(n) for n in self.nodes]}


class Storage:
    """Real Linux storage; portable tests override metadata and durability."""

    @fixed_errors
    def owner(self) -> tuple[int, int]:
        """Require native POSIX ownership instead of weakening guarantees."""
        if sys.platform == "win32":
            raise CutoverError("POSIX storage unsupported")
        return os.geteuid(), os.getegid()

    @fixed_errors
    def metadata(self, path: Path) -> tuple[int, int, int]:
        """Read no-follow permissions and ownership."""
        self.owner()
        info = path.lstat()
        return stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid

    @fixed_errors
    def set_metadata(self, path: Path, mode: int, uid: int, gid: int) -> None:
        """Restore exact ownership and mode without following links."""
        if sys.platform == "win32":
            raise CutoverError("POSIX storage unsupported")
        self.owner()
        self.safe(path, leaf_link=True)
        os.chown(path, uid, gid, follow_symlinks=False)
        if not path.is_symlink():
            os.chmod(path, mode, follow_symlinks=False)
        if self.metadata(path) != (mode, uid, gid):
            raise CutoverError("filesystem metadata restoration incomplete")
        if path.is_symlink():
            self.sync_directory(path.parent)
        elif path.is_dir():
            self.sync_directory(path)
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @fixed_errors
    def sync_directory(self, path: Path) -> None:
        """Make directory effects durable or fail on unsupported hosts."""
        if sys.platform == "win32":
            raise CutoverError("POSIX durability unsupported")
        self.owner()
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @fixed_errors
    def sync_file(self, path: Path) -> None:
        """Flush regular file data without following a substituted file."""
        self.owner()
        self.safe(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            named = path.lstat()
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (named.st_dev, named.st_ino):
                raise CutoverError("filesystem changed before durability")
            os.fsync(fd)
            after = path.lstat()
            if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
                raise CutoverError("filesystem changed during durability")
        finally:
            os.close(fd)

    @fixed_errors
    def make_durable(
        self,
        path: Path,
        snapshot: FileSnapshot,
        *,
        namespace_parents: tuple[Path, ...] = (),
    ) -> None:
        """Verify and flush an observed tree and its affected namespace.

        Matching visible bytes alone do not establish durability. Both new
        execution and replay flush every regular file, all tree directories,
        and the source/destination namespace parents before completion.
        """
        self.owner()
        if not self.matches(path, snapshot):
            raise CutoverError("filesystem durability snapshot mismatch")
        identities: dict[Path, tuple[int, int]] = {}
        directories: set[Path] = set()
        for node in snapshot.nodes:
            target = path / node.name
            info = target.lstat()
            identities[target] = (info.st_dev, info.st_ino)
            if node.kind == "directory":
                directories.add(target)
        for parent in (path.parent, *namespace_parents):
            # An absent nested destination has no immediate parent to flush.
            # Flush the existing ancestor which proves its absent namespace.
            while not parent.exists() and not parent.is_symlink():
                parent = parent.parent
            self.safe(parent)
            if not parent.is_dir():
                raise CutoverError("filesystem namespace is not a directory")
            directories.add(parent)
        for directory in directories:
            info = directory.lstat()
            identities[directory] = (info.st_dev, info.st_ino)
        for node in snapshot.nodes:
            if node.kind == "file":
                self.sync_file(path / node.name)
        for directory in sorted(
            directories, key=lambda item: (-len(item.parts), str(item))
        ):
            self.sync_directory(directory)
        for target, identity in identities.items():
            self.safe(target, leaf_link=True)
            info = target.lstat()
            if identity != (info.st_dev, info.st_ino):
                raise CutoverError("filesystem identity changed during flush")
        if not self.matches(path, snapshot):
            raise CutoverError("filesystem changed during durability")

    @fixed_errors
    def safe(self, path: Path, *, leaf_link: bool = False) -> None:
        """Reject traversal, unsafe parents, special files and hardlinks."""
        if not path.is_absolute() or ".." in path.parts:
            raise CutoverError("unsafe filesystem path")
        for ancestor in reversed(path.parents):
            if ancestor.is_symlink():
                raise CutoverError("unsafe filesystem parent")
            if ancestor.exists() and not ancestor.is_dir():
                raise CutoverError("unsafe filesystem parent")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) and leaf_link:
            return
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise CutoverError("unsupported filesystem type")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise CutoverError("multiply linked filesystem input")

    @fixed_errors
    def private(self, path: Path, *, directory: bool = False) -> None:
        """Validate private recovery storage ownership, type and mode."""
        self.safe(path)
        expected = (0o700 if directory else 0o600, *self.owner())
        if path.is_dir() != directory or self.metadata(path) != expected:
            raise CutoverError("recovery storage is not private")

    @fixed_errors
    def mkdir(self, path: Path) -> None:
        """Exclusively create and durably publish a private directory."""
        self.safe(path)
        self.owner()
        path.mkdir(mode=0o700)
        self.set_metadata(path, 0o700, *self.owner())
        self.sync_directory(path)
        self.sync_directory(path.parent)

    @fixed_errors
    def read(self, path: Path) -> bytes:
        """Read regular bytes with no-follow descriptor identity checking."""
        self.safe(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise CutoverError("unsafe file read")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read()
            after = path.lstat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise CutoverError("filesystem changed during read")
            return data
        finally:
            os.close(fd)

    @fixed_errors
    def write(self, path: Path, data: bytes) -> None:
        """Create private bytes exclusively and flush before returning."""
        self.safe(path)
        self.owner()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self.set_metadata(path, 0o600, *self.owner())
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        self.sync_directory(path.parent)

    @fixed_errors
    def inspect(self, path: Path, *, mask: bool = False) -> FileSnapshot:
        """Fingerprint exact bytes, types, ownership, modes and absence."""
        self.safe(path, leaf_link=mask)
        if not path.exists() and not path.is_symlink():
            return FileSnapshot(str(path), ())
        nodes: list[Node] = []
        pending = [path]
        while pending:
            current = pending.pop(0)
            self.safe(current, leaf_link=mask and current == path)
            mode, uid, gid = self.metadata(current)
            name = current.relative_to(path).as_posix()
            if current.is_symlink():
                if not mask or os.readlink(current) != "/dev/null":
                    raise CutoverError("unsupported symbolic link")
                kind, digest = "mask", ""
            elif current.is_dir():
                kind, digest = "directory", ""
                pending.extend(sorted(current.iterdir()))
            else:
                kind = "file"
                digest = hashlib.sha256(self.read(current)).hexdigest()
            nodes.append(Node(name, kind, mode, uid, gid, digest))
        return FileSnapshot(str(path), tuple(nodes))

    @fixed_errors
    def matches(self, path: Path, snapshot: FileSnapshot) -> bool:
        """Compare all captured representation fields against live inputs."""
        return self.inspect(path, mask=True).nodes == snapshot.nodes

    @fixed_errors
    def capture(
        self, path: Path, backup: Path, *, mask: bool = False
    ) -> FileSnapshot:
        """Copy original bytes privately and reject concurrent source drift."""
        snapshot = self.inspect(path, mask=mask)
        for index, node in enumerate(snapshot.nodes):
            if node.kind == "file":
                self.write(backup / str(index), self.read(path / node.name))
        if not self.matches(path, snapshot):
            raise CutoverError("snapshot source changed")
        self.verify_backup(snapshot, backup)
        return snapshot

    @fixed_errors
    def verify_backup(self, snapshot: FileSnapshot, backup: Path) -> None:
        """Verify private blob identities before granting restore authority."""
        self.private(backup, directory=True)
        expected = {
            str(i) for i, n in enumerate(snapshot.nodes) if n.kind == "file"
        }
        if {p.name for p in backup.iterdir()} != expected:
            raise CutoverError("unexpected backup inventory")
        for index, node in enumerate(snapshot.nodes):
            if node.kind == "file":
                blob = backup / str(index)
                self.private(blob)
                if hashlib.sha256(self.read(blob)).hexdigest() != node.digest:
                    raise CutoverError("backup checksum mismatch")

    @fixed_errors
    def move(self, source: Path, destination: Path) -> None:
        """Preserve a same-filesystem tree without replacing any target."""
        self.owner()
        self.safe(source, leaf_link=True)
        self.safe(destination)
        if destination.exists() or destination.is_symlink():
            raise CutoverError("quarantine collision")
        if source.lstat().st_dev != destination.parent.stat().st_dev:
            raise CutoverError("quarantine crosses filesystems")
        snapshot = self.inspect(source, mask=True)
        source.rename(destination)
        self.make_durable(
            destination, snapshot, namespace_parents=(source.parent,)
        )

    @fixed_errors
    def restore(self, snapshot: FileSnapshot, backup: Path) -> None:
        """Exclusively recreate verified bytes; retain partial failures."""
        self.owner()
        path = Path(snapshot.path)
        self.safe(path)
        self.verify_backup(snapshot, backup)
        if path.exists() or path.is_symlink():
            raise CutoverError("restoration target is occupied")
        self._restore_nodes(
            snapshot, backup, set(), "filesystem restoration incomplete"
        )

    @fixed_errors
    def replace_journal(self, source: Path, target: Path) -> None:
        """Atomically publish a flushed journal on the same filesystem."""
        self.owner()
        self.private(source)
        self.private(target)
        if source.parent != target.parent:
            raise CutoverError("journal replacement crosses directories")
        os.replace(source, target)
        self.sync_directory(target.parent)

    @fixed_errors
    def resume_restore(self, snapshot: FileSnapshot, backup: Path) -> None:
        """Resume a partial copy only when every existing node matches.

        Unknown bytes, extra paths, or foreign metadata remain untouched and
        require operator recovery. Missing nodes are still created exclusively.
        """
        self.owner()
        path = Path(snapshot.path)
        self.verify_backup(snapshot, backup)
        actual = self.inspect(path, mask=True)
        expected = {node.name: node for node in snapshot.nodes}
        existing = {node.name: node for node in actual.nodes}
        for name, node in existing.items():
            wanted = expected.get(name)
            if wanted is None or (node.kind, node.digest) != (
                wanted.kind,
                wanted.digest,
            ):
                raise CutoverError("partial restoration drift")
            original = (wanted.mode, wanted.uid, wanted.gid)
            temporary = (
                0o700 if node.kind == "directory" else 0o600,
                *self.owner(),
            )
            if (node.mode, node.uid, node.gid) not in {original, temporary}:
                raise CutoverError("partial restoration metadata drift")
        self._restore_nodes(
            snapshot, backup, set(existing), "partial restoration incomplete"
        )

    def _restore_nodes(
        self,
        snapshot: FileSnapshot,
        backup: Path,
        existing: set[str],
        failure: str,
    ) -> None:
        """Create missing nodes and finalize a verified restoration."""
        path = Path(snapshot.path)
        for index, node in enumerate(snapshot.nodes):
            if node.name in existing:
                continue
            target = path / node.name
            if node.kind == "directory":
                self.mkdir(target)
            elif node.kind == "file":
                self.write(target, self.read(backup / str(index)))
            else:
                target.symlink_to("/dev/null")
                self.sync_directory(target.parent)
        for node in reversed(snapshot.nodes):
            self.set_metadata(path / node.name, node.mode, node.uid, node.gid)
            self.sync_directory((path / node.name).parent)
        if not self.matches(path, snapshot):
            raise CutoverError(failure)
        self.make_durable(path, snapshot)
