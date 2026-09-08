"""Private, checksummed transaction metadata and fail-closed recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codereeve.migration.model import (
    EvidenceState,
    MigrationAction,
    MigrationEvidence,
    MigrationReport,
    MigrationStatus,
)
from codereeve.paths import PathConflictError, validate_safe_file_path
from codereeve.redact import redact_secrets

_OPERATIONS = {"stage", "backup", "publish", "cleanup"}
_PHASES = {
    "created",
    "planned",
    "before",
    "after",
    "rollback_before",
    "rollback_after",
    "complete",
    "restored",
}
_ACTION_CODES = {
    "migrate_config",
    "migrate_state",
    "migrate_baseline",
    "migrate_host",
    "migrate_secrets",
}


class JournalError(RuntimeError):
    """Unsafe, corrupt, unsupported, or non-durable transaction metadata."""


def fsync_directory(path: Path) -> None:
    """Flush directory changes or explicitly reject unsupported platforms.

    Args:
        path: Existing directory to make durable.

    Raises:
        JournalError: Windows exposes no stdlib directory fsync capability.
        OSError: A supported platform cannot flush this directory.
    """
    if os.name == "nt":
        raise JournalError("directory durability unsupported on this platform")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(fd: int) -> None:
    """Flush a file descriptor; kept separate for fault injection."""
    os.fsync(fd)


def _private_directory(path: Path, mode: int = 0o700) -> None:
    """Create one private directory exclusively without changing ancestors."""
    validate_safe_file_path(path / "probe", label="transaction")
    path.mkdir(mode=mode)


def _json(value: object) -> bytes:
    """Encode deterministic UTF-8 metadata for hashing and persistence."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _digest(value: object) -> str:
    """Hash canonical JSON metadata."""
    return hashlib.sha256(_json(value)).hexdigest()


def _safe_path(value: object) -> Path:
    """Reject ambiguous paths and paths that would persist secret tokens."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise JournalError("invalid metadata path")
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or redact_secrets(value) != value
    ):
        raise JournalError("unsafe metadata path")
    return path


def _read_file(path: Path, *, private: bool = False) -> bytes:
    """Read a regular file with no-follow and descriptor identity checks."""
    validate_safe_file_path(path, label="transaction metadata")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        info = os.fstat(fd)
        named = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            raise JournalError("unsafe metadata file")
        if private and os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise JournalError("metadata permissions are not private")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of accepting the last value."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JournalError("duplicate metadata key")
        result[key] = value
    return result


def _decode(data: bytes) -> dict[str, Any]:
    """Decode exactly one JSON object with unique keys."""
    value = json.loads(data, object_pairs_hook=_object_pairs)
    if not isinstance(value, dict):
        raise JournalError("invalid metadata object")
    return value


@dataclass(frozen=True)
class FileMetadata:
    """One original input path, type, mode, and SHA-256; never contents."""

    path: Path
    kind: str
    mode: int
    sha256: str | None

    def as_dict(self) -> dict[str, object]:
        """Return the closed JSON metadata shape."""
        return {
            "path": str(self.path),
            "kind": self.kind,
            "mode": self.mode,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Manifest:
    """Validated immutable input inventory for a single transaction.

    Attributes:
        transaction_id: UTC timestamp plus random nonce.
        created_at: UTC ISO timestamp.
        actions: Original immutable migration plan, in deterministic order.
        entries: Metadata for every source directory and regular file.
        checksum: SHA-256 over the canonical manifest payload.
    """

    transaction_id: str
    created_at: str
    actions: tuple[MigrationAction, ...]
    entries: tuple[FileMetadata, ...]
    checksum: str


@dataclass(frozen=True)
class JournalEvent:
    """A closed metadata event, with operation details only when planned.

    Attributes:
        operation_id: Unique ASCII identifier; empty for global events.
        phase: created/planned/before/after/rollback_before/rollback_after/
            complete/restored. Recovery treats before as uncertain mutation.
        operation: stage/backup/publish/cleanup for planned events only.
        source: Absolute mutation source for planned events.
        destination: Absolute mutation destination for planned events.
        entries: Optional expected staged/source metadata for verification.
    """

    operation_id: str
    phase: str
    operation: str | None = None
    source: Path | None = None
    destination: Path | None = None
    entries: tuple[FileMetadata, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Render only allowed metadata fields, never caller data blobs."""
        return {
            "operation_id": self.operation_id,
            "phase": self.phase,
            "operation": self.operation,
            "source": str(self.source) if self.source is not None else None,
            "destination": str(self.destination)
            if self.destination is not None
            else None,
            "entries": [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class RecoveryState:
    """Strict parser result; manual recovery always forbids mutation."""

    manifest: Manifest | None = None
    events: tuple[JournalEvent, ...] = ()
    terminal: bool = False
    manual_recovery: bool = False
    diagnostic: str = ""


def _metadata(value: object) -> FileMetadata:
    """Validate one file record with exact fields and primitive types."""
    if not isinstance(value, dict) or set(value) != {
        "path",
        "kind",
        "mode",
        "sha256",
    }:
        raise JournalError("invalid file metadata")
    kind, mode, digest = value["kind"], value["mode"], value["sha256"]
    if (
        kind not in ("file", "directory")
        or type(mode) is not int
        or not 0 <= mode <= 0o777
    ):
        raise JournalError("invalid file metadata")
    if (kind == "directory" and digest is not None) or (
        kind == "file"
        and (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        )
    ):
        raise JournalError("invalid file checksum")
    return FileMetadata(_safe_path(value["path"]), kind, mode, digest)


def _event(value: object) -> JournalEvent:
    """Validate one closed event schema before state-machine processing."""
    if not isinstance(value, dict) or set(value) != {
        "operation_id",
        "phase",
        "operation",
        "source",
        "destination",
        "entries",
    }:
        raise JournalError("invalid journal event")
    op_id, phase = value["operation_id"], value["phase"]
    if (
        not isinstance(op_id, str)
        or not isinstance(phase, str)
        or phase not in _PHASES
        or not isinstance(value["entries"], list)
    ):
        raise JournalError("invalid journal event")
    if phase in {"created", "complete", "restored"}:
        if op_id:
            raise JournalError("global event has operation id")
    elif not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", op_id):
        raise JournalError("invalid operation id")
    source: Path | None
    destination: Path | None
    if phase == "planned":
        if (
            not isinstance(value["operation"], str)
            or value["operation"] not in _OPERATIONS
        ):
            raise JournalError("invalid planned operation")
        source = _safe_path(value["source"])
        destination = _safe_path(value["destination"])
    else:
        if (
            any(
                value[key] is not None
                for key in ("operation", "source", "destination")
            )
            or value["entries"]
        ):
            raise JournalError("unexpected event metadata")
        source = destination = None
    return JournalEvent(
        op_id,
        phase,
        value["operation"],
        source,
        destination,
        tuple(_metadata(item) for item in value["entries"]),
    )


def _validate_transitions(events: tuple[JournalEvent, ...]) -> None:
    """Reject gaps, duplicate boundaries, and pending terminal work."""
    states: dict[str, str] = {}
    previous = ""
    rollback = False
    for index, event in enumerate(events):
        phase, op_id = event.phase, event.operation_id
        if index == 0:
            if phase != "created":
                raise JournalError("missing initial journal event")
        elif (
            phase == "created"
            or previous == "restored"
            or (previous == "complete" and phase != "rollback_before")
        ):
            raise JournalError("invalid journal transition")
        elif phase == "planned":
            if op_id in states or rollback or previous == "complete":
                raise JournalError("invalid operation plan")
            states[op_id] = phase
        elif phase in {"complete", "restored"}:
            allowed = (
                {"after"}
                if phase == "complete"
                else {"planned", "rollback_after"}
            )
            if (
                (phase == "complete" and not states)
                or not set(states.values()) <= allowed
                or (phase == "complete" and rollback)
            ):
                raise JournalError("incomplete operations at terminal event")
        else:
            allowed_prior = {
                "before": {"planned"},
                "after": {"before"},
                "rollback_before": {"before", "after"},
                "rollback_after": {"rollback_before"},
            }
            if (
                phase not in allowed_prior
                or states.get(op_id) not in allowed_prior[phase]
            ):
                raise JournalError("invalid operation transition")
            if phase.startswith("rollback"):
                rollback = True
            elif rollback or previous == "complete":
                raise JournalError("forward operation after rollback")
            states[op_id] = phase
        previous = phase


def _snapshot(path: Path) -> tuple[FileMetadata, ...]:
    """Capture input metadata recursively, rejecting every linked input."""
    path = _safe_path(str(path.absolute()))
    validate_safe_file_path(
        path / "probe" if path.is_dir() else path, label="migration input"
    )
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode) & 0o777
    if stat.S_ISREG(info.st_mode):
        return (
            FileMetadata(
                path,
                "file",
                mode,
                hashlib.sha256(_read_file(path)).hexdigest(),
            ),
        )
    if stat.S_ISDIR(info.st_mode):
        result = [FileMetadata(path, "directory", mode, None)]
        for child in sorted(path.iterdir()):
            result.extend(_snapshot(child))
        return tuple(result)
    raise JournalError("unsafe migration input")


def verify_manifest(path: Path) -> Manifest:
    """Read and strictly validate a manifest without changing the filesystem.

    Args:
        path: A transaction's manifest.json path.

    Returns:
        Immutable original inputs and their verified metadata checksum.

    Raises:
        JournalError: Malformed, unsafe, unreadable, or changed metadata.
    """
    try:
        wrapper = _decode(_read_file(path, private=True))
        if set(wrapper) != {"payload", "checksum"}:
            raise JournalError("invalid manifest envelope")
        data = wrapper["payload"]
        if (
            not isinstance(data, dict)
            or set(data)
            != {
                "schema_version",
                "transaction_id",
                "created_at",
                "actions",
                "entries",
            }
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
        ):
            raise JournalError("invalid manifest schema")
        if wrapper["checksum"] != _digest(data):
            raise JournalError("manifest checksum mismatch")
        transaction_id = data["transaction_id"]
        if (
            not isinstance(transaction_id, str)
            or not re.fullmatch(
                r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}", transaction_id
            )
            or transaction_id != path.parent.name
        ):
            raise JournalError("invalid transaction identity")
        timestamp = datetime.fromisoformat(data["created_at"])
        if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
            raise JournalError("invalid transaction timestamp")
        if (
            not isinstance(data["actions"], list)
            or not data["actions"]
            or not isinstance(data["entries"], list)
        ):
            raise JournalError("invalid manifest inventory")
        actions = []
        for action in data["actions"]:
            if (
                not isinstance(action, dict)
                or set(action) != {"code", "scope", "source", "destination"}
                or action["code"] not in _ACTION_CODES
                or not isinstance(action["scope"], str)
                or not re.fullmatch(r"[a-z_]+", action["scope"])
            ):
                raise JournalError("invalid manifest action")
            actions.append(
                MigrationAction(
                    action["code"],
                    action["scope"],
                    _safe_path(action["source"]),
                    _safe_path(action["destination"]),
                )
            )
        entries = tuple(_metadata(item) for item in data["entries"])
        if len({entry.path for entry in entries}) != len(entries) or not {
            a.source for a in actions
        } <= {entry.path for entry in entries}:
            raise JournalError("invalid source inventory")
        return Manifest(
            transaction_id,
            data["created_at"],
            tuple(actions),
            entries,
            wrapper["checksum"],
        )
    except (OSError, PathConflictError, ValueError, TypeError, KeyError):
        raise JournalError(
            "manifest unavailable or corrupt; manual recovery required"
        ) from None


def _load(path: Path) -> RecoveryState:
    """Read checksummed events tied to one immutable manifest."""
    manifest = verify_manifest(path.parent / "manifest.json")
    raw = _read_file(path, private=True)
    if not raw or not raw.endswith(b"\n"):
        raise JournalError("empty or truncated journal")
    events = []
    previous = manifest.checksum
    for sequence, line in enumerate(raw.splitlines()):
        record = _decode(line)
        if (
            set(record) != {"sequence", "previous", "event", "checksum"}
            or type(record["sequence"]) is not int
            or record["sequence"] != sequence
            or record["previous"] != previous
        ):
            raise JournalError("invalid journal sequence")
        payload = {
            key: record[key] for key in ("sequence", "previous", "event")
        }
        if record["checksum"] != _digest(payload):
            raise JournalError("journal checksum mismatch")
        previous = record["checksum"]
        events.append(_event(record["event"]))
    _validate_transitions(tuple(events))
    return RecoveryState(
        manifest, tuple(events), events[-1].phase in {"complete", "restored"}
    )


def load_incomplete_journal(path: Path) -> RecoveryState:
    """Return strict read-only recovery evidence; corruption never clears.

    Args:
        path: Existing journal.jsonl path.

    Returns:
        Parsed events or explicit manual-recovery evidence on any failure.
    """
    try:
        return _load(path)
    except (
        JournalError,
        OSError,
        PathConflictError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return RecoveryState(
            manual_recovery=True,
            diagnostic=(
                "transaction metadata unavailable or corrupt; "
                "manual recovery required"
            ),
        )


def _append(path: Path, data: bytes) -> None:
    """Append one complete line and fsync before returning."""
    validate_safe_file_path(path, label="journal")
    fd = os.open(
        path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise JournalError("unsafe journal")
        if os.write(fd, data) != len(data):
            raise JournalError(
                "partial journal append; manual recovery required"
            )
        _fsync_file(fd)
    finally:
        os.close(fd)


class MigrationJournal:
    """Append-only writer; callers must retain the project writer lease."""

    def __init__(self, path: Path) -> None:
        """Retain artifact locations; use create or open for validation."""
        self.path = path
        self.manifest_path = path.parent / "manifest.json"
        self._failed = False

    @classmethod
    def create(
        cls, transaction_root: Path, report: MigrationReport, now: datetime
    ) -> MigrationJournal:
        """Create exclusive private artifacts, inventorying metadata only.

        Args:
            transaction_root: Project .codereeve-migration directory.
            report: Ready plan with at least one original input.
            now: A timezone-aware timestamp, normalized to UTC.

        Returns:
            Durable journal containing its initial created event.

        Raises:
            JournalError: Unsafe inputs, duplicate identity, unsupported
                durability, or any failed persistence boundary.
        """
        try:
            if (
                report.status is not MigrationStatus.READY
                or not report.actions
                or now.utcoffset() is None
            ):
                raise JournalError("ready plan and aware timestamp required")
            transaction_root = _safe_path(str(transaction_root.absolute()))
            validate_safe_file_path(
                transaction_root / "probe", label="transaction root"
            )
            fsync_directory(transaction_root.parent)
            now = now.astimezone(timezone.utc)
            transaction_id = (
                now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
            )
            entries = tuple(
                entry
                for action in report.actions
                for entry in _snapshot(action.source)
            )
            if not transaction_root.exists():
                _private_directory(transaction_root)
                fsync_directory(transaction_root.parent)
            elif (
                os.name != "nt"
                and stat.S_IMODE(transaction_root.stat().st_mode) & 0o077
            ):
                raise JournalError(
                    "transaction root permissions are not private"
                )
            directory = transaction_root / transaction_id
            _private_directory(directory)
            fsync_directory(transaction_root)
            data = {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "created_at": now.isoformat(),
                "actions": [
                    {
                        **asdict(action),
                        "source": str(action.source.absolute()),
                        "destination": str(action.destination.absolute()),
                    }
                    for action in report.actions
                ],
                "entries": [entry.as_dict() for entry in entries],
            }
            temporary = directory / "manifest.tmp"
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                content = (
                    _json({"payload": data, "checksum": _digest(data)}) + b"\n"
                )
                if os.write(fd, content) != len(content):
                    raise JournalError("partial manifest write")
                _fsync_file(fd)
            finally:
                os.close(fd)
            os.replace(temporary, directory / "manifest.json")
            fsync_directory(directory)
            path = directory / "journal.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            verify_manifest(directory / "manifest.json")
            payload = {
                "sequence": 0,
                "previous": _digest(data),
                "event": JournalEvent("", "created").as_dict(),
            }
            _append(
                path, _json({**payload, "checksum": _digest(payload)}) + b"\n"
            )
            fsync_directory(directory)
            return cls(path)
        except (OSError, PathConflictError, ValueError, TypeError):
            raise JournalError(
                "transaction creation failed; inspect recovery metadata"
            ) from None

    @classmethod
    def open(cls, path: Path) -> MigrationJournal:
        """Reopen a verified journal under a caller-owned writer lease."""
        if load_incomplete_journal(path).manual_recovery:
            raise JournalError("journal corrupt; manual recovery required")
        return cls(path)

    def record(self, event: JournalEvent) -> None:
        """Validate and durably append one event before returning.

        Args:
            event: Typed metadata boundary with no file contents.

        Raises:
            JournalError: Invalid transition or failed persistence. A
                failed writer cannot append again; recovery must reopen.
        """
        if self._failed:
            raise JournalError("journal writer failed; reopen for recovery")
        try:
            state = _load(self.path)
            validated = _event(event.as_dict())
            _validate_transitions((*state.events, validated))
            last = _decode(
                _read_file(self.path, private=True).splitlines()[-1]
            )
            payload = {
                "sequence": len(state.events),
                "previous": last["checksum"],
                "event": validated.as_dict(),
            }
            try:
                _append(
                    self.path,
                    _json({**payload, "checksum": _digest(payload)}) + b"\n",
                )
            except JournalError:
                self._failed = True
                raise
        except (OSError, PathConflictError, ValueError, TypeError):
            self._failed = True
            raise JournalError(
                "journal append failed; manual recovery required"
            ) from None


def probe_journals(transaction_root: Path) -> MigrationEvidence:
    """Inspect existing artifacts without creating, acquiring, or mutating.

    Args:
        transaction_root: Project .codereeve-migration directory.

    Returns:
        Journal-only evidence; writers, lease, and service stay UNKNOWN.
    """
    verified: list[Path] = []
    try:
        validate_safe_file_path(
            transaction_root / "probe", label="transactions"
        )
        if not transaction_root.exists():
            return MigrationEvidence(journals=EvidenceState.CLEAR)
        for directory in sorted(transaction_root.iterdir()):
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise JournalError("unsafe transaction directory")
            state = load_incomplete_journal(directory / "journal.jsonl")
            if state.manual_recovery or not state.terminal:
                raise JournalError("incomplete transaction")
            verified.append(directory)
        return MigrationEvidence(
            journals=EvidenceState.CLEAR, verified_transactions=tuple(verified)
        )
    except (JournalError, OSError, PathConflictError):
        return MigrationEvidence(journals=EvidenceState.BLOCKED)
