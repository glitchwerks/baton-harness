"""Real filesystem transaction, failure, and recovery contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from codereeve.config_env import EnvLayer
from codereeve.migration import transaction as transaction_mod
from codereeve.migration.inventory import inventory_migration
from codereeve.migration.journal import (
    JournalEvent,
    MigrationJournal,
    load_incomplete_journal,
    verify_manifest,
)
from codereeve.migration.lease import WriterLease, probe_writer_lease
from codereeve.migration.model import (
    EvidenceState,
    MigrationContext,
    MigrationEvidence,
)
from codereeve.migration.transaction import (
    FileOperations,
    MigrationError,
    RestorationStatus,
    apply_migration,
    restore_migration,
    verify_staged_migration,
)
from codereeve.paths import PathLayout


def put(path: Path, content: bytes) -> None:
    """Create isolated fixture bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def context(tmp_path: Path) -> MigrationContext:
    """Supply all legacy managed inputs and isolated host/system roots."""
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {},
        home=tmp_path / "home",
        etc_root=tmp_path / "etc",
    )
    put(
        layout.legacy_config,
        (
            b"# retained\r\nBH_REPO_OWNER=\r\n"
            b"BH_RUNLOG_PATH=.baton-harness/runlog.jsonl\r\n"
            b"BH_HEARTBEAT_FILE=/custom/heartbeat # custom\r\n"
            b"GH_TOKEN=TOP_SECRET\r\n"
        ),
    )
    put(layout.legacy_config.parent / "ruleset-baseline.json", b"{}\n")
    put(layout.legacy_state / "runlog.jsonl", b'{"event":1}\n')
    put(layout.legacy_state / "heartbeat", b"alive\n")
    (layout.legacy_state / "empty").mkdir()
    return MigrationContext(layout)


class PortableOperations(FileOperations):
    """Inject only unavailable durability and explicit coordinator evidence."""

    def __init__(self, *, check_managed: bool = True) -> None:
        """Record publication seams and lease-protected boundaries."""
        self.trace: list[tuple[str, str, Path]] = []
        self.project: Path | None = None
        self.check_managed = check_managed

    def verify_quiescence(
        self,
        project: Path,
        lease: WriterLease,
    ) -> MigrationEvidence:
        """Attest fixture writers remain stopped under an actual held lock."""
        self.project = project
        assert probe_writer_lease(lease.path) is EvidenceState.BLOCKED
        return MigrationEvidence(
            writers=EvidenceState.CLEAR,
            service=EvidenceState.CLEAR,
        )

    def sync_directory(self, path: Path) -> None:
        """Model a supported directory flush; no Windows durability claim."""
        self.boundary("directory_fsync", "before", path)
        self.boundary("directory_fsync", "after", path)

    def boundary(self, name: str, phase: str, path: Path) -> None:
        """Observe complete publication and retained lease at every seam."""
        self.trace.append((name, phase, path))
        if self.project is not None:
            assert (
                probe_writer_lease(self.project / ".codereeve-migration.lock")
                is EvidenceState.BLOCKED
            )
        if (
            self.check_managed
            and name == "publish"
            and path.name == ".codereeve"
        ):
            if phase == "before":
                assert not path.exists()
            else:
                assert {p.name for p in path.iterdir()} == {
                    "config.env",
                    "ruleset-baseline.json",
                    "runlog.jsonl",
                    "heartbeat",
                    "empty",
                }


def test_managed_transaction_publishes_complete_tree_and_retains_evidence(
    context: MigrationContext,
) -> None:
    """Partial managed publication or lossy rewriting breaks this test."""
    original = context.layout.legacy_config.read_bytes()
    operations = PortableOperations()
    result = apply_migration(context, operations=operations)
    canonical = context.layout.canonical_state
    assert canonical.joinpath("config.env").read_text() == (
        '# retained\nCODEREEVE_REPO_OWNER=""\n'
        "CODEREEVE_RUNLOG_PATH=.codereeve/runlog.jsonl\n"
        "CODEREEVE_HEARTBEAT_FILE=/custom/heartbeat # custom\n"
        "GH_TOKEN=TOP_SECRET\n"
    )
    assert not context.layout.legacy_config.exists()
    assert not context.layout.legacy_state.exists()
    assert any(
        p.is_file() and p.read_bytes() == original for p in result.backups
    )
    assert len(result.backups) == 3
    assert all(p.exists() for p in result.backups)
    state = load_incomplete_journal(
        result.manifest_path.with_name("journal.jsonl")
    )
    assert state.terminal and state.events[-1].phase == "complete"
    metadata = (
        result.manifest_path.read_text()
        + result.manifest_path.with_name("journal.jsonl").read_text()
    )
    assert "TOP_SECRET" not in metadata
    assert (
        probe_writer_lease(canonical.parent / ".codereeve-migration.lock")
        is EvidenceState.CLEAR
    )
    assert (
        sum(
            name == "publish" and phase == "after"
            for name, phase, _ in operations.trace
        )
        == 1
    )


@pytest.mark.parametrize("existing_parent", [False, True])
def test_host_and_secrets_preserve_confidentiality_and_stage_locally(
    context: MigrationContext,
    existing_parent: bool,
) -> None:
    """External configuration stages and backups stay on their filesystems."""
    layout = context.layout
    for source in (layout.legacy_host, layout.legacy_secrets):
        put(source, b"BWS_ACCESS_TOKEN=TOP_SECRET\nBH_REPO_NAME=demo\n")
        source.chmod(0o600)
    if existing_parent:
        for destination in (layout.canonical_host, layout.canonical_secrets):
            put(destination.parent / "unrelated", b"keep")
    operations = PortableOperations()
    result = apply_migration(context, operations=operations)
    for destination in (layout.canonical_host, layout.canonical_secrets):
        assert (
            destination.read_text()
            == "BWS_ACCESS_TOKEN=TOP_SECRET\nCODEREEVE_REPO_NAME=demo\n"
        )
        if os.name != "nt":
            assert destination.stat().st_mode & 0o777 == 0o600
        if existing_parent:
            assert (destination.parent / "unrelated").read_bytes() == b"keep"
    assert len(result.backups) == 5
    state = load_incomplete_journal(
        result.manifest_path.with_name("journal.jsonl")
    )
    for event in state.events:
        if event.operation == "publish":
            assert event.source.parent == event.destination.parent


@pytest.mark.parametrize(
    "problem", ["canonical", "collision", "aliases", "journal"]
)
def test_readonly_blockers_precede_even_lock_creation(
    context: MigrationContext,
    problem: str,
) -> None:
    """A rejected inventory cannot create transaction or lock artifacts."""
    layout = context.layout
    if problem == "canonical":
        layout.canonical_state.mkdir()
    elif problem == "collision":
        put(layout.legacy_state / "config.env", b"BH_REPO_OWNER=\n")
    elif problem == "aliases":
        context = replace(
            context,
            layers=(
                EnvLayer("operator", {"CODEREEVE_REPO_OWNER": "different"}),
            ),
        )
    else:
        put(
            layout.canonical_state.parent
            / ".codereeve-migration"
            / "bad"
            / "manifest.json",
            b"{}",
        )
    before = layout.legacy_config.read_bytes()
    with pytest.raises(MigrationError):
        apply_migration(context, operations=PortableOperations())
    assert layout.legacy_config.read_bytes() == before
    assert not (
        layout.canonical_state.parent / ".codereeve-migration.lock"
    ).exists()


def test_snapshot_evidence_does_not_authorize_unmanaged_legacy(
    context: MigrationContext,
) -> None:
    """Old CLEAR snapshots cannot substitute for fresh authority."""
    context = replace(
        context,
        evidence=MigrationEvidence(
            writers=EvidenceState.CLEAR,
            service=EvidenceState.CLEAR,
        ),
    )
    with pytest.raises(MigrationError):
        apply_migration(context)
    assert context.layout.legacy_config.exists()
    assert not context.layout.canonical_state.exists()


def test_active_lease_blocks_apply(context: MigrationContext) -> None:
    """A concurrent cooperative writer prevents any source mutation."""
    path = context.layout.canonical_state.parent / ".codereeve-migration.lock"
    with WriterLease.acquire(path, purpose="test"):
        with pytest.raises(MigrationError):
            apply_migration(context, operations=PortableOperations())
    assert context.layout.legacy_config.exists()


BOUNDARIES = [
    "copy",
    "rewrite",
    "verify",
    "backup",
    "publish",
    "file_fsync",
    "directory_fsync",
    "journal_record",
    "final_verify",
]


@pytest.mark.parametrize("name", BOUNDARIES)
@pytest.mark.parametrize("phase", ["before", "after"])
def test_caught_failure_reverses_without_losing_originals(
    context: MigrationContext,
    name: str,
    phase: str,
) -> None:
    """Every injected storage boundary must restore or report incomplete."""
    original = context.layout.legacy_config.read_bytes()

    class FailingOperations(PortableOperations):
        """Raise once so real reverse restoration can execute."""

        fired = False

        def boundary(self, current: str, when: str, path: Path) -> None:
            """Fail at the requested real boundary."""
            super().boundary(current, when, path)
            if (current, when) == (name, phase) and not self.fired:
                self.fired = True
                raise OSError("TOP_SECRET")

    operations = FailingOperations()
    with pytest.raises(MigrationError) as failure:
        apply_migration(context, operations=operations)
    assert operations.fired
    assert "TOP_SECRET" not in str(failure.value)
    assert context.layout.legacy_config.read_bytes() == original
    assert (
        context.layout.legacy_state / "heartbeat"
    ).read_bytes() == b"alive\n"
    assert not context.layout.canonical_state.exists()


def test_restore_is_idempotent_retains_backups_and_refuses_stale_evidence(
    context: MigrationContext,
) -> None:
    """Restoration needs fresh quiescence and preserves backup bytes."""
    original = context.layout.legacy_config.read_bytes()
    result = apply_migration(context, operations=PortableOperations())
    refused = restore_migration(result.manifest_path)
    assert refused.status is RestorationStatus.INCOMPLETE
    restored = restore_migration(
        result.manifest_path, operations=PortableOperations()
    )
    assert restored.status is RestorationStatus.COMPLETE
    assert context.layout.legacy_config.read_bytes() == original
    assert not context.layout.canonical_state.exists()
    assert all(path.exists() for path in result.backups)
    again = restore_migration(
        result.manifest_path, operations=PortableOperations()
    )
    assert again.status is RestorationStatus.NOT_NEEDED


def test_restore_refuses_externally_changed_destination(
    context: MigrationContext,
) -> None:
    """External canonical edits cannot be overwritten during recovery."""
    result = apply_migration(context, operations=PortableOperations())
    put(context.layout.canonical_state / "heartbeat", b"changed")
    restored = restore_migration(
        result.manifest_path, operations=PortableOperations()
    )
    assert restored.status is RestorationStatus.INCOMPLETE
    assert (
        context.layout.canonical_state / "heartbeat"
    ).read_bytes() == b"changed"
    assert all(path.exists() for path in result.backups)


def test_inventory_can_inspect_metadata_under_actual_writer_lock(
    context: MigrationContext,
) -> None:
    """Windows lock bytes are not read as data while a lease is retained."""
    path = context.layout.canonical_state.parent / ".codereeve-migration.lock"
    with WriterLease.acquire(path, purpose="test"):
        report = inventory_migration(context)
    assert not any(
        f.code == "unreadable_path" and f.scope == "lease"
        for f in report.findings
    )
    assert any(f.code == "lease_state_unverified" for f in report.findings)


def test_backup_boundary_rechecks_source_after_external_edit(
    context: MigrationContext,
) -> None:
    """A last-moment external edit must never be moved into our backup."""
    source = context.layout.legacy_config.parent / "ruleset-baseline.json"

    class ChangedInput(PortableOperations):
        """Change a source just before its OS rename boundary."""

        def boundary(self, name: str, phase: str, path: Path) -> None:
            """Inject an uncooperative writer after earlier validation."""
            super().boundary(name, phase, path)
            if name == "backup" and phase == "before":
                source.write_bytes(b"external-change")

    with pytest.raises(MigrationError):
        apply_migration(context, operations=ChangedInput())
    assert source.read_bytes() == b"external-change"
    assert not list(source.parent.glob(source.name + ".codereeve-backup-*"))


@pytest.mark.parametrize("prefix", range(17))
def test_process_death_at_each_durable_prefix_recovers_fresh(
    context: MigrationContext,
    prefix: int,
) -> None:
    """Fresh recovery covers created, planned, uncertain and after records."""
    original = context.layout.legacy_config.read_bytes()
    base = context.layout.canonical_state.parent.parent
    script = """
import os
import sys
from pathlib import Path
from codereeve.paths import PathLayout
from codereeve.migration.model import MigrationContext
from codereeve.migration.transaction import apply_migration
from tests.test_migration_transaction import PortableOperations
class Crash(PortableOperations):
    count = -1
    def boundary(self, name, phase, path):
        super().boundary(name, phase, path)
        boundaries = {("journal_create", "after"), ("journal_record", "after")}
        if (name, phase) in boundaries:
            self.count += 1
            if self.count == int(sys.argv[2]):
                os._exit(73)
base = Path(sys.argv[1])
layout = PathLayout.for_environment(
    base / "project", {}, home=base / "home", etc_root=base / "etc"
)
apply_migration(MigrationContext(layout), operations=Crash())
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(base), str(prefix)],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr.decode()
    manifest = next(
        (base / "project" / ".codereeve-migration").glob("*/manifest.json")
    )
    restored = restore_migration(manifest, operations=PortableOperations())
    assert restored.status is RestorationStatus.COMPLETE
    assert context.layout.legacy_config.read_bytes() == original
    assert (
        context.layout.legacy_state / "heartbeat"
    ).read_bytes() == b"alive\n"
    assert not context.layout.canonical_state.exists()
    assert (
        restore_migration(manifest, operations=PortableOperations()).status
        is RestorationStatus.NOT_NEEDED
    )


@pytest.mark.parametrize("field", ["writers", "service"])
def test_fresh_unknown_shutdown_evidence_blocks(
    context: MigrationContext,
    field: str,
) -> None:
    """Both writers and service must be freshly verified, independently."""

    class Unknown(PortableOperations):
        """Return one unknown component from the live coordinator."""

        def verify_quiescence(
            self, project: Path, lease: WriterLease
        ) -> MigrationEvidence:
            """Keep actual lease validation but withhold shutdown proof."""
            return replace(
                super().verify_quiescence(project, lease),
                **{field: EvidenceState.UNKNOWN},
            )

    with pytest.raises(MigrationError):
        apply_migration(context, operations=Unknown())
    assert context.layout.legacy_config.exists()
    assert not context.layout.canonical_state.exists()


def test_real_windows_durability_blocks_before_data_or_lock_mutation(
    context: MigrationContext,
) -> None:
    """Windows stdlib cannot pretend to supply directory crash durability."""
    if os.name != "nt":
        return

    class RealDurability(PortableOperations):
        """Keep only coordinator evidence injected."""

        sync_directory = FileOperations.sync_directory

    with pytest.raises(MigrationError):
        apply_migration(context, operations=RealDurability())
    project = context.layout.canonical_state.parent
    assert not (project / ".codereeve-migration.lock").exists()
    assert not (project / ".codereeve-migration").exists()


def test_cross_device_report_blocks_before_backup(
    context: MigrationContext,
) -> None:
    """A storage capability reporting another device cannot publish."""

    class CrossDevice(PortableOperations):
        """Return inconsistent stage/destination device evidence."""

        calls = 0

        def device(self, path: Path) -> int:
            """Represent a mount change between device checks."""
            self.calls += 1
            return self.calls

    with pytest.raises(MigrationError):
        apply_migration(context, operations=CrossDevice())
    assert context.layout.legacy_state.exists()
    assert context.layout.legacy_config.exists()
    assert not context.layout.canonical_state.exists()


def test_state_config_cannot_publish_dependency_on_legacy_aliases(
    tmp_path: Path,
) -> None:
    """A state-tree config must also pass compatibility-disabled validation."""
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {},
        home=tmp_path / "home",
        etc_root=tmp_path / "etc",
    )
    put(layout.legacy_state / "config.env", b"BH_REPO_OWNER=legacy\n")
    with pytest.raises(MigrationError):
        apply_migration(
            MigrationContext(layout),
            operations=PortableOperations(check_managed=False),
        )
    assert (
        layout.legacy_state.joinpath("config.env").read_bytes()
        == b"BH_REPO_OWNER=legacy\n"
    )
    assert not layout.canonical_state.exists()


def test_every_observed_boundary_reverses_caught_failure(
    tmp_path: Path,
) -> None:
    """Exercise every occurrence, including every backup and durable append."""
    baseline = context.__wrapped__(tmp_path / "baseline")
    trace = PortableOperations()
    apply_migration(baseline, operations=trace)
    for index in range(len(trace.trace)):
        case = context.__wrapped__(tmp_path / str(index))
        original = case.layout.legacy_config.read_bytes()

        class FailureAtIndex(PortableOperations):
            """Fail once at the exact observed operation occurrence."""

            fired = False
            failure_index = index

            def boundary(self, name: str, phase: str, path: Path) -> None:
                """Inject a boundary failure while retaining real storage."""
                super().boundary(name, phase, path)
                if (
                    len(self.trace) - 1 == self.failure_index
                    and not self.fired
                ):
                    self.fired = True
                    raise OSError("TOP_SECRET")

        operations = FailureAtIndex()
        with pytest.raises(MigrationError) as failure:
            apply_migration(case, operations=operations)
        assert operations.fired, index
        assert "TOP_SECRET" not in str(failure.value)
        assert case.layout.legacy_config.read_bytes() == original, index
        assert case.layout.legacy_state.is_dir(), index
        assert not case.layout.canonical_state.exists(), index


def test_state_copy_streams_instead_of_reading_whole_runtime_log(
    context: MigrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbounded runlog size must not determine copy memory usage."""
    original_read = transaction_mod._read
    original_fdopen = os.fdopen
    original_info = (context.layout.legacy_state / "runlog.jsonl").stat()

    class BoundedReader:
        """Assert bounded runtime reads at the actual descriptor boundary."""

        def __init__(self, stream: object) -> None:
            """Retain the actual open input stream."""
            self.stream = stream

        def __enter__(self) -> BoundedReader:
            """Enter the wrapped stream."""
            self.stream.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            """Close the actual stream through its context protocol."""
            self.stream.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            """Reject any unbounded read including manifest snapshots."""
            assert 0 <= size <= 1024 * 1024
            return self.stream.read(size)

    def bounded_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        """Keep real descriptor reads and enforce the runtime-log limit."""
        info = os.fstat(fd)
        stream = original_fdopen(fd, *args, **kwargs)
        if (info.st_dev, info.st_ino) == (
            original_info.st_dev,
            original_info.st_ino,
        ):
            return BoundedReader(stream)
        return stream

    def config_only(path: Path) -> bytes:
        """Reject whole-file reads of arbitrary runtime data."""
        if path.name == "runlog.jsonl":
            raise OSError("whole runtime log read")
        return original_read(path)

    monkeypatch.setattr(transaction_mod, "_read", config_only)
    monkeypatch.setattr(os, "fdopen", bounded_fdopen)
    apply_migration(context, operations=PortableOperations())
    assert (
        context.layout.canonical_state / "runlog.jsonl"
    ).read_bytes() == b'{"event":1}\n'


def test_unrelated_default_value_does_not_change_product_path_rewrite(
    context: MigrationContext,
) -> None:
    """A third-party value cannot override product default recognition."""
    path = context.layout.legacy_config
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "BH_REDISPATCH_COUNTS_PATH=.baton-harness/dispatch-counts.json\n"
        )
        unrelated = context.layout.legacy_state / "runlog.jsonl"
        stream.write(f"THIRD_PARTY_PATH={unrelated}\n")
    apply_migration(context, operations=PortableOperations())
    assert (
        "CODEREEVE_RUNLOG_PATH=.codereeve/runlog.jsonl"
        in context.layout.canonical_config.read_text()
    )
    assert (
        "CODEREEVE_REDISPATCH_COUNTS_PATH=.codereeve/dispatch-counts.json"
        in context.layout.canonical_config.read_text()
    )


@pytest.mark.parametrize("prefix", range(11))
def test_process_death_during_reverse_recovery_resumes(
    context: MigrationContext,
    prefix: int,
) -> None:
    """Repeated recovery resumes rollback-before and rollback-after records."""
    original = context.layout.legacy_config.read_bytes()
    result = apply_migration(context, operations=PortableOperations())
    script = """
import os
import sys
from pathlib import Path
from codereeve.migration.transaction import restore_migration
from tests.test_migration_transaction import PortableOperations
class Crash(PortableOperations):
    count = -1
    def boundary(self, name, phase, path):
        super().boundary(name, phase, path)
        if (name, phase) == ("journal_record", "after"):
            self.count += 1
            if self.count == int(sys.argv[2]):
                os._exit(73)
restore_migration(Path(sys.argv[1]), operations=Crash())
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(result.manifest_path), str(prefix)],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr.decode()
    restored = restore_migration(
        result.manifest_path, operations=PortableOperations()
    )
    assert restored.status in {
        RestorationStatus.COMPLETE,
        RestorationStatus.NOT_NEEDED,
    }
    assert context.layout.legacy_config.read_bytes() == original
    assert (
        context.layout.legacy_state.joinpath("heartbeat").read_bytes()
        == b"alive\n"
    )
    assert all(path.exists() for path in result.backups)


def test_host_only_apply_and_restore(tmp_path: Path) -> None:
    """Host migration remains project-bound without managed data."""
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {},
        home=tmp_path / "home",
        etc_root=tmp_path / "etc",
    )
    layout.canonical_state.parent.mkdir()
    put(layout.legacy_host, b"# host only\n")
    result = apply_migration(
        MigrationContext(layout),
        operations=PortableOperations(check_managed=False),
    )
    assert layout.canonical_host.read_bytes() == b"# host only\n"
    assert not layout.canonical_state.exists()
    assert (
        restore_migration(
            result.manifest_path, operations=PortableOperations()
        ).status
        is RestorationStatus.COMPLETE
    )
    assert layout.legacy_host.read_bytes() == b"# host only\n"


@pytest.mark.parametrize("scope", ["host", "secrets"])
@pytest.mark.parametrize("name", ["backup", "publish"])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_external_scope_failure_reverses_all_prior_publications(
    context: MigrationContext,
    scope: str,
    name: str,
    phase: str,
) -> None:
    """Failures on later filesystems must also unpublish the managed tree."""
    layout = context.layout
    original = b"GH_TOKEN=TOP_SECRET\nBH_REPO_NAME=demo\n"
    for path in (layout.legacy_host, layout.legacy_secrets):
        put(path, original)
    target = getattr(layout, "canonical_" + scope).parent
    original_source = getattr(layout, "legacy_" + scope)

    class Failure(PortableOperations):
        """Fail at one external input's real rename boundary."""

        fired = False

        def boundary(self, current: str, when: str, path: Path) -> None:
            """Select external publication or its sibling backup."""
            super().boundary(current, when, path)
            matched = (
                path == target
                if name == "publish"
                else (
                    path.parent == original_source.parent
                    and path.name.startswith(
                        original_source.name + ".codereeve-backup-"
                    )
                )
            )
            if matched and (current, when) == (name, phase) and not self.fired:
                self.fired = True
                raise OSError("TOP_SECRET")

    operations = Failure()
    with pytest.raises(MigrationError):
        apply_migration(context, operations=operations)
    assert operations.fired
    assert layout.legacy_host.read_bytes() == original
    assert layout.legacy_secrets.read_bytes() == original
    assert layout.legacy_config.exists() and layout.legacy_state.exists()
    assert not layout.canonical_state.exists()
    assert not layout.canonical_host.exists()
    assert not layout.canonical_secrets.exists()


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    "drift", ["missing", "changed", "canonical", "backup"]
)
def test_completed_reverse_outcomes_are_revalidated_before_success(
    context: MigrationContext,
    terminal: bool,
    drift: str,
) -> None:
    """Durable rollback records never substitute for current restored bytes."""
    result = apply_migration(context, operations=PortableOperations())
    state_source = context.layout.legacy_state

    class InterruptedRecovery(PortableOperations):
        """Stop after persisting the state input's reverse completion."""

        def record(
            self, journal: MigrationJournal, event: JournalEvent
        ) -> None:
            """Interrupt only after the requested operation really persists."""
            super().record(journal, event)
            state = load_incomplete_journal(journal.path)
            plan = next(
                (
                    row
                    for row in state.events
                    if row.phase == "planned"
                    and row.operation_id == event.operation_id
                ),
                None,
            )
            if (
                event.phase == "rollback_after"
                and plan is not None
                and plan.operation == "backup"
                and plan.source == state_source
            ):
                raise OSError("interrupted after restored state")

    initial = restore_migration(
        result.manifest_path,
        operations=PortableOperations() if terminal else InterruptedRecovery(),
    )
    assert initial.status is (
        RestorationStatus.COMPLETE
        if terminal
        else RestorationStatus.INCOMPLETE
    )
    state_backup = next(
        path
        for path in result.backups
        if path.name.startswith(".baton-harness.")
    )
    if drift == "missing":
        state_source.rename(state_source.with_name("externally-moved-state"))
    elif drift == "changed":
        (state_source / "heartbeat").write_bytes(b"external bytes")
    elif drift == "canonical":
        put(context.layout.canonical_state / "external", b"external bytes")
    else:
        (state_backup / "heartbeat").write_bytes(b"external backup bytes")
    journal_path = result.manifest_path.with_name("journal.jsonl")
    before = journal_path.read_bytes()
    resumed = restore_migration(
        result.manifest_path, operations=PortableOperations()
    )
    assert resumed.status is RestorationStatus.INCOMPLETE
    assert journal_path.read_bytes() == before
    if drift == "missing":
        assert not state_source.exists()
    elif drift == "changed":
        assert (state_source / "heartbeat").read_bytes() == b"external bytes"
    elif drift == "canonical":
        assert (
            context.layout.canonical_state / "external"
        ).read_bytes() == b"external bytes"
    else:
        assert (
            state_backup / "heartbeat"
        ).read_bytes() == b"external backup bytes"


def test_public_staged_verifier_accepts_nested_only_config(
    tmp_path: Path,
) -> None:
    """A nested config basename must not imply a managed-root config exists."""
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {},
        home=tmp_path / "home",
        etc_root=tmp_path / "etc",
    )
    put(
        layout.legacy_state / "nested" / "config.env", b"BH_CUSTOM=preserved\n"
    )

    class PublicVerifier(PortableOperations):
        """Use the exported verifier before original inputs are moved."""

        def boundary(self, name: str, phase: str, path: Path) -> None:
            """Verify the same real stage apply just verified internally."""
            super().boundary(name, phase, path)
            if (name, phase) == ("verify", "after"):
                manifest = next(
                    (
                        layout.canonical_state.parent / ".codereeve-migration"
                    ).glob("*/manifest.json")
                )
                verify_staged_migration(path, verify_manifest(manifest))

    apply_migration(
        MigrationContext(layout),
        operations=PublicVerifier(check_managed=False),
    )
    assert (
        layout.canonical_state / "nested" / "config.env"
    ).read_bytes() == b"BH_CUSTOM=preserved\n"
    assert not layout.canonical_config.exists()


def test_public_staged_verifier_sanitizes_source_read_errors(
    context: MigrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exported verifier reports storage faults as safe domain errors."""

    class PublicVerifier(PortableOperations):
        """Inject an unavailable original while the public helper reads it."""

        def boundary(self, name: str, phase: str, path: Path) -> None:
            """Invoke public verification against real staged output."""
            super().boundary(name, phase, path)
            if (name, phase) != ("verify", "after"):
                return
            manifest = next(
                (
                    context.layout.canonical_state.parent
                    / ".codereeve-migration"
                ).glob("*/manifest.json")
            )
            validated = verify_manifest(manifest)

            def unavailable(source: Path) -> bytes:
                """Return an OS error that must never cross the public API."""
                raise OSError("TOP_SECRET")

            with monkeypatch.context() as patcher:
                patcher.setattr(transaction_mod, "_read", unavailable)
                with pytest.raises(MigrationError) as failure:
                    verify_staged_migration(path, validated)
                assert "TOP_SECRET" not in str(failure.value)

    apply_migration(context, operations=PublicVerifier())
    assert context.layout.canonical_config.exists()
