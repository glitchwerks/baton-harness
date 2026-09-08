"""Lease-protected migration with durable, reversible publication records."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from codereeve.config_env import (
    PRODUCT_ALIASES,
    EnvLayer,
    parse_env_text,
    resolve_environment,
    rewrite_assignments,
)
from codereeve.migration.inventory import inventory_migration
from codereeve.migration.journal import (
    FileMetadata,
    JournalEvent,
    Manifest,
    MigrationJournal,
    fsync_directory,
    load_incomplete_journal,
    probe_journals,
    verify_manifest,
)
from codereeve.migration.lease import WriterLease
from codereeve.migration.model import (
    EvidenceState,
    MigrationContext,
    MigrationEvidence,
    MigrationReport,
    MigrationStatus,
)
from codereeve.paths import validate_safe_file_path


class RestorationStatus(str, Enum):
    """Evidence a service coordinator must inspect before any restart."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    NOT_NEEDED = "not_needed"


@dataclass(frozen=True)
class RestorationResult:
    """Value-free recovery outcome; incomplete never authorizes restart."""

    status: RestorationStatus
    manifest_path: Path
    diagnostic: str = ""


@dataclass(frozen=True)
class AppliedMigration:
    """Durable completed transaction and retained original backup locations."""

    manifest_path: Path
    backups: tuple[Path, ...]


class MigrationError(RuntimeError):
    """Value-free failure with optional structured restoration evidence."""

    def __init__(
        self,
        diagnostic: str,
        restoration: RestorationResult | None = None,
    ) -> None:
        """Retain safe recovery evidence, never raw storage exception text."""
        super().__init__(diagnostic)
        self.restoration = restoration


class FileOperations:
    """Storage and coordinator boundary for apply and restore.

    Override verify_quiescence only with an authoritative coordinator that
    freshly verifies ALL relevant writers stopped and keeps them stopped
    until the call returns, including uncooperative legacy writers. A prior
    inventory, absent marker, process-name scan, or lock probe is not proof.
    Storage overrides must preserve exclusive creation, durability, and
    same-filesystem atomic replacement contracts.
    """

    def verify_quiescence(
        self,
        project: Path,
        lease: WriterLease,
    ) -> MigrationEvidence:
        """Fail closed without an external shutdown coordinator.

        Args:
            project: Exact managed project covered by this transaction.
            lease: Currently retained exclusive project writer lease.

        Raises:
            MigrationError: Standalone operations cannot prove shutdown.
        """
        raise MigrationError("fresh all-writer shutdown verification required")

    def boundary(self, name: str, phase: str, path: Path) -> None:
        """Expose before/after storage boundaries without secret contents."""

    def sync_file(self, fd: int) -> None:
        """Durably flush an open file; metadata paths are not secret values."""
        self.boundary("file_fsync", "before", Path("file"))
        os.fsync(fd)
        self.boundary("file_fsync", "after", Path("file"))

    def sync_directory(self, path: Path) -> None:
        """Flush a directory or reject unsupported Windows durability."""
        self.boundary("directory_fsync", "before", path)
        fsync_directory(path)
        self.boundary("directory_fsync", "after", path)

    def device(self, path: Path) -> int:
        """Return a safely inspected existing directory's device."""
        _safe(path)
        return path.lstat().st_dev

    def create_journal(
        self,
        root: Path,
        report: MigrationReport,
    ) -> MigrationJournal:
        """Persist manifest and initial event using this durability seam."""
        self.boundary("journal_create", "before", root)
        journal = MigrationJournal.create(
            root,
            report,
            datetime.now(timezone.utc),
            directory_sync=self.sync_directory,
            file_sync=self.sync_file,
        )
        self.boundary("journal_create", "after", journal.manifest_path)
        return journal

    def record(self, journal: MigrationJournal, event: JournalEvent) -> None:
        """Wrap durable append, including uncertain write failures."""
        self.boundary("journal_record", "before", journal.path)
        journal.record(event)
        self.boundary("journal_record", "after", journal.path)

    def move(
        self,
        source: Path,
        destination: Path,
        name: str,
        entries: tuple[FileMetadata, ...],
    ) -> None:
        """Atomically move into an absent destination on the same device."""
        self.boundary(name, "before", destination)
        _verify(source, entries)
        _absent(destination)
        if self.device(source.parent) != self.device(destination.parent):
            raise MigrationError("cross-filesystem publication blocked")
        os.replace(source, destination)
        self.sync_directory(source.parent)
        if destination.parent != source.parent:
            self.sync_directory(destination.parent)
        self.boundary(name, "after", destination)


REAL_FILE_OPERATIONS = FileOperations()


def _safe(path: Path) -> os.stat_result | None:
    """Reject linked ancestors and nonregular files without resolving paths."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        validate_safe_file_path(path, label="migration")
        return None
    validate_safe_file_path(
        path / "probe" if stat.S_ISDIR(info.st_mode) else path,
        label="migration",
    )
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise MigrationError("unsafe migration path")
    return info


def _absent(path: Path) -> None:
    """Require a destination absent, including dangling links."""
    if _safe(path) is not None:
        raise MigrationError("migration destination already exists")


def _read(path: Path) -> bytes:
    """Read a no-follow regular descriptor and compare named identity."""
    _safe(path.parent)
    validate_safe_file_path(path, label="migration input")
    before = path.lstat()
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        actual = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode) or (
            before.st_dev,
            before.st_ino,
        ) != (actual.st_dev, actual.st_ino):
            raise MigrationError("migration input changed")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read()
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            actual.st_dev,
            actual.st_ino,
            actual.st_size,
            actual.st_mtime_ns,
        ):
            raise MigrationError("migration input changed")
        return content
    finally:
        os.close(fd)


def _snapshot(path: Path) -> tuple[FileMetadata, ...]:
    """Hash a complete tree through no-follow descriptors in bounded chunks."""
    info = _safe(path)
    if info is None:
        raise MigrationError("expected migration path missing")
    mode = stat.S_IMODE(info.st_mode) & 0o777
    if stat.S_ISDIR(info.st_mode):
        result = [FileMetadata(path, "directory", mode, None)]
        for child in sorted(path.iterdir()):
            result.extend(_snapshot(child))
        return tuple(result)
    validate_safe_file_path(path, label="migration input")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (info.st_dev, info.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise MigrationError("migration input changed")
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise MigrationError("migration input changed")
        return (FileMetadata(path, "file", mode, digest.hexdigest()),)
    finally:
        os.close(fd)


def _relocate(
    entries: tuple[FileMetadata, ...],
    source: Path,
    destination: Path,
) -> tuple[FileMetadata, ...]:
    """Rebase an immutable tree inventory without reading any file."""
    return tuple(
        replace(entry, path=destination / entry.path.relative_to(source))
        for entry in entries
    )


def _verify(path: Path, entries: tuple[FileMetadata, ...]) -> None:
    """Require exact current path set, kinds, hashes, and permissions."""
    if set(_snapshot(path)) != set(entries):
        raise MigrationError("migration content or metadata changed")


def _converted(path: Path, project: Path) -> bytes:
    """Canonicalize known keys and exact defaults using the shared parser."""
    text = _read(path).decode("utf-8")
    assignments = parse_env_text(text, source="migration config")
    defaults = {
        "CODEREEVE_RUNLOG_PATH": "runlog.jsonl",
        "CODEREEVE_HEARTBEAT_FILE": "heartbeat",
        "CODEREEVE_FAILURE_COUNTS_PATH": "failure-counts.json",
        "CODEREEVE_REDISPATCH_COUNTS_PATH": "dispatch-counts.json",
    }
    paths = {}
    paths["CODEREEVE_DAEMON_SECRETS_PATH"] = (
        "/etc/bh-daemon/secrets.env",
        "/etc/codereeve/secrets.env",
    )
    values = resolve_environment(
        (
            EnvLayer(
                "migration config",
                {item.key: item.value for item in assignments},
            ),
        ),
        export_legacy=False,
    ).values
    for key, name in defaults.items():
        old, new = f".baton-harness/{name}", f".codereeve/{name}"
        absolute_old = str(project / old)
        if values.get(key) == absolute_old:
            old, new = absolute_old, str(project / new)
        paths[key] = (old, new)
    rewritten = rewrite_assignments(assignments, path_values=paths)
    _verify_config(rewritten.encode("utf-8"))
    if "\r\n" in text:
        rewritten = rewritten.replace("\n", "\r\n")
    return rewritten.encode("utf-8")


def _verify_config(content: bytes) -> None:
    """Reject remaining product aliases and resolve with exports disabled."""
    assignments = parse_env_text(
        content.decode("utf-8"), source="staged config"
    )
    values = {item.key: item.value for item in assignments}
    if {alias.legacy for alias in PRODUCT_ALIASES} & values.keys():
        raise MigrationError("staged config depends on compatibility")
    resolve_environment(
        (EnvLayer("staged config", values),), export_legacy=False
    )


@dataclass(frozen=True)
class _Output:
    """One expected output and its original input or synthetic directory."""

    metadata: FileMetadata
    source: Path | None = None
    content: bytes | None = None


@dataclass(frozen=True)
class _Group:
    """One indivisible canonical publication and private sibling stage."""

    destination: Path
    outputs: tuple[_Output, ...]


def _groups(manifest: Manifest, project: Path) -> tuple[_Group, ...]:
    """Combine managed inputs; publish absent host parents as whole trees."""
    groups: dict[Path, list[_Output]] = {}
    managed = project / ".codereeve"
    for action in manifest.actions:
        destination = action.destination
        root = (
            managed
            if action.scope in {"config", "baseline", "state"}
            else destination
        )
        if root != managed and _safe(root.parent) is None:
            root = root.parent
        outputs = groups.setdefault(root, [])
        for entry in manifest.entries:
            if (
                entry.path != action.source
                and action.source not in entry.path.parents
            ):
                continue
            target = destination / entry.path.relative_to(action.source)
            content = (
                _converted(entry.path, project)
                if action.scope in {"config", "host", "secrets"}
                else None
            )
            mode = entry.mode & (0o700 if entry.kind == "directory" else 0o600)
            # Windows chmod exposes read/write bits rather than POSIX modes.
            if os.name == "nt":
                mode = (
                    0o777
                    if entry.kind == "directory"
                    else (0o666 if mode & 0o200 else 0o444)
                )
            metadata = FileMetadata(
                target,
                entry.kind,
                mode,
                hashlib.sha256(content).hexdigest()
                if content is not None
                else entry.sha256,
            )
            outputs.append(_Output(metadata, entry.path, content))
    result = []
    for root, outputs in groups.items():
        if all(output.metadata.path != root for output in outputs):
            outputs.append(
                _Output(
                    FileMetadata(
                        root,
                        "directory",
                        0o777 if os.name == "nt" else 0o700,
                        None,
                    )
                )
            )
        result.append(
            _Group(
                root,
                tuple(
                    sorted(
                        outputs,
                        key=lambda output: (
                            len(output.metadata.path.parts),
                            str(output.metadata.path),
                        ),
                    )
                ),
            )
        )
    return tuple(result)


def _write_output(
    output: _Output, path: Path, operations: FileOperations
) -> None:
    """Exclusively create and flush one staged directory or file."""
    _absent(path)
    entry = output.metadata
    if entry.kind == "directory":
        path.mkdir(mode=entry.mode)
        operations.sync_directory(path.parent)
        return
    operations.boundary("copy", "before", path)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        entry.mode,
    )
    try:
        if output.source is None:
            raise MigrationError("missing copy source")
        # Config rewrite is its own observable boundary; never shell-evaluate.
        if output.content is not None:
            operations.boundary("rewrite", "before", path)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            if output.content is not None:
                stream.write(output.content)
            else:
                _copy_stream(output.source, stream)
            stream.flush()
        os.chmod(path, entry.mode)
        operations.sync_file(fd)
        if output.content is not None:
            operations.boundary("rewrite", "after", path)
    finally:
        os.close(fd)
    operations.sync_directory(path.parent)
    operations.boundary("copy", "after", path)


def _copy_stream(source: Path, destination: BinaryIO) -> None:
    """Stream regular source bytes with descriptor and named-path guards."""
    validate_safe_file_path(source, label="migration source")
    before = source.lstat()
    fd = os.open(
        source,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise MigrationError("migration copy source changed")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            shutil.copyfileobj(stream, destination, length=1024 * 1024)
        after = source.lstat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise MigrationError("migration copy source changed")
    finally:
        os.close(fd)


def verify_staged_migration(stage: Path, manifest: Manifest) -> None:
    """Verify the combined managed stage against original and converted inputs.

    Args:
        stage: Private combined managed staging directory.
        manifest: Validated immutable original input inventory.

    Raises:
        MigrationError: Missing, changed, unsafe, or alias-dependent output.
    """
    try:
        project = _project_from_manifest(manifest)
        group = next(
            (
                group
                for group in _groups(manifest, project)
                if group.destination == project / ".codereeve"
            ),
            None,
        )
        if group is None:
            raise MigrationError("manifest has no managed stage")
        entries = tuple(output.metadata for output in group.outputs)
        _verify(stage, _relocate(entries, group.destination, stage))
        if any(
            entry.path == group.destination / "config.env" for entry in entries
        ):
            _verify_config(_read(stage / "config.env"))
    except MigrationError:
        raise
    except Exception:
        raise MigrationError(
            "staged verification failed; inspect migration inputs"
        ) from None


def _project_from_manifest(manifest: Manifest) -> Path:
    """Read managed project identity from its canonical action paths."""
    for action in manifest.actions:
        if action.scope == "state":
            return action.destination.parent
        if action.scope in {"config", "baseline"}:
            return action.destination.parent.parent
    raise MigrationError("managed project identity requires journal location")


def _quiescent(
    operations: FileOperations, project: Path, lease: WriterLease
) -> MigrationEvidence:
    """Require fresh coordinator authority independent of context snapshots."""
    evidence = operations.verify_quiescence(project, lease)
    if (
        evidence.writers is not EvidenceState.CLEAR
        or evidence.service is not EvidenceState.CLEAR
    ):
        raise MigrationError("fresh all-writer shutdown verification required")
    return evidence


def _report(
    context: MigrationContext, evidence: MigrationEvidence | None = None
) -> MigrationReport:
    """Validate local inputs and strict existing journals without mutation."""
    journals = probe_journals(
        context.layout.canonical_state.parent / ".codereeve-migration"
    )
    observed = evidence or context.evidence
    report = inventory_migration(
        replace(
            context,
            evidence=replace(
                observed,
                journals=journals.journals,
                verified_transactions=journals.verified_transactions,
            ),
        )
    )
    # Service evidence is freshly supplied later; all data blockers are early.
    blockers = [
        finding
        for finding in report.findings
        if finding.blocking
        and (
            evidence is not None or finding.code != "service_state_unverified"
        )
    ]
    if blockers or not report.actions:
        raise MigrationError(
            "migration inventory blocked or no data migration needed"
        )
    return replace(report, status=MigrationStatus.READY)


def apply_migration(
    context: MigrationContext,
    *,
    operations: FileOperations = REAL_FILE_OPERATIONS,
    lease: WriterLease | None = None,
) -> AppliedMigration:
    """Stage, verify, back up, and atomically publish under verified shutdown.

    Args:
        context: Explicit layout, environment, and advisory inventory evidence.
        operations: Durable storage and authoritative shutdown coordinator.
        lease: Optional caller-owned project lease, never released here.

    Returns:
        Completed manifest and timestamped backups retained through 0.3.x.

    Raises:
        MigrationError: Unsafe readiness, unsupported durability, or failed
            transaction. Attached restoration evidence distinguishes recovery.
    """
    journal: MigrationJournal | None = None
    try:
        _report(context)
        project = context.layout.canonical_state.parent.absolute()
        operations.sync_directory(
            project
        )  # Reject unsupported OS pre-mutation.
        with WriterLease.hold(
            project / ".codereeve-migration.lock",
            purpose="migration",
            lease=lease,
        ) as lease:
            evidence = _quiescent(operations, project, lease)
            report = _report(
                context, replace(evidence, lease=EvidenceState.CLEAR)
            )
            try:
                journal = operations.create_journal(
                    project / ".codereeve-migration", report
                )
                manifest = verify_manifest(journal.manifest_path)
                groups = _groups(manifest, project)
                staged = []
                for index, group in enumerate(groups):
                    _absent(group.destination)
                    stage = group.destination.with_name(
                        f".codereeve-stage-{manifest.transaction_id}-{index}"
                    )
                    if operations.device(stage.parent) != operations.device(
                        group.destination.parent
                    ):
                        raise MigrationError(
                            "cross-filesystem staging blocked"
                        )
                    entries = _relocate(
                        tuple(item.metadata for item in group.outputs),
                        group.destination,
                        stage,
                    )
                    op_id = f"stage-{index}"
                    operations.record(
                        journal,
                        JournalEvent(
                            op_id,
                            "planned",
                            "stage",
                            group.destination,
                            stage,
                            entries,
                        ),
                    )
                    operations.record(journal, JournalEvent(op_id, "before"))
                    for output in group.outputs:
                        _write_output(
                            output,
                            stage
                            / output.metadata.path.relative_to(
                                group.destination
                            ),
                            operations,
                        )
                    operations.boundary("verify", "before", stage)
                    _verify(stage, entries)
                    if group.destination == project / ".codereeve":
                        config = stage / "config.env"
                        if any(entry.path == config for entry in entries):
                            _verify_config(_read(config))
                    operations.boundary("verify", "after", stage)
                    operations.record(journal, JournalEvent(op_id, "after"))
                    staged.append((stage, group, entries))
                # All stages verified before removing a single original input.
                for action in manifest.actions:
                    _verify(
                        action.source,
                        tuple(
                            entry
                            for entry in manifest.entries
                            if entry.path == action.source
                            or action.source in entry.path.parents
                        ),
                    )
                backups = []
                for index, action in enumerate(manifest.actions):
                    backup = action.source.with_name(
                        f"{action.source.name}.codereeve-backup-{manifest.transaction_id}"
                    )
                    entries = tuple(
                        entry
                        for entry in manifest.entries
                        if entry.path == action.source
                        or action.source in entry.path.parents
                    )
                    _move_recorded(
                        journal,
                        f"backup-{index}",
                        "backup",
                        action.source,
                        backup,
                        entries,
                        operations,
                    )
                    backups.append(backup)
                for index, (stage, group, entries) in enumerate(staged):
                    _move_recorded(
                        journal,
                        f"publish-{index}",
                        "publish",
                        stage,
                        group.destination,
                        entries,
                        operations,
                    )
                operations.boundary(
                    "final_verify", "before", journal.manifest_path
                )
                for stage, group, entries in staged:
                    _verify(
                        group.destination,
                        _relocate(entries, stage, group.destination),
                    )
                operations.boundary(
                    "final_verify", "after", journal.manifest_path
                )
                operations.record(journal, JournalEvent("", "complete"))
                return AppliedMigration(journal.manifest_path, tuple(backups))
            except Exception:
                result = (
                    _restore_locked(journal.manifest_path, operations)
                    if journal is not None
                    else None
                )
                raise MigrationError(
                    "migration failed; inspect restoration evidence", result
                ) from None
    except MigrationError:
        raise
    except Exception:
        raise MigrationError(
            "migration blocked; no safe completion evidence"
        ) from None


def _move_recorded(
    journal: MigrationJournal,
    op_id: str,
    name: str,
    source: Path,
    destination: Path,
    entries: tuple[FileMetadata, ...],
    operations: FileOperations,
) -> None:
    """Journal uncertain moves, then verify before recording completion."""
    operations.record(
        journal,
        JournalEvent(op_id, "planned", name, source, destination, entries),
    )
    operations.record(journal, JournalEvent(op_id, "before"))
    _verify(source, entries)
    _absent(destination)
    operations.move(source, destination, name, entries)
    _verify(destination, _relocate(entries, source, destination))
    operations.record(journal, JournalEvent(op_id, "after"))


def _reverse(event: JournalEvent, operations: FileOperations) -> None:
    """Reverse verified active paths; retain private stages and backups."""
    source, destination = event.source, event.destination
    if source is None or destination is None:
        raise MigrationError("invalid recovery operation")
    if event.operation == "stage":
        # A partial private stage is retained as evidence, never reused.
        return
    if event.operation not in {"backup", "publish"} or not event.entries:
        raise MigrationError("unsupported recovery operation")
    at_source = _safe(source) is not None
    at_destination = _safe(destination) is not None
    if at_source:
        _verify(source, event.entries)
    if at_destination:
        _verify(destination, _relocate(event.entries, source, destination))
    if event.operation == "publish":
        if at_source and not at_destination:
            return
        if not at_source and at_destination:
            operations.move(
                destination,
                source,
                "unpublish",
                _relocate(event.entries, source, destination),
            )
            _verify(source, event.entries)
            return
        raise MigrationError("ambiguous publication recovery")
    if at_source:
        return
    if not at_destination:
        raise MigrationError("missing original and backup")
    # Restore via private sibling, retaining the renamed backup as evidence.
    stage = destination.with_name(destination.name + ".restore")
    staged_entries = _relocate(event.entries, source, stage)
    if _safe(stage) is None:
        for entry in sorted(
            event.entries,
            key=lambda item: (len(item.path.parts), str(item.path)),
        ):
            relative = entry.path.relative_to(source)
            _write_output(
                _Output(entry, destination / relative),
                stage / relative,
                operations,
            )
    _verify(stage, staged_entries)
    operations.move(stage, source, "restore", staged_entries)
    _verify(source, event.entries)


def _restore_locked(
    path: Path, operations: FileOperations
) -> RestorationResult:
    """Recover persisted state under the caller's lease and shutdown proof."""
    try:
        state = load_incomplete_journal(path.with_name("journal.jsonl"))
        if state.manual_recovery or state.manifest is None:
            raise MigrationError(
                "recovery metadata requires manual intervention"
            )
        plans = [event for event in state.events if event.phase == "planned"]
        phases = {
            event.operation_id: event.phase
            for event in state.events
            if event.operation_id
        }
        forward_completed = {
            event.operation_id
            for event in state.events
            if event.phase == "after"
        }
        _verify_completed_reversals(plans, phases, forward_completed)
        if state.events[-1].phase == "restored":
            _verify_restored_inputs(state.manifest)
            return RestorationResult(RestorationStatus.NOT_NEEDED, path)
        journal = MigrationJournal.open(
            path.with_name("journal.jsonl"), file_sync=operations.sync_file
        )
        for event in reversed(plans):
            phase = phases[event.operation_id]
            if phase in {"planned", "rollback_after"}:
                continue
            if phase != "rollback_before":
                operations.record(
                    journal,
                    JournalEvent(event.operation_id, "rollback_before"),
                )
            _reverse(event, operations)
            operations.record(
                journal, JournalEvent(event.operation_id, "rollback_after")
            )
            phases[event.operation_id] = "rollback_after"
        _verify_completed_reversals(plans, phases, forward_completed)
        _verify_restored_inputs(state.manifest)
        operations.record(journal, JournalEvent("", "restored"))
        return RestorationResult(RestorationStatus.COMPLETE, path)
    except Exception:
        return RestorationResult(
            RestorationStatus.INCOMPLETE,
            path,
            "restoration incomplete; retain artifacts and block restart; "
            "manual recovery required",
        )


def _verify_completed_reversals(
    plans: list[JournalEvent],
    phases: dict[str, str],
    forward_completed: set[str],
) -> None:
    """Check recorded reverse outcomes without repairing any external drift."""
    for event in plans:
        if (
            phases[event.operation_id] != "rollback_after"
            or event.operation == "stage"
        ):
            continue
        source, destination = event.source, event.destination
        if source is None or destination is None or not event.entries:
            raise MigrationError("invalid completed recovery operation")
        _verify(source, event.entries)
        if event.operation == "publish":
            _absent(destination)
        elif event.operation == "backup":
            if _safe(destination) is not None:
                _verify(
                    destination, _relocate(event.entries, source, destination)
                )
            elif event.operation_id in forward_completed:
                raise MigrationError("retained backup missing")
        else:
            raise MigrationError("unsupported completed recovery operation")


def _verify_restored_inputs(manifest: Manifest) -> None:
    """Prove all originals exist and canonical publication remains absent."""
    for action in manifest.actions:
        entries = tuple(
            entry
            for entry in manifest.entries
            if entry.path == action.source
            or action.source in entry.path.parents
        )
        _verify(action.source, entries)
        canonical = (
            action.destination.parent
            if action.scope in {"config", "baseline"}
            else action.destination
        )
        _absent(canonical)


def restore_migration(
    manifest_path: Path,
    *,
    operations: FileOperations = REAL_FILE_OPERATIONS,
    lease: WriterLease | None = None,
) -> RestorationResult:
    """Restore persisted operations in reverse using fresh shutdown authority.

    Args:
        manifest_path: Manifest inside project/.codereeve-migration/ID.
        operations: Storage and coordinator boundary, defaulting to refusal.
        lease: Optional caller-owned project lease, never released here.

    Returns:
        COMPLETE, NOT_NEEDED, or INCOMPLETE coordinator evidence.
        Backups and private stages remain available; no service is controlled.
        Both success statuses revalidate current restoration invariants;
        a terminal journal alone is never evidence that restart is safe.
    """
    path = manifest_path.absolute()
    try:
        if (
            path.name != "manifest.json"
            or path.parent.parent.name != ".codereeve-migration"
        ):
            raise MigrationError("invalid transaction location")
        project = path.parent.parent.parent
        operations.sync_directory(project)
        with WriterLease.hold(
            project / ".codereeve-migration.lock",
            purpose="restoration",
            lease=lease,
        ) as lease:
            _quiescent(operations, project, lease)
            return _restore_locked(path, operations)
    except Exception:
        return RestorationResult(
            RestorationStatus.INCOMPLETE,
            path,
            "restoration blocked; fresh shutdown and durability required",
        )
