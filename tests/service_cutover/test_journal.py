"""Closed journal contract tests."""

from pathlib import Path, PurePosixPath

import pytest
from test_storage import portable_storage

from codereeve.service_cutover import journal as module
from codereeve.service_cutover import model
from codereeve.service_cutover.model import (
    CutoverError,
    ServiceSpec,
    UnitState,
)


def test_journal_contract_available() -> None:
    """Require concrete journal creation and reopening."""
    assert hasattr(module, "CutoverJournal")
    assert hasattr(model, "ServiceSnapshot")


def make_journal(
    tmp_path: Path, *, mode: str = "cutover", fresh: bool = False
) -> module.CutoverJournal:
    """Create an isolated journal whose original unit contains a secret."""
    assert hasattr(module, "CutoverJournal")
    assert hasattr(model, "ServiceSnapshot")
    store = portable_storage(tmp_path)
    old = UnitState(
        "bh-daemon.service",
        "loaded",
        "active",
        "enabled",
        42,
        "a" * 32,
        "/system.slice/bh-daemon.service",
        "/etc/systemd/system/bh-daemon.service",
        (),
        "control-group",
        "svc",
        "/opt/old/bin/bh-daemon",
        "running",
        "",
        "SECRET_MARKER",
    )
    new = UnitState(
        "codereeve.service", "not-found", "inactive", "", 0, "", "", "", ()
    )
    spec = ServiceSpec(
        *(PurePosixPath(p) for p in ("/project", "/opt/new")),
        "svc",
        None,
        None,
        PurePosixPath("/home/svc"),
    )
    original = tmp_path / "original.service"
    original.write_bytes(b"SECRET_MARKER")
    if fresh:
        old = UnitState(
            "bh-daemon.service", "not-found", "inactive", "", 0, "", "", "", ()
        )
    snapshot = model.ServiceSnapshot(old, new, (original,))
    return module.CutoverJournal.create(
        tmp_path, spec, snapshot, storage=store, mode=mode
    )


def test_journal_creation_excludes_secret_metadata(tmp_path: Path) -> None:
    """Reject blind UnitState serialization of command/environment values."""
    journal = make_journal(tmp_path)
    assert b"SECRET_MARKER" not in journal.path.read_bytes()
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.phase == "created"
    assert opened.pending is None


@pytest.mark.parametrize("mutation", ["truncate", "checksum", "unknown"])
def test_corruption_refuses_recovery(tmp_path: Path, mutation: str) -> None:
    """Require closed checksummed records before any recovery authority."""
    journal = make_journal(tmp_path)
    data = journal.path.read_bytes()
    if mutation == "truncate":
        data = data[:-1]
    elif mutation == "checksum":
        data = data.replace(b"created", b"damaged")
    else:
        data += b"{}\n"
    journal.path.write_bytes(data)
    with pytest.raises(CutoverError):
        module.CutoverJournal.open(journal.path, storage=journal.storage)


def test_pending_intent_survives_reopen(tmp_path: Path) -> None:
    """Never infer a systemd effect completed from filesystem appearance."""
    journal = make_journal(tmp_path)
    journal.record(
        "effect_intent",
        {"operation": "verify_job", "unit": "codereeve-verify-a.service"},
    )
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.pending["operation"] == "verify_job"
    with pytest.raises(CutoverError):
        opened.record("aborted", {})


def test_illegal_transition_and_arbitrary_metadata_refuse(
    tmp_path: Path,
) -> None:
    """Prevent skipping health or persisting secret-bearing output."""
    journal = make_journal(tmp_path)
    with pytest.raises(CutoverError):
        journal.record("committed", {})
    with pytest.raises(CutoverError):
        journal.record(
            "effect_intent",
            {"operation": "verify_job", "stdout": "SECRET_MARKER"},
        )
    assert b"SECRET_MARKER" not in journal.path.read_bytes()


def advance(journal: module.CutoverJournal, phase: str = "migrated") -> None:
    """Exercise actual paired journal effects up to the requested phase."""
    journal.record(
        "preflight_passed",
        {
            "old_uid": 41,
            "old_gid": 41,
            "new_uid": 42,
            "new_gid": 43,
            "provenance_digest": "a" * 64,
        },
    )
    for event, effects in (
        ("stopped", ("stop_old",)),
        ("guarded", ("guard_old", "reload")),
        ("migrated", ("migrate",)),
    ):
        for operation in effects:
            metadata = {"operation": operation}
            if operation == "migrate":
                metadata["path"] = str(journal.path.parent / "migration.json")
            journal.record("effect_intent", metadata)
            journal.record("effect_done", metadata)
        journal.record(event, {})
        if event == phase:
            return


def rollback(journal: module.CutoverJournal) -> None:
    """Record coordinator proof of complete writer shutdown."""
    journal.record("rollback_intent", {})
    journal.record("effect_intent", {"operation": "stop_all"})
    journal.record("effect_done", {"operation": "stop_all"})
    journal.record("all_writers_stopped", {})


def test_publication_preserves_runtime_and_restores_absence(
    tmp_path: Path,
) -> None:
    """Quarantine changed bytes before restoring exact migration inputs."""
    journal = make_journal(tmp_path)
    advance(journal)
    state = tmp_path / "state"
    state.mkdir()
    (state / "log").write_bytes(b"before")
    absent = tmp_path / "absent"
    assert hasattr(journal, "capture_publication")
    journal.capture_publication({state: "runtime", absent: "runtime"})
    (state / "log").write_bytes(b"new writes")
    absent.write_bytes(b"new file")
    rollback(journal)
    journal.restore_publication()
    assert (state / "log").read_bytes() == b"before"
    assert not absent.exists()
    preserved = list(tmp_path.glob(".codereeve-cutover/*/quarantine-*"))
    assert any(
        p.is_dir() and (p / "log").read_bytes() == b"new writes"
        for p in preserved
    )
    assert any(
        p.is_file() and p.read_bytes() == b"new file" for p in preserved
    )


def test_nested_operator_drift_prevents_all_runtime_moves(
    tmp_path: Path,
) -> None:
    """A runtime ancestor must not authorize overwriting operator config."""
    journal = make_journal(tmp_path)
    advance(journal)
    state = tmp_path / "state"
    state.mkdir()
    config = state / "config.env"
    config.write_bytes(b"original")
    assert hasattr(journal, "capture_publication")
    journal.capture_publication({state: "runtime", config: "operator"})
    config.write_bytes(b"operator edit")
    rollback(journal)
    with pytest.raises(CutoverError):
        journal.restore_publication()
    assert config.read_bytes() == b"operator edit"
    assert not list(tmp_path.glob(".codereeve-cutover/*/quarantine-*"))


def test_restore_requires_all_writer_shutdown(tmp_path: Path) -> None:
    """A new-service stop alone cannot authorize filesystem rollback."""
    journal = make_journal(tmp_path)
    assert hasattr(journal, "restore_publication")
    with pytest.raises(CutoverError):
        journal.restore_publication()


def test_install_only_never_allows_start(tmp_path: Path) -> None:
    """Keep install-only provenance distinct from healthy activation."""
    journal = make_journal(tmp_path, mode="install_only")
    with pytest.raises(CutoverError):
        journal.record("effect_intent", {"operation": "start_new"})
    for operation in ("publish_unit", "reload"):
        journal.record("effect_intent", {"operation": operation})
        journal.record("effect_done", {"operation": operation})
    journal.record("installed", {})
    assert (
        module.CutoverJournal.open(journal.path, storage=journal.storage).phase
        == "installed"
    )


def test_interrupted_stop_enters_rollback_without_false_completion(
    tmp_path: Path,
) -> None:
    """Retain interrupted forward effects while proving writer shutdown."""
    journal = make_journal(tmp_path)
    journal.record(
        "preflight_passed",
        {
            "old_uid": 41,
            "old_gid": 41,
            "new_uid": 42,
            "new_gid": 43,
            "provenance_digest": "a" * 64,
        },
    )
    journal.record("effect_intent", {"operation": "stop_old"})
    journal.record("rollback_intent", {})
    assert journal.interrupted == [{"operation": "stop_old"}]
    journal.record("effect_intent", {"operation": "stop_all"})
    journal.record("effect_done", {"operation": "stop_all"})
    journal.record("all_writers_stopped", {})
    assert module.CutoverJournal.open(
        journal.path, storage=journal.storage
    ).interrupted


def test_atomic_record_failure_leaves_reopenable_prefix(
    tmp_path: Path,
) -> None:
    """An interrupted journal publication must leave old or new valid state."""
    journal = make_journal(tmp_path)

    def fail(source: Path, target: Path) -> None:
        raise OSError("private marker")

    journal.storage.replace_journal = fail
    with pytest.raises(CutoverError):
        journal.record(
            "effect_intent",
            {"operation": "verify_job", "unit": "codereeve-verify-a.service"},
        )
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.phase == "created" and opened.pending is None


def test_preflight_abort_retains_job_cleanup_obligation(
    tmp_path: Path,
) -> None:
    """Cancellation requires explicit cleanup of a possibly running job."""
    journal = make_journal(tmp_path)
    job = {"operation": "verify_job", "unit": "codereeve-verify-a.service"}
    journal.record("effect_intent", job)
    journal.record("abort_intent", {})
    with pytest.raises(CutoverError):
        journal.record("aborted", {})
    journal.record("effect_resolved", {**job, "resolution": "compensated"})
    journal.record("aborted", {})
    assert journal.phase == "aborted"


@pytest.mark.parametrize("when", ["before", "after"])
def test_quarantine_interruption_resumes_with_protected_child(
    tmp_path: Path, when: str
) -> None:
    """Reopen around a real rename without losing protected input evidence."""
    journal = make_journal(tmp_path)
    advance(journal)
    state = tmp_path / "state"
    state.mkdir()
    config = state / "config.env"
    config.write_bytes(b"operator")
    (state / "log").write_bytes(b"original")
    journal.capture_publication({state: "runtime", config: "operator"})
    (state / "log").write_bytes(b"new")
    rollback(journal)
    move = journal.storage.move

    def interrupted(source: Path, destination: Path) -> None:
        if when == "after":
            move(source, destination)
        raise OSError("crash")

    journal.storage.move = interrupted
    with pytest.raises(CutoverError):
        journal.restore_publication()
    journal.storage.move = move
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    opened.restore_publication()
    assert (state / "log").read_bytes() == b"original"
    assert config.read_bytes() == b"operator"


def test_partial_restore_is_resumable_without_overwrite(
    tmp_path: Path,
) -> None:
    """Resume verified partial copies without overwriting existing bytes."""
    journal = make_journal(tmp_path)
    advance(journal)
    state = tmp_path / "state"
    state.mkdir()
    (state / "a").write_bytes(b"original a")
    (state / "b").write_bytes(b"original b")
    journal.capture_publication({state: "runtime"})
    (state / "a").write_bytes(b"runtime")
    rollback(journal)
    write = journal.storage.write

    def interrupted(path: Path, data: bytes) -> None:
        write(path, data)
        if path == state / "a":
            raise OSError("crash")

    journal.storage.write = interrupted
    with pytest.raises(CutoverError):
        journal.restore_publication()
    journal.storage.write = write
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    opened.restore_publication()
    assert (state / "b").read_bytes() == b"original b"


def test_filesystem_restored_requires_publication_verification(
    tmp_path: Path,
) -> None:
    """Require publication verification before generic restoration."""
    journal = make_journal(tmp_path)
    advance(journal)
    path = tmp_path / "runtime"
    path.write_bytes(b"old")
    journal.capture_publication({path: "runtime"})
    rollback(journal)
    with pytest.raises(CutoverError):
        journal.record("filesystem_restored", {})


@pytest.mark.parametrize(
    "mutation", ["version", "duplicate", "mode", "unit_type", "node_escape"]
)
def test_rechecks_closed_manifest_after_valid_checksum(
    tmp_path: Path, mutation: str
) -> None:
    """A recomputed checksum must not bypass schema or identity validation."""
    import hashlib
    import json

    journal = make_journal(tmp_path)
    record = json.loads(journal.path.read_bytes())
    meta = record["metadata"]
    if mutation == "version":
        meta["version"] = True
    elif mutation == "duplicate":
        meta["original"].append(meta["original"][0])
    elif mutation == "mode":
        meta["mode"] = "unknown"
    elif mutation == "unit_type":
        meta["old"]["active_state"] = []
    else:
        meta["original"][0]["nodes"][0]["name"] = "../escape"
    record.pop("checksum")
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    record["checksum"] = hashlib.sha256(encoded).hexdigest()
    journal.path.write_bytes(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with pytest.raises(CutoverError):
        module.CutoverJournal.open(journal.path, storage=journal.storage)


def test_cutover_cannot_publish_before_shutdown(tmp_path: Path) -> None:
    """Created-phase unit publication belongs only to explicit install-only."""
    journal = make_journal(tmp_path)
    with pytest.raises(CutoverError):
        journal.record("effect_intent", {"operation": "publish_unit"})


@pytest.mark.parametrize(
    "boundary",
    ["write_before", "write_after", "replace_before", "replace_after"],
)
def test_journal_io_interruption_keeps_valid_prefix(
    tmp_path: Path, boundary: str
) -> None:
    """Exercise actual file creation and atomic rename on both boundaries."""
    journal = make_journal(tmp_path)
    name = "write" if boundary.startswith("write") else "replace_journal"
    original = getattr(journal.storage, name)

    def fail(first: Path, second: object) -> None:
        if boundary.endswith("after"):
            original(first, second)
        raise OSError("interruption")

    setattr(journal.storage, name, fail)
    with pytest.raises(CutoverError):
        journal.record(
            "effect_intent",
            {"operation": "verify_job", "unit": "codereeve-verify-a.service"},
        )
    setattr(journal.storage, name, original)
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.phase == "created"
    assert (opened.pending is not None) == (boundary == "replace_after")


def test_commit_requires_health_and_enablement(tmp_path: Path) -> None:
    """Verified health cannot bypass either required enablement completion."""
    journal = make_journal(tmp_path)
    advance(journal)
    runtime = tmp_path / "runtime"
    journal.capture_publication({runtime: "runtime"})
    for op in ("publish_unit", "reload"):
        journal.record("effect_intent", {"operation": op})
        journal.record("effect_done", {"operation": op})
    journal.record("published", {})
    journal.record("effect_intent", {"operation": "start_new"})
    journal.record("effect_done", {"operation": "start_new"})
    journal.record(
        "activated", {"pid": 9, "invocation_id": "b" * 32, "started_ns": 10}
    )
    journal.record("verified", {})
    with pytest.raises(CutoverError):
        journal.record("committed", {})
    for op in ("enable_new", "disable_old"):
        journal.record("effect_intent", {"operation": op})
        journal.record("effect_done", {"operation": op})
    journal.record("committed", {})
    with pytest.raises(CutoverError):
        journal.record("rollback_intent", {})
    assert (
        module.CutoverJournal.open(journal.path, storage=journal.storage).phase
        == "committed"
    )


def test_pre_stop_abort_after_successful_preflight_keeps_old_running(
    tmp_path: Path,
) -> None:
    """No old-stop intent means recovery can safely remain pre-stop."""
    journal = make_journal(tmp_path)
    journal.record(
        "preflight_passed",
        {
            "old_uid": 41,
            "old_gid": 41,
            "new_uid": 42,
            "new_gid": 43,
            "provenance_digest": "a" * 64,
        },
    )
    journal.record("abort_intent", {})
    journal.record("aborted", {})
    assert journal.phase == "aborted"


def test_install_only_abort_requires_restoring_published_selection(
    tmp_path: Path,
) -> None:
    """A never-started service still needs its modified selection restored."""
    journal = make_journal(tmp_path, mode="install_only")
    journal.record("effect_intent", {"operation": "publish_unit"})
    journal.record("effect_done", {"operation": "publish_unit"})
    journal.record("abort_intent", {})
    with pytest.raises(CutoverError):
        journal.record("aborted", {})
    for operation in ("restore_selection", "reload"):
        journal.record("effect_intent", {"operation": operation})
        journal.record("effect_done", {"operation": operation})
    journal.record("aborted", {})


def test_safe_service_spec_survives_reopen(tmp_path: Path) -> None:
    """Recover declared paths without exposing environment values."""
    journal = make_journal(tmp_path)
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert hasattr(opened, "spec")
    assert opened.spec.environment.as_posix() == "/opt/new"
    assert opened.spec.run_user == "svc"
    assert hasattr(opened, "original_snapshots")
    assert opened.original_snapshots[0].nodes[0].kind == "file"


def test_file_fsync_precedes_journal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require flushed content before an atomic journal replacement."""
    import os

    journal = make_journal(tmp_path)
    calls = []
    sync = os.fsync
    replace = journal.storage.replace_journal

    def flush(fd: int) -> None:
        sync(fd)
        calls.append("flush")

    def publish(source: Path, target: Path) -> None:
        calls.append("publish")
        replace(source, target)

    monkeypatch.setattr(os, "fsync", flush)
    journal.storage.replace_journal = publish
    journal.record(
        "effect_intent",
        {"operation": "verify_job", "unit": "codereeve-verify-a.service"},
    )
    assert calls.index("flush") < calls.index("publish")


def test_absent_old_service_has_explicit_absent_identity(
    tmp_path: Path,
) -> None:
    """A fresh install must not fabricate a former service account."""
    journal = make_journal(tmp_path, fresh=True)
    journal.record(
        "preflight_passed",
        {
            "old_uid": None,
            "old_gid": None,
            "new_uid": 42,
            "new_gid": 43,
            "provenance_digest": "a" * 64,
        },
    )
    assert journal.phase == "preflight_passed"


def test_saved_specification_and_backup_accessors(tmp_path: Path) -> None:
    """Immutable representations locate verified private originals only."""
    journal = make_journal(tmp_path)
    assert (journal.original_backup(0) / "0").read_bytes() == b"SECRET_MARKER"
    with pytest.raises(CutoverError):
        journal.original_backup(-1)


def test_private_ancestor_permissions_refuse_recovery(tmp_path: Path) -> None:
    """Every recovery directory must be private, not only blob leaves."""
    journal = make_journal(tmp_path)
    journal.storage.set_metadata(
        journal.path.parent / "original", 0o777, 42, 43
    )
    with pytest.raises(CutoverError):
        module.CutoverJournal.open(journal.path, storage=journal.storage)


def test_verification_effect_cannot_name_known_daemon(tmp_path: Path) -> None:
    """Preflight cleanup authority must never include either daemon unit."""
    journal = make_journal(tmp_path)
    with pytest.raises(CutoverError):
        journal.record(
            "effect_intent",
            {"operation": "verify_job", "unit": "bh-daemon.service"},
        )


def test_fixed_publication_effect_records_selection_digest(
    tmp_path: Path,
) -> None:
    """Persist exact intended publication identity without copying content."""
    journal = make_journal(tmp_path, mode="install_only")
    effect = {
        "operation": "publish_unit",
        "path": str(tmp_path / "unit"),
        "digest": "a" * 64,
    }
    journal.record("effect_intent", effect)
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.pending == effect
    with pytest.raises(CutoverError):
        journal.record("effect_done", {**effect, "digest": "secret"})


def test_prior_selection_waits_for_generic_migration_restore(
    tmp_path: Path,
) -> None:
    """Restore migration inputs before restoring startup selection."""
    journal = make_journal(tmp_path)
    advance(journal)
    rollback(journal)
    journal.record("filesystem_restored", {})
    with pytest.raises(CutoverError):
        journal.record("effect_intent", {"operation": "restore_selection"})


@pytest.mark.parametrize("namespace", ["source", "destination"])
def test_quarantine_retry_repeats_failed_namespace_flush(
    tmp_path: Path, namespace: str
) -> None:
    """Visible renamed bytes must not bypass either failed namespace flush."""
    journal = make_journal(tmp_path)
    advance(journal)
    runtime_parent = tmp_path / "outputs"
    runtime_parent.mkdir()
    source = runtime_parent / "runtime"
    source.mkdir()
    (source / "log").write_bytes(b"original")
    journal.capture_publication({source: "runtime"})
    (source / "log").write_bytes(b"new runtime")
    rollback(journal)
    target = (
        source.parent
        / ".codereeve-cutover"
        / journal.path.parent.name
        / "quarantine-0"
    )
    failed_parent = source.parent if namespace == "source" else target.parent
    sync = journal.storage.sync_directory
    failures: list[Path] = []

    def fail(path: Path) -> None:
        if not source.exists() and target.exists() and path == failed_parent:
            failures.append(path)
            raise OSError("interrupted namespace flush")
        sync(path)

    journal.storage.sync_directory = fail
    with pytest.raises(CutoverError):
        journal.restore_publication()
    assert target.joinpath("log").read_bytes() == b"new runtime"
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    with pytest.raises(CutoverError):
        opened.restore_publication()
    assert len(failures) == 2
    assert opened.pending is not None
    assert opened.pending["operation"] == "quarantine"
    journal.storage.sync_directory = sync
    retried: list[Path] = []

    def observe(path: Path) -> None:
        if not source.exists() and target.exists():
            retried.append(path)
        sync(path)

    journal.storage.sync_directory = observe
    reopened = module.CutoverJournal.open(
        journal.path, storage=journal.storage
    )
    reopened.restore_publication()
    assert source.joinpath("log").read_bytes() == b"original"
    assert source.parent in retried
    assert target.parent in retried


@pytest.mark.parametrize("tree", [False, True])
def test_matching_and_partial_restore_retry_repeats_file_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree: bool
) -> None:
    """Reflush copied files after interruption, including partial trees."""
    import os

    journal = make_journal(tmp_path)
    advance(journal)
    source = tmp_path / "runtime"
    if tree:
        source.mkdir()
        destination = source / "a"
        destination.write_bytes(b"original")
        (source / "b").write_bytes(b"second")
    else:
        destination = source
        destination.write_bytes(b"original")
    journal.capture_publication({source: "runtime"})
    destination.write_bytes(b"new runtime")
    rollback(journal)
    sync = os.fsync
    failures: list[int] = []

    def fail(fd: int) -> None:
        if (
            destination.exists()
            and destination.read_bytes() == b"original"
            and os.fstat(fd).st_ino == destination.stat().st_ino
        ):
            failures.append(fd)
            raise OSError("interrupted content flush")
        sync(fd)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(CutoverError):
        journal.restore_publication()
    assert destination.read_bytes() == b"original"
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    with pytest.raises(CutoverError):
        opened.restore_publication()
    assert len(failures) == 2
    assert opened.pending is not None
    assert opened.pending["operation"] == "restore_publication"
    monkeypatch.setattr(os, "fsync", sync)
    reopened = module.CutoverJournal.open(
        journal.path, storage=journal.storage
    )
    reopened.restore_publication()
    assert reopened.publication_restored
    if tree:
        assert (source / "b").read_bytes() == b"second"


def test_matching_restore_retry_repeats_parent_flush(tmp_path: Path) -> None:
    """A fully visible restored file still requires its namespace flush."""
    journal = make_journal(tmp_path)
    advance(journal)
    source = tmp_path / "runtime"
    source.write_bytes(b"original")
    journal.capture_publication({source: "runtime"})
    source.write_bytes(b"new runtime")
    rollback(journal)
    sync = journal.storage.sync_directory

    def fail(path: Path) -> None:
        if (
            source.exists()
            and source.read_bytes() == b"original"
            and path == source.parent
        ):
            raise OSError("interrupted restored namespace")
        sync(path)

    journal.storage.sync_directory = fail
    with pytest.raises(CutoverError):
        journal.restore_publication()
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    with pytest.raises(CutoverError):
        opened.restore_publication()
    assert opened.pending is not None
    assert opened.pending["operation"] == "restore_publication"
    journal.storage.sync_directory = sync
    module.CutoverJournal.open(
        journal.path, storage=journal.storage
    ).restore_publication()


def advance_runtime_phase(journal: module.CutoverJournal, phase: str) -> None:
    """Establish real journal prerequisites for later verification phases."""
    advance(journal)
    if phase == "migrated":
        return
    runtime = journal.path.parent.parent.parent / "runtime"
    journal.capture_publication({runtime: "runtime"})
    for operation in ("publish_unit", "reload"):
        journal.record("effect_intent", {"operation": operation})
        journal.record("effect_done", {"operation": operation})
    journal.record("published", {})
    if phase == "published":
        return
    journal.record("effect_intent", {"operation": "start_new"})
    journal.record("effect_done", {"operation": "start_new"})
    journal.record(
        "activated", {"pid": 90, "invocation_id": "b" * 32, "started_ns": 10}
    )
    if phase == "activated":
        return
    journal.record("verified", {})
    if phase == "verified":
        return
    for operation in ("enable_new", "disable_old"):
        journal.record("effect_intent", {"operation": operation})
        journal.record("effect_done", {"operation": operation})
    journal.record("committed", {})


@pytest.mark.parametrize(
    "phase", ["migrated", "published", "activated", "verified", "committed"]
)
def test_verification_jobs_have_durable_lifecycles_in_runtime_phases(
    tmp_path: Path, phase: str
) -> None:
    """Service context/live/recovery verification cannot bypass the journal."""
    journal = make_journal(tmp_path)
    advance_runtime_phase(journal, phase)
    job = {"operation": "verify_job", "unit": "codereeve-verify-a.service"}
    journal.record("effect_intent", job)
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.phase == phase and opened.pending == job
    assert opened.jobs == {job["unit"]}
    opened.record("effect_done", job)
    cleanup = {**job, "operation": "cleanup_job"}
    opened.record("effect_intent", cleanup)
    opened.record("effect_done", cleanup)
    assert not opened.jobs and opened.pending is None


@pytest.mark.parametrize(
    "phase,terminal,effects",
    [
        ("verified", "committed", ("enable_new", "disable_old")),
        ("committed", "finalized", ("release_gate", "publish_unit", "reload")),
    ],
)
def test_commit_and_finalization_wait_for_verification_cleanup(
    tmp_path: Path, phase: str, terminal: str, effects: tuple[str, ...]
) -> None:
    """Complete other prerequisites cannot conceal a verification job."""
    journal = make_journal(tmp_path)
    advance_runtime_phase(journal, phase)
    for operation in effects:
        journal.record("effect_intent", {"operation": operation})
        journal.record("effect_done", {"operation": operation})
    job = {"operation": "verify_job", "unit": "codereeve-verify-a.service"}
    journal.record("effect_intent", job)
    with pytest.raises(CutoverError, match="unresolved service effect"):
        journal.record(terminal, {})
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    with pytest.raises(CutoverError):
        opened.record(terminal, {})
    opened.record("effect_done", job)
    opened.record(terminal, {})
    assert opened.phase == terminal


def test_zero_action_inventory_advances_without_migration_manifest(
    tmp_path: Path,
) -> None:
    """Record a no-action inventory without a migration manifest."""
    journal = make_journal(tmp_path)
    advance(journal, "guarded")
    journal.record(
        "migration_not_needed",
        {"inventory_digest": "a" * 64, "action_count": 0},
    )
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.phase == "migrated"
    assert opened.migration_attempted is False
    assert all(
        event["metadata"].get("operation") != "migrate"
        for event in opened.events
    )
    rollback(opened)
    opened.record("filesystem_restored", {})
    opened.record("effect_intent", {"operation": "restore_selection"})
    opened.record("effect_done", {"operation": "restore_selection"})


@pytest.mark.parametrize(
    "metadata",
    [
        {"inventory_digest": "a" * 64, "action_count": False},
        {"inventory_digest": "a" * 64, "action_count": 1},
        {"inventory_digest": "a" * 64, "action_count": -1},
        {"inventory_digest": "invalid", "action_count": 0},
        {"inventory_digest": "a" * 64, "action_count": 0, "path": "/fake"},
    ],
)
def test_no_action_inventory_metadata_is_closed(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    """Only exact zero and a validated SHA256 inventory digest are accepted."""
    journal = make_journal(tmp_path)
    advance(journal, "guarded")
    with pytest.raises(CutoverError):
        journal.record("migration_not_needed", metadata)
    assert journal.phase == "guarded"


def test_no_action_cannot_conceal_an_attempted_migration(
    tmp_path: Path,
) -> None:
    """A cancelled generic attempt still needs its original recovery route."""
    journal = make_journal(tmp_path)
    advance(journal, "guarded")
    effect = {
        "operation": "migrate",
        "path": str(journal.path.parent / "migration.json"),
    }
    journal.record("effect_intent", effect)
    journal.record("effect_cancelled", effect)
    with pytest.raises(CutoverError):
        journal.record(
            "migration_not_needed",
            {"inventory_digest": "a" * 64, "action_count": 0},
        )


def test_quarantine_flushes_runtime_files_and_tree_before_completion(
    tmp_path: Path,
) -> None:
    """Require runtime file data and nested directories before completion."""
    journal = make_journal(tmp_path)
    advance(journal)
    source = tmp_path / "runtime"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "log").write_bytes(b"original")
    (source / "nested" / "state").write_bytes(b"original nested")
    journal.capture_publication({source: "runtime"})
    (source / "log").write_bytes(b"runtime writes")
    rollback(journal)
    target = journal.path.parent / "quarantine-0"
    files: set[Path] = set()
    directories: set[Path] = set()
    file_sync = journal.storage.sync_file
    directory_sync = journal.storage.sync_directory
    record = journal.record

    def flush_file(path: Path) -> None:
        file_sync(path)
        files.add(path)

    def flush_directory(path: Path) -> None:
        directory_sync(path)
        directories.add(path)

    def check_completion(event: str, metadata: dict[str, object]) -> None:
        if (
            event == "effect_done"
            and metadata.get("operation") == "quarantine"
        ):
            assert {target / "log", target / "nested" / "state"} <= files
            assert {
                target,
                target / "nested",
                target.parent,
                source.parent,
            } <= directories
        record(event, metadata)

    journal.storage.sync_file = flush_file
    journal.storage.sync_directory = flush_directory
    journal.record = check_completion
    journal.restore_publication()
    assert (target / "log").read_bytes() == b"runtime writes"


@pytest.mark.parametrize("operation", ["stage_secrets", "prepare_gate"])
def test_private_preparation_effects_are_journaled_before_mutation(
    tmp_path: Path,
    operation: str,
) -> None:
    """Allow private staging before canonical publication."""
    journal = make_journal(tmp_path)
    if operation == "prepare_gate":
        advance(journal, "migrated")
    metadata = {
        "operation": operation,
        "path": str(tmp_path / "owned"),
        "digest": "a" * 64,
    }
    journal.record("effect_intent", metadata)
    opened = module.CutoverJournal.open(journal.path, storage=journal.storage)
    assert opened.pending == metadata
    opened.record("effect_done", metadata)


def test_original_effective_selection_attestation_is_retained(
    tmp_path: Path,
) -> None:
    """Persist full loaded environment evidence as a digest without values."""
    from dataclasses import replace

    assert "environment_digest" in model.UnitState.__dataclass_fields__
    journal = make_journal(tmp_path)
    old = model.UnitState(
        "bh-daemon.service", "not-found", "inactive", "", 0, "", "", "", ()
    )
    old = replace(old, environment_digest="a" * 64)
    new = replace(old, name="codereeve.service")
    root = tmp_path / "other"
    root.mkdir()
    created = module.CutoverJournal.create(
        root,
        journal.spec,
        model.ServiceSnapshot(old, new),
        storage=journal.storage,
    )
    opened = module.CutoverJournal.open(created.path, storage=journal.storage)
    assert opened.header["old"]["environment_digest"] == "a" * 64


@pytest.mark.parametrize("invalid", [0, False, None])
def test_environment_digest_requires_a_string(
    tmp_path: Path, invalid: object
) -> None:
    """Falsy nonstrings cannot weaken original environment attestation."""
    journal = make_journal(tmp_path)
    selected = dict(journal.header["old"])
    selected["environment_digest"] = invalid
    with pytest.raises(CutoverError):
        module._validate_unit(selected)
