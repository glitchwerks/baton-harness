"""Keep portable inode metadata aligned with real namespace lifetimes."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable

import pytest


def track_inode_lifetimes(
    metadata: dict[int, tuple[int, int, int]], patch: pytest.MonkeyPatch
) -> None:
    """Forget dead inodes without altering filesystem or recovery behavior.

    The model intentionally follows inodes across rename and hard links.
    Linux can promptly recycle a deleted inode, so retaining its old modeled
    metadata incorrectly makes a new restoration look like operator drift.
    Eviction occurs only after a successful native namespace mutation and
    only when the removed node has no surviving names. This also applies to
    generic migration copies, outside the service-storage fault observer.
    Existing serialized inode dictionaries remain the process-state format.

    Args:
        metadata: Mutable inode-to-POSIX-metadata map owned by one fixture.
        patch: Fixture-scoped patch lifetime, including fresh recovery.
    """

    def node(path: object, descriptor: int | None) -> os.stat_result | None:
        """Inspect the named node itself, including dir_fd-relative paths."""
        try:
            return os.stat(path, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            # The real operation decides whether missing/inaccessible names
            # are errors; observation must not replace its native outcome.
            return None

    def forget(removed: os.stat_result | None) -> None:
        """Evict only a deleted directory or a file's final hard link."""
        if removed is not None and (
            stat.S_ISDIR(removed.st_mode) or removed.st_nlink == 1
        ):
            metadata.pop(removed.st_ino, None)

    for name in ("unlink", "remove", "rmdir"):
        original = getattr(os, name)

        def remove(
            path: object,
            *,
            dir_fd: int | None = None,
            _original: Callable[..., object] = original,
        ) -> object:
            """Retain metadata on failure or while another hard link lives."""
            removed = node(path, dir_fd)
            result = _original(path, dir_fd=dir_fd)
            forget(removed)
            return result

        patch.setattr(os, name, remove)

    for name in ("rename", "replace"):
        original = getattr(os, name)

        def move(
            source: object,
            destination: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            _original: Callable[..., object] = original,
        ) -> object:
            """Preserve source metadata and evict only overwritten nodes."""
            prior_source = node(source, src_dir_fd)
            prior_target = node(destination, dst_dir_fd)
            result = _original(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            same_inode = (
                prior_source is not None
                and prior_target is not None
                and (prior_source.st_dev, prior_source.st_ino)
                == (prior_target.st_dev, prior_target.st_ino)
            )
            if not same_inode:
                forget(prior_target)
            return result

        patch.setattr(os, name, move)
