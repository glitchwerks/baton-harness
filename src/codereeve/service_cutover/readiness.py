"""Root-owned commit receipts for a single gated systemd invocation.

The coordinator supplies commit authority by sequencing publication after
its durable commit and permanent unit reload. These helpers only enforce
receipt identity, ownership, atomic publication, and storage durability.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

from codereeve.service_cutover.model import CutoverError
from codereeve.service_cutover.storage import Storage

_GATE_ROOT = Path("/run/codereeve/cutover")
_STORAGE = Storage()
_P = ParamSpec("_P")
_R = TypeVar("_R")


class ReadinessError(CutoverError):
    """Startup receipt cannot establish committed authority."""


def _safe_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Keep paths, content, and OS diagnostics out of gate failures."""

    @wraps(function)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Preserve cancellation while reporting a fixed failure."""
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, TypeError, CutoverError):
            raise ReadinessError(
                "cutover receipt validation incomplete"
            ) from None

    return guarded


def _hex_id(value: str) -> bool:
    """Recognize exact transaction and systemd runtime identifiers."""
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{32}", value) is not None
    )


def gate_path(transaction_id: str) -> Path:
    """Return the fixed receipt location for a valid transaction ID.

    Args:
        transaction_id: The journal directory's UUID hex identifier.

    Returns:
        The absolute service-readable receipt path.

    Raises:
        ReadinessError: If the transaction identifier is not exact hex32.
    """
    if not _hex_id(transaction_id):
        raise ReadinessError("invalid cutover transaction identifier")
    return _GATE_ROOT / transaction_id / "committed.json"


def _expected(path: Path, pid: int, invocation_id: str) -> dict[str, object]:
    """Validate the path and identity and form the exact receipt payload."""
    if (
        path != gate_path(path.parent.name)
        or not path.is_absolute()
        or type(pid) is not int
        or pid <= 0
        or not _hex_id(invocation_id)
    ):
        raise ReadinessError("invalid cutover receipt identity")
    return {
        "schema_version": 1,
        "transaction_id": path.parent.name,
        "state": "committed",
        "pid": pid,
        "invocation_id": invocation_id,
    }


def _check_node(path: Path, storage: Storage, *, directory: bool) -> None:
    """Require root-owned, service-readable, non-writable regular nodes."""
    info = path.lstat()
    mode, uid, _gid = storage.metadata(path)
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    allowed_modes = (0o755, 0o555) if directory else (0o644, 0o444)
    if (
        not expected_kind(info.st_mode)
        or uid != 0
        or mode not in allowed_modes
    ):
        raise ReadinessError("unsafe cutover receipt ownership or mode")


def _ancestry(path: Path, storage: Storage) -> None:
    """Reject links and unsafe ownership all the way to the filesystem root."""
    for parent in reversed(path.parents):
        _check_node(parent, storage, directory=True)


@_safe_errors
def prepare_gate(transaction_id: str, *, storage: Storage = _STORAGE) -> Path:
    """Prepare root-controlled readable directories without releasing work.

    Args:
        transaction_id: Journal directory UUID hex identifier.
        storage: POSIX metadata/durability adapter; tests use disposable paths.

    Returns:
        The missing receipt path to place in the temporary unit environment.

    Raises:
        ReadinessError: On unsafe existing ancestry, receipt, or failed I/O.
    """
    if storage.owner()[0] != 0:
        raise ReadinessError("root receipt publisher required")
    path = gate_path(transaction_id)
    managed = {_GATE_ROOT.parent, _GATE_ROOT, path.parent}
    for parent in reversed(path.parents):
        try:
            parent.lstat()
        except FileNotFoundError:
            if parent not in managed:
                raise ReadinessError("missing runtime ancestry") from None
            parent.mkdir(mode=0o755)
            storage.set_metadata(parent, 0o755, 0, 0)
        _check_node(parent, storage, directory=True)
        if parent in managed:
            storage.sync_directory(parent)
            storage.sync_directory(parent.parent)
    if any(path.parent.iterdir()):
        raise ReadinessError("cutover receipt directory is not empty")
    return path


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous duplicate JSON keys instead of accepting the last."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReadinessError("duplicate receipt field")
        result[key] = value
    return result


@_safe_errors
def receipt_committed(
    path: Path, *, pid: int, invocation_id: str, storage: Storage = _STORAGE
) -> bool:
    """Validate an exact root-controlled receipt for the expected process.

    Args:
        path: Fixed transaction receipt path.
        pid: Current or freshly observed systemd main PID.
        invocation_id: Corresponding systemd runtime invocation identifier.
        storage: POSIX metadata/durability adapter.

    Returns:
        False for absence or a verified pending publication link; True only
        for the complete exact receipt with its temporary link removed.

    Raises:
        ReadinessError: On unsafe, unreadable, malformed, or mismatched data.
    """
    exists, pending = _receipt_state(path, pid, invocation_id, storage)
    return exists and pending is None


def _pending_link(path: Path, storage: Storage) -> Path | None:
    """Recognize only the owned exclusive-publication temporary hardlink."""
    info = path.lstat()
    if info.st_nlink == 1:
        return None
    if info.st_nlink != 2:
        raise ReadinessError("unaccounted receipt hardlinks")
    for candidate in path.parent.glob(".receipt-*"):
        try:
            other = candidate.lstat()
        except FileNotFoundError:
            continue  # Publisher may have completed its unlink meanwhile.
        if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
            try:
                _check_node(candidate, storage, directory=False)
            except (FileNotFoundError, CutoverError):
                if (
                    not candidate.exists()
                    and not candidate.is_symlink()
                    and path.lstat().st_nlink == 1
                ):
                    return None
                raise
            return candidate
    if path.lstat().st_nlink == 1:
        return None
    raise ReadinessError("unaccounted receipt hardlinks")


def _receipt_state(
    path: Path, pid: int, invocation_id: str, storage: Storage
) -> tuple[bool, Path | None]:
    """Validate bytes and distinguish absence from an owned pending link."""
    expected = _expected(path, pid, invocation_id)
    _ancestry(path, storage)
    try:
        path.lstat()
    except FileNotFoundError:
        return False, None
    _check_node(path, storage, directory=False)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        named = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (named.st_dev, named.st_ino):
            raise ReadinessError("receipt changed while opening")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ReadinessError("receipt exceeds size limit")
    content = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        not isinstance(content, dict)
        or type(content.get("schema_version")) is not int
        or type(content.get("pid")) is not int
        or content != expected
    ):
        raise ReadinessError("receipt does not match current invocation")
    _ancestry(path, storage)
    _check_node(path, storage, directory=False)
    after = path.lstat()
    if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
        raise ReadinessError("receipt changed while reading")
    return True, _pending_link(path, storage)


@_safe_errors
def publish_commit_receipt(
    path: Path, *, pid: int, invocation_id: str, storage: Storage = _STORAGE
) -> None:
    """Durably publish or reflush an identical committed receipt.

    Args:
        path: Previously prepared transaction receipt path.
        pid: Freshly verified main PID of the gated systemd invocation.
        invocation_id: Freshly verified systemd runtime identifier.
        storage: POSIX metadata/durability adapter.

    Raises:
        ReadinessError: If existing evidence is unsafe, differs, or I/O fails.

    The caller must first durably commit and reload the permanent ungated
    unit. Publication is exclusive: an existing mismatched receipt is never
    replaced. Retry re-establishes durability after uncertain interruption.
    """
    if storage.owner()[0] != 0:
        raise ReadinessError("root receipt publisher required")
    payload = _expected(path, pid, invocation_id)
    exists, pending = _receipt_state(path, pid, invocation_id, storage)
    if pending is not None:
        pending.unlink(missing_ok=True)
    if not exists:
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(
                    (json.dumps(payload, sort_keys=True) + "\n").encode(
                        "utf-8"
                    )
                )
                stream.flush()
                os.fsync(stream.fileno())
            storage.set_metadata(temporary_path, 0o644, 0, 0)
            storage.sync_file(temporary_path)
            # link publishes complete bytes atomically with no replacement,
            # unlike POSIX rename which can overwrite a concurrent receipt.
            try:
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError:
                pass  # A retry/racer must still prove identical evidence.
        finally:
            temporary_path.unlink(missing_ok=True)
    if not receipt_committed(
        path, pid=pid, invocation_id=invocation_id, storage=storage
    ):
        raise ReadinessError("receipt publication missing")
    storage.sync_file(path)
    storage.sync_directory(path.parent)
    if not receipt_committed(
        path, pid=pid, invocation_id=invocation_id, storage=storage
    ):
        raise ReadinessError("receipt changed during publication")


async def wait_for_commit(
    path: Path, *, storage: Storage = _STORAGE, interval_s: float = 0.25
) -> None:
    """Wait interruptibly for a receipt bound to this runtime invocation.

    Args:
        path: Unit-supplied transaction receipt path.
        storage: POSIX metadata/durability adapter.
        interval_s: Delay between observations; tests may shorten it.

    Raises:
        ReadinessError: On unsafe evidence or missing systemd identity.
        asyncio.CancelledError: When daemon shutdown cancels the wait.
    """
    pid = os.getpid()
    invocation_id = os.environ.get("INVOCATION_ID", "")
    while not receipt_committed(
        path, pid=pid, invocation_id=invocation_id, storage=storage
    ):
        await asyncio.sleep(interval_s)
