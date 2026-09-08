"""Root-controlled startup receipts using disposable files and POSIX seams."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from codereeve.service_cutover import readiness as module
from codereeve.service_cutover.storage import Storage

TXID = "a" * 32
INVOCATION = "b" * 32


@pytest.fixture
def gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Storage, Path]:
    """Model POSIX ownership/durability only; all I/O uses real local files."""

    class Portable(Storage):
        """Replace unavailable Windows ownership and directory flushing."""

        def owner(self) -> tuple[int, int]:
            return (0, 0)

        def metadata(self, path: Path) -> tuple[int, int, int]:
            return metadata.get(
                path, (0o755 if path.is_dir() else 0o644, 0, 0)
            )

        def set_metadata(
            self, path: Path, mode: int, uid: int, gid: int
        ) -> None:
            metadata[path] = (mode, uid, gid)

        def sync_directory(self, path: Path) -> None:
            flushes.append(path)

        def sync_file(self, path: Path) -> None:
            flushes.append(path)

    metadata: dict[Path, tuple[int, int, int]] = {}
    flushes: list[Path] = []
    store = Portable()
    store.saved = metadata
    store.flushes = flushes
    monkeypatch.setattr(
        module, "_GATE_ROOT", tmp_path / "run" / "codereeve" / "cutover"
    )
    (tmp_path / "run").mkdir()
    return module, store, module.gate_path(TXID)


def test_commit_receipt_is_bound_and_durable(
    gate: tuple[Any, Any, Path],
) -> None:
    """A touched file cannot replace strict transaction/process evidence."""
    module, store, path = gate
    assert module.prepare_gate(TXID, storage=store) == path
    assert not module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    assert json.loads(path.read_text()) == {
        "schema_version": 1,
        "transaction_id": TXID,
        "state": "committed",
        "pid": 123,
        "invocation_id": INVOCATION,
    }
    assert module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    assert store.metadata(path) == (0o644, 0, 0)
    assert path.parent in store.flushes
    store.flushes.clear()
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    assert path in store.flushes and path.parent in store.flushes
    with pytest.raises(module.ReadinessError):
        module.prepare_gate(TXID, storage=store)


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "wrong_pid",
        "wrong_invocation",
        "wrong_txid",
        "extra",
        "boolean_pid",
        "boolean_schema",
        "uncommitted",
        "duplicate",
        "huge",
    ],
)
def test_invalid_receipt_never_opens(
    gate: tuple[Any, Any, Path], change: str
) -> None:
    """Exact schema, bounded bytes, and all identity fields are mandatory."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "transaction_id": TXID,
        "state": "committed",
        "pid": 123,
        "invocation_id": INVOCATION,
    }
    changes = {
        "wrong_pid": ("pid", 124),
        "wrong_invocation": ("invocation_id", "c" * 32),
        "wrong_txid": ("transaction_id", "c" * 32),
        "extra": ("secret", "SECRET_MARKER"),
        "boolean_pid": ("pid", True),
        "boolean_schema": ("schema_version", True),
        "uncommitted": ("state", "pending"),
    }
    if change in changes:
        key, value = changes[change]
        payload[key] = value
    content = json.dumps(payload)
    if change == "empty":
        content = ""
    elif change == "duplicate":
        content = content[:-1] + ', "pid": 123}'
    elif change == "huge":
        content += " " * 5000
    path.write_text(content)
    with pytest.raises(module.ReadinessError) as error:
        module.receipt_committed(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )
    assert "SECRET_MARKER" not in str(error.value)
    with pytest.raises(module.ReadinessError):
        module.publish_commit_receipt(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )
    assert path.read_text() == content


@pytest.mark.parametrize("target", ["receipt", "parent", "ancestor"])
@pytest.mark.parametrize("mode,uid", [(0o777, 0), (0o755, 42), (0o700, 0)])
def test_unsafe_or_unreadable_path_refuses(
    gate: tuple[Any, Any, Path], target: str, mode: int, uid: int
) -> None:
    """Neither service-writable nor service-inaccessible gates are usable."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    node = {
        "receipt": path,
        "parent": path.parent,
        "ancestor": path.parent.parent.parent,
    }[target]
    store.set_metadata(node, mode, uid, 0)
    with pytest.raises(module.ReadinessError):
        module.receipt_committed(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )


@pytest.mark.parametrize("target", ["receipt", "parent"])
def test_symlinks_fail_closed(
    gate: tuple[Any, Any, Path], target: str
) -> None:
    """A linked receipt or transaction directory cannot authorize work."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    if target == "receipt":
        other = path.with_name("other")
        other.write_text("{}")
        path.symlink_to(other)
    else:
        path.parent.rmdir()
        other = path.parent.with_name("other")
        other.mkdir()
        path.parent.symlink_to(other, target_is_directory=True)
    with pytest.raises(module.ReadinessError):
        module.receipt_committed(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )


def test_safe_interrupted_preparation_can_retry(
    gate: tuple[Any, Any, Path],
) -> None:
    """Interrupted preparation may reuse a safe empty transaction directory."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    assert module.prepare_gate(TXID, storage=store) == path
    store.set_metadata(path.parent, 0o777, 42, 42)
    with pytest.raises(module.ReadinessError):
        module.prepare_gate(TXID, storage=store)
    assert store.metadata(path.parent) == (0o777, 42, 42)


@pytest.mark.parametrize("phase", ["file", "directory"])
def test_interrupted_publication_reestablishes_durability(
    gate: tuple[Any, Any, Path], phase: str
) -> None:
    """Retry flushes visible evidence before reporting success."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    original = store.sync_file if phase == "file" else store.sync_directory

    def interrupt(node: Path) -> None:
        raise KeyboardInterrupt("SECRET_MARKER")

    name = "sync_file" if phase == "file" else "sync_directory"
    setattr(store, name, interrupt)
    with pytest.raises(KeyboardInterrupt):
        module.publish_commit_receipt(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )
    setattr(store, name, original)
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    assert module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )


def test_runtime_wait_is_interruptible_and_uses_process_identity(
    gate: tuple[Any, Any, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiting daemon releases only its own systemd invocation."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    monkeypatch.setenv("INVOCATION_ID", INVOCATION)

    async def exercise() -> None:
        task = asyncio.create_task(
            module.wait_for_commit(path, storage=store, interval_s=0.001)
        )
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        module.publish_commit_receipt(
            path, pid=os.getpid(), invocation_id=INVOCATION, storage=store
        )
        await asyncio.wait_for(module.wait_for_commit(path, storage=store), 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("value", ["", "A" * 32, "not-systemd"])
def test_invalid_runtime_identity_refuses_gate(
    gate: tuple[Any, Any, Path], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Interactive launches may beat but cannot satisfy a systemd gate."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    monkeypatch.setenv("INVOCATION_ID", value)
    with pytest.raises(module.ReadinessError):
        asyncio.run(module.wait_for_commit(path, storage=store))


def test_linked_publication_window_waits_and_retry_cleans_only_owned_temp(
    gate: tuple[Any, Any, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted exclusive link is pending, then recoverable on retry."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    unlink = Path.unlink

    def crash_before_unlink(node: Path, missing_ok: bool = False) -> None:
        if node.name.startswith(".receipt-"):
            raise KeyboardInterrupt()
        unlink(node, missing_ok=missing_ok)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "unlink", crash_before_unlink)
        with pytest.raises(KeyboardInterrupt):
            module.publish_commit_receipt(
                path, pid=123, invocation_id=INVOCATION, storage=store
            )
    assert path.stat().st_nlink == 2
    assert not module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    assert path.stat().st_nlink == 1
    assert list(path.parent.glob(".receipt-*")) == []
    assert module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )


@pytest.mark.parametrize("known_pair", [False, True])
def test_unaccounted_hardlinks_refuse(
    gate: tuple[Any, Any, Path], known_pair: bool
) -> None:
    """Only the exact temporary publication pair may remain pending."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    os.link(path, path.with_name("unaccounted"))
    if known_pair:
        os.link(path, path.with_name(".receipt-pending"))
    with pytest.raises(module.ReadinessError):
        module.receipt_committed(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )
    with pytest.raises(module.ReadinessError):
        module.publish_commit_receipt(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )
    assert path.with_name("unaccounted").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "elsewhere/committed.json",
        "../" + TXID + "/committed.json",
        TXID + "/wrong.json",
    ],
)
def test_receipt_path_is_fixed(
    gate: tuple[Any, Any, Path], relative: str
) -> None:
    """A syntactically valid identity cannot authorize another path."""
    module, store, path = gate
    with pytest.raises(module.ReadinessError):
        module.receipt_committed(
            path.parent.parent / relative,
            pid=123,
            invocation_id=INVOCATION,
            storage=store,
        )


@pytest.mark.parametrize("value", ["", "A" * 32, "abc", "../escape"])
def test_transaction_identifier_is_exact(
    gate: tuple[Any, Any, Path], value: str
) -> None:
    """Preparation cannot escape the dedicated runtime transaction root."""
    module, store, path = gate
    with pytest.raises(module.ReadinessError):
        module.prepare_gate(value, storage=store)


def test_directory_receipt_refuses(gate: tuple[Any, Any, Path]) -> None:
    """A directory at the expected receipt name is an error, never a wait."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    path.mkdir()
    with pytest.raises(module.ReadinessError):
        module.receipt_committed(
            path, pid=123, invocation_id=INVOCATION, storage=store
        )


def test_reader_tolerates_publisher_removing_its_pending_link(
    gate: tuple[Any, Any, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publisher finishing between link stat and validation is not unsafe."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    pending = path.with_name(".receipt-finishing")
    os.link(path, pending)
    original = module._check_node

    def finish(node: Path, storage: Storage, *, directory: bool) -> None:
        if node == pending:
            pending.unlink()
        original(node, storage, directory=directory)

    monkeypatch.setattr(module, "_check_node", finish)
    assert module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )


def test_preparation_refuses_nonempty_transaction_directory(
    gate: tuple[Any, Any, Path],
) -> None:
    """A reused transaction directory must have no unexplained artifacts."""
    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    other = path.with_name("unrelated")
    other.write_text("SECRET_MARKER")
    with pytest.raises(module.ReadinessError):
        module.prepare_gate(TXID, storage=store)
    assert other.read_text() == "SECRET_MARKER"


def test_reader_tolerates_pending_link_removed_during_metadata(
    gate: tuple[Any, Any, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POSIX adapter can wrap a concurrent unlink as CutoverError."""
    from codereeve.service_cutover.model import CutoverError

    module, store, path = gate
    module.prepare_gate(TXID, storage=store)
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    pending = path.with_name(".receipt-finishing")
    os.link(path, pending)
    original = store.metadata

    def finish(node: Path) -> tuple[int, int, int]:
        if node == pending:
            pending.unlink()
            raise CutoverError("filesystem operation incomplete")
        return original(node)

    monkeypatch.setattr(store, "metadata", finish)
    assert module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )


@pytest.mark.parametrize(
    "effect", ["mkdir", "link", "file_fsync", "directory_fsync"]
)
@pytest.mark.parametrize("timing", ["before", "after"])
def test_interrupted_creation_and_publication_retry(
    gate: tuple[Any, Any, Path],
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
    timing: str,
) -> None:
    """Each visible effect can be safely retried after either interruption."""
    module, store, path = gate
    if effect != "mkdir":
        module.prepare_gate(TXID, storage=store)

    if effect == "mkdir":
        target, name = Path, "mkdir"
    elif effect == "link":
        target, name = os, "link"
    else:
        target, name = (
            store,
            "sync_file" if effect == "file_fsync" else "sync_directory",
        )
    original = getattr(target, name)

    def interrupt(*args: object, **kwargs: object) -> None:
        if timing == "before":
            raise KeyboardInterrupt()
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    with monkeypatch.context() as patcher:
        patcher.setattr(target, name, interrupt)
        with pytest.raises(KeyboardInterrupt):
            if effect == "mkdir":
                module.prepare_gate(TXID, storage=store)
            else:
                module.publish_commit_receipt(
                    path, pid=123, invocation_id=INVOCATION, storage=store
                )
    if effect == "mkdir":
        assert module.prepare_gate(TXID, storage=store) == path
    module.publish_commit_receipt(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
    assert module.receipt_committed(
        path, pid=123, invocation_id=INVOCATION, storage=store
    )
