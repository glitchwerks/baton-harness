"""Deterministic inode lifetime contracts for the portable host model."""

import os
import shutil
import sys

import pytest
from conftest import CutoverContext


@pytest.mark.parametrize("operation", ["unlink", "remove", "rmdir"])
def test_deleted_inode_loses_modeled_metadata(
    cutover_context: CutoverContext, operation: str
) -> None:
    """A future inode reuse cannot inherit a deleted node's metadata."""
    _, backend, storage = cutover_context
    path = backend.filesystem_root / "deleted"
    if operation == "rmdir":
        path.mkdir()
    else:
        path.write_bytes(b"previous inode owner")
    storage.set_metadata(path, 0o644, 7, 8)
    inode = path.stat().st_ino
    getattr(os, operation)(path)
    assert inode not in storage.fixture_metadata


@pytest.mark.parametrize("operation", ["rename", "replace"])
def test_source_rename_preserves_modeled_metadata(
    cutover_context: CutoverContext, operation: str
) -> None:
    """Moving a live inode keeps its metadata at the new path."""
    _, backend, storage = cutover_context
    source = backend.filesystem_root / "source"
    target = backend.filesystem_root / "target"
    source.write_bytes(b"source")
    storage.set_metadata(source, 0o640, 7, 8)
    getattr(os, operation)(source, target)
    assert storage.metadata(target) == (0o640, 7, 8)


@pytest.mark.parametrize("directory", [False, True])
def test_replacement_evicts_only_overwritten_inode(
    cutover_context: CutoverContext, directory: bool
) -> None:
    """Successful publication forgets the old target, not the source."""
    if directory and sys.platform != "linux":
        pytest.skip("directory replacement requires POSIX rename semantics")
    _, backend, storage = cutover_context
    source = backend.filesystem_root / "source"
    target = backend.filesystem_root / "target"
    for path in (source, target):
        if directory:
            path.mkdir()
        else:
            path.write_bytes(b"content")
    storage.set_metadata(source, 0o700, 7, 8)
    storage.set_metadata(target, 0o755, 9, 10)
    old_inode = target.stat().st_ino
    os.replace(source, target)
    assert old_inode not in storage.fixture_metadata
    assert storage.metadata(target) == (0o700, 7, 8)


@pytest.mark.parametrize("operation", ["unlink", "remove", "replace"])
def test_surviving_hardlink_keeps_modeled_metadata(
    cutover_context: CutoverContext, operation: str
) -> None:
    """Removing one name preserves a surviving inode until its last link."""
    _, backend, storage = cutover_context
    target = backend.filesystem_root / "target"
    survivor = backend.filesystem_root / "survivor"
    target.write_bytes(b"retained")
    os.link(target, survivor)
    storage.set_metadata(target, 0o640, 7, 8)
    inode = target.stat().st_ino
    if operation == "replace":
        source = backend.filesystem_root / "source"
        source.write_bytes(b"replacement")
        os.replace(source, target)
    else:
        getattr(os, operation)(target)
    assert storage.metadata(survivor) == (0o640, 7, 8)
    survivor.unlink()
    assert inode not in storage.fixture_metadata


@pytest.mark.parametrize("operation", ["unlink", "rmdir", "replace", "rename"])
def test_failed_namespace_change_keeps_modeled_metadata(
    cutover_context: CutoverContext, operation: str
) -> None:
    """Failed native operations leave the metadata cache untouched."""
    _, backend, storage = cutover_context
    target = backend.filesystem_root / "target"
    target.mkdir()
    (target / "child").write_bytes(b"nonempty")
    storage.set_metadata(target, 0o750, 7, 8)
    before = dict(storage.fixture_metadata)
    with pytest.raises(OSError):
        if operation in {"rename", "replace"}:
            getattr(os, operation)(target / "missing", target)
        else:
            getattr(os, operation)(target)
    assert storage.fixture_metadata == before


@pytest.mark.parametrize("operation", ["rename", "replace"])
@pytest.mark.parametrize("alias", [False, True])
def test_same_inode_noop_keeps_modeled_metadata(
    cutover_context: CutoverContext, operation: str, alias: bool
) -> None:
    """Same-path and POSIX hardlink rename no-ops retain live metadata."""
    if alias and sys.platform != "linux":
        pytest.skip("same-inode hardlink rename requires POSIX semantics")
    _, backend, storage = cutover_context
    source = backend.filesystem_root / "source"
    source.write_bytes(b"source")
    target = source
    if alias:
        target = backend.filesystem_root / "alias"
        os.link(source, target)
    storage.set_metadata(source, 0o640, 7, 8)
    getattr(os, operation)(source, target)
    assert storage.metadata(target) == (0o640, 7, 8)


@pytest.mark.skipif(sys.platform != "linux", reason="dir_fd is POSIX-only")
@pytest.mark.parametrize(
    "operation", ["unlink", "remove", "rmdir", "rename", "replace"]
)
def test_relative_descriptor_operations_evict_metadata(
    cutover_context: CutoverContext, operation: str
) -> None:
    """shutil-style descriptor-relative mutations address the right inode."""
    _, backend, storage = cutover_context
    root = backend.filesystem_root
    target = root / "target"
    if operation == "rmdir":
        target.mkdir()
    else:
        target.write_bytes(b"target")
    storage.set_metadata(target, 0o644, 7, 8)
    inode = target.stat().st_ino
    descriptor = os.open(root, os.O_RDONLY)
    try:
        if operation in {"rename", "replace"}:
            source = root / "source"
            source.write_bytes(b"source")
            storage.set_metadata(source, 0o600, 9, 10)
            getattr(os, operation)(
                "source",
                "target",
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            assert storage.metadata(target) == (0o600, 9, 10)
        else:
            getattr(os, operation)("target", dir_fd=descriptor)
    finally:
        os.close(descriptor)
    assert inode not in storage.fixture_metadata


def test_recursive_removal_evicts_descendants(
    cutover_context: CutoverContext,
) -> None:
    """Real shutil removal expires nested metadata, including Linux dir_fd."""
    _, backend, storage = cutover_context
    tree = backend.filesystem_root / "tree"
    nested = tree / "nested"
    nested.mkdir(parents=True)
    leaf = nested / "leaf"
    leaf.write_bytes(b"removed")
    survivor = backend.filesystem_root / "survivor"
    survivor.write_bytes(b"retained")
    deleted = (tree, nested, leaf)
    for path in (*deleted, survivor):
        storage.set_metadata(path, 0o750, 7, 8)
    inodes = [path.stat().st_ino for path in deleted]
    shutil.rmtree(tree)
    assert all(inode not in storage.fixture_metadata for inode in inodes)
    assert storage.metadata(survivor) == (0o750, 7, 8)
