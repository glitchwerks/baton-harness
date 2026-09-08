"""Durable metadata-only transaction records and strict recovery evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from codereeve.migration import journal as journal_mod
from codereeve.migration.journal import (
    JournalError,
    JournalEvent,
    MigrationJournal,
    load_incomplete_journal,
    probe_journals,
    verify_manifest,
)
from codereeve.migration.model import (
    EvidenceState,
    MigrationAction,
    MigrationReport,
    MigrationStatus,
)


@pytest.fixture
def report(tmp_path: Path) -> MigrationReport:
    """Create a real confidential input and a metadata-only plan."""
    source = tmp_path / "legacy.env"
    source.write_text("GH_TOKEN=TOP_SECRET\n", encoding="utf-8")
    return MigrationReport(
        MigrationStatus.READY,
        (
            MigrationAction(
                "migrate_config", "project", source, tmp_path / "config.env"
            ),
        ),
    )


@pytest.fixture
def portable_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the explicit directory-durability capability on Windows."""
    monkeypatch.setattr(journal_mod, "fsync_directory", lambda path: None)


def test_journal_events_reopen_and_contain_metadata_only(
    tmp_path: Path, report: MigrationReport, portable_fsync: None
) -> None:
    """Each event is durable and recoverable with no source contents."""
    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )
    manifest = verify_manifest(journal.manifest_path)
    assert manifest.entries[0].sha256
    assert manifest.entries[0].mode
    planned = JournalEvent(
        "op1",
        "planned",
        "backup",
        report.actions[0].source,
        tmp_path / "backup",
    )
    for event in (
        planned,
        JournalEvent("op1", "before"),
        JournalEvent("op1", "after"),
        JournalEvent("", "complete"),
    ):
        journal.record(event)
        raw = journal.path.read_text(encoding="utf-8")
        assert raw.endswith("\n")
        assert all(
            isinstance(json.loads(line), dict) for line in raw.splitlines()
        )
        state = load_incomplete_journal(journal.path)
        assert not state.manual_recovery
        assert state.events[-1] == event
    assert state.terminal
    assert "TOP_SECRET" not in raw + journal.manifest_path.read_text()
    evidence = probe_journals(tmp_path / "transactions")
    assert evidence.journals is EvidenceState.CLEAR
    assert evidence.writers is EvidenceState.UNKNOWN
    assert evidence.verified_transactions == (journal.path.parent,)


@pytest.mark.parametrize(
    "damage", ["partial", "checksum", "extra_field", "empty"]
)
def test_corruption_blocks_recovery_and_evidence(
    tmp_path: Path, report: MigrationReport, portable_fsync: None, damage: str
) -> None:
    """No truncated, changed, unknown-schema, or empty journal is terminal."""
    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )
    if damage == "partial":
        journal.path.write_bytes(journal.path.read_bytes() + b'{"phase":')
    elif damage == "empty":
        journal.path.write_bytes(b"")
    else:
        rows = journal.path.read_text().splitlines()
        row = json.loads(rows[0])
        if damage == "checksum":
            row["checksum"] = "0" * 64
        else:
            row["unknown"] = "TOP_SECRET"
        journal.path.write_text(json.dumps(row) + "\n")
    state = load_incomplete_journal(journal.path)
    assert state.manual_recovery
    assert not state.terminal
    assert "TOP_SECRET" not in state.diagnostic
    assert (
        probe_journals(tmp_path / "transactions").journals
        is EvidenceState.BLOCKED
    )
    with pytest.raises(JournalError):
        MigrationJournal.open(journal.path)


def test_transition_validation_and_reverse_recovery(
    tmp_path: Path, report: MigrationReport, portable_fsync: None
) -> None:
    """Invalid order cannot be persisted; uncertain before can roll back."""
    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )
    with pytest.raises(JournalError):
        journal.record(JournalEvent("missing", "after"))
    journal.record(
        JournalEvent(
            "op1",
            "planned",
            "publish",
            report.actions[0].source,
            tmp_path / "new",
        )
    )
    journal.record(JournalEvent("op1", "before"))
    with pytest.raises(JournalError):
        journal.record(JournalEvent("", "complete"))
    reopened = MigrationJournal.open(journal.path)
    reopened.record(JournalEvent("op1", "rollback_before"))
    reopened.record(JournalEvent("op1", "rollback_after"))
    reopened.record(JournalEvent("", "restored"))
    assert load_incomplete_journal(journal.path).terminal


@pytest.mark.parametrize("seam", ["_append", "_fsync_file", "fsync_directory"])
def test_durability_failure_never_claims_success(
    tmp_path: Path,
    report: MigrationReport,
    portable_fsync: None,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    """Write and either flush failure propagate without exception values."""

    def fail(*args: object) -> None:
        """Simulate a platform/storage failure containing secret detail."""
        raise OSError("TOP_SECRET")

    monkeypatch.setattr(journal_mod, seam, fail)
    with pytest.raises(JournalError) as failure:
        MigrationJournal.create(
            tmp_path / "transactions", report, datetime.now(timezone.utc)
        )
    assert "TOP_SECRET" not in str(failure.value)


def test_manifest_checksum_and_permissions_fail_closed(
    tmp_path: Path,
    report: MigrationReport,
    portable_fsync: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed manifest or inability to secure artifacts blocks use."""
    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )
    raw = json.loads(journal.manifest_path.read_text())
    raw["checksum"] = "0" * 64
    journal.manifest_path.write_text(json.dumps(raw))
    with pytest.raises(JournalError):
        verify_manifest(journal.manifest_path)
    assert load_incomplete_journal(journal.path).manual_recovery

    def fail(path: Path, mode: int) -> None:
        """Deny private directory creation."""
        raise PermissionError("TOP_SECRET")

    monkeypatch.setattr(journal_mod, "_private_directory", fail)
    with pytest.raises(JournalError):
        MigrationJournal.create(
            tmp_path / "another", report, datetime.now(timezone.utc)
        )


def test_duplicate_terminal_event_rejected(
    tmp_path: Path, report: MigrationReport, portable_fsync: None
) -> None:
    """A complete marker cannot be appended repeatedly to forge progress."""
    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )
    journal.record(
        JournalEvent(
            "op",
            "planned",
            "backup",
            report.actions[0].source,
            tmp_path / "backup",
        )
    )
    journal.record(JournalEvent("op", "before"))
    journal.record(JournalEvent("op", "after"))
    journal.record(JournalEvent("", "complete"))
    with pytest.raises(JournalError):
        journal.record(JournalEvent("", "complete"))


def test_failed_append_poisoned_until_recovery(
    tmp_path: Path,
    report: MigrationReport,
    portable_fsync: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial-append error prevents continued use of a failed writer."""
    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )
    event = JournalEvent(
        "op",
        "planned",
        "backup",
        report.actions[0].source,
        tmp_path / "backup",
    )

    def fail(path: Path, data: bytes) -> None:
        """Report an ambiguous short append."""
        raise JournalError("partial append")

    with monkeypatch.context() as patcher:
        patcher.setattr(journal_mod, "_append", fail)
        with pytest.raises(JournalError):
            journal.record(event)
    with pytest.raises(JournalError):
        journal.record(event)


def test_directory_capability_and_private_artifacts(
    tmp_path: Path, report: MigrationReport
) -> None:
    """Supported platforms secure artifacts; others mutate nothing."""
    import os
    import stat

    root = tmp_path / "transactions"
    if os.name == "nt":
        with pytest.raises(JournalError, match="unsupported"):
            MigrationJournal.create(root, report, datetime.now(timezone.utc))
        assert not root.exists()
    else:
        journal = MigrationJournal.create(
            root, report, datetime.now(timezone.utc)
        )
        assert stat.S_IMODE(journal.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(journal.manifest_path.stat().st_mode) == 0o600


def test_existing_transaction_id_never_overwritten(
    tmp_path: Path,
    report: MigrationReport,
    portable_fsync: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated timestamp and nonce cannot replace persisted evidence."""
    import uuid

    monkeypatch.setattr(journal_mod.uuid, "uuid4", lambda: uuid.UUID(int=1))
    now = datetime.now(timezone.utc)
    journal = MigrationJournal.create(tmp_path / "transactions", report, now)
    before = journal.path.read_bytes()
    with pytest.raises(JournalError):
        MigrationJournal.create(tmp_path / "transactions", report, now)
    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("seam", ["short_write", "file_fsync"])
def test_actual_append_boundary_failures_block_recovery(
    tmp_path: Path,
    report: MigrationReport,
    portable_fsync: None,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    """Short writes corrupt evidence; flush failures poison active writers."""
    import os

    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        """Persist only part of the event to represent interruption."""
        return real_write(fd, data[:5])

    def fail_flush(fd: int) -> None:
        """Report a storage failure after append."""
        raise OSError("TOP_SECRET")

    with monkeypatch.context() as patcher:
        if seam == "short_write":
            patcher.setattr(os, "write", short_write)
        else:
            patcher.setattr(journal_mod, "_fsync_file", fail_flush)
        with pytest.raises(JournalError):
            journal.record(
                JournalEvent(
                    "op",
                    "planned",
                    "backup",
                    report.actions[0].source,
                    tmp_path / "backup",
                )
            )
    with pytest.raises(JournalError):
        journal.record(JournalEvent("op", "before"))
    if seam == "short_write":
        assert load_incomplete_journal(journal.path).manual_recovery


def test_readonly_probe_never_proves_writer_shutdown(
    tmp_path: Path,
    report: MigrationReport,
    portable_fsync: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete metadata blocks; probes cannot authorize legacy writers."""
    journal = MigrationJournal.create(
        tmp_path / "transactions", report, datetime.now(timezone.utc)
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        """Reject probe mutations."""
        raise AssertionError("unexpected mutation")

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "mkdir", forbidden)
        patcher.setattr(Path, "write_text", forbidden)
        patcher.setattr(Path, "write_bytes", forbidden)
        patcher.setattr(journal_mod, "_append", forbidden)
        evidence = probe_journals(journal.path.parent.parent)
    assert evidence.journals is EvidenceState.BLOCKED
    assert evidence.writers is EvidenceState.UNKNOWN
    assert evidence.service is EvidenceState.UNKNOWN
    assert evidence.lease is EvidenceState.UNKNOWN


def test_created_only_recovery_can_finish_without_inventing_operations(
    tmp_path: Path, report: MigrationReport, portable_fsync: None
) -> None:
    """A crash before planning restores as a terminal, mutation-free no-op."""
    root = tmp_path / "transactions"
    journal = MigrationJournal.create(root, report, datetime.now(timezone.utc))
    reopened = MigrationJournal.open(journal.path)
    with pytest.raises(JournalError):
        reopened.record(JournalEvent("", "complete"))
    reopened.record(JournalEvent("", "restored"))
    state = load_incomplete_journal(journal.path)
    assert state.terminal and not state.manual_recovery
    assert [event.phase for event in state.events] == ["created", "restored"]
    evidence = probe_journals(root)
    assert evidence.journals is EvidenceState.CLEAR
    assert evidence.verified_transactions == (journal.path.parent,)


def test_explicit_durability_callbacks_cover_create_and_reopen(
    tmp_path: Path,
    report: MigrationReport,
) -> None:
    """Injected storage durability is retained by both journal writers."""
    import os

    flushed: list[int] = []

    def sync(fd: int) -> None:
        """Flush real files and record the actual descriptor boundary."""
        os.fsync(fd)
        flushed.append(fd)

    journal = MigrationJournal.create(
        tmp_path / "transactions",
        report,
        datetime.now(timezone.utc),
        directory_sync=lambda path: None,
        file_sync=sync,
    )
    assert len(flushed) >= 2
    reopened = MigrationJournal.open(journal.path, file_sync=sync)
    before = len(flushed)
    reopened.record(JournalEvent("", "restored"))
    assert len(flushed) == before + 1
