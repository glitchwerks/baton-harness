"""Interrupted transaction restoration and forward finalization contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CutoverContext

from codereeve.provenance import Provenance
from codereeve.service_cutover.coordinator import cutover
from codereeve.service_cutover.journal import CutoverJournal
from codereeve.service_cutover.model import (
    ServiceSnapshot,
    ServiceSpec,
    UnitState,
)


def test_failed_health_preserves_output_and_restores_original(
    cutover_context: CutoverContext,
) -> None:
    """Restore snapshot inputs after preserving new writes with no overlap."""
    spec, backend, storage = cutover_context
    path = backend.filesystem_root / "srv/project/.codereeve/new.log"
    original = backend.verify_health

    def failed(selected: ServiceSpec, started: UnitState) -> Provenance:
        path.write_bytes(b"new runtime output")
        backend.fail_at = "health"
        return original(selected, started)

    backend.verify_health = failed
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert not path.exists()
    assert any(
        p.read_bytes() == b"new runtime output"
        for p in backend.filesystem_root.rglob("new.log")
    )
    journal = CutoverJournal.open(result.journal_path, storage=storage)
    assert journal.phase == "rolled_back"
    assert max(backend.active_counts) <= 1
    assert all(s.active_state == "inactive" for s in backend.states.values())


def test_failed_upgrade_restores_data_before_old_activation(
    upgrade_context: CutoverContext,
) -> None:
    """Real generic restoration must precede restarting the original daemon."""
    spec, backend, storage = upgrade_context
    backend.fail_at = "health"
    original = backend.filesystem_root / "opt/old/bin/bh-daemon"
    before = (
        original.read_bytes(),
        original.stat().st_ino,
        original.stat().st_mtime_ns,
    )
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert (
        backend.filesystem_root / "srv/project/.baton-harness/run.log"
    ).read_bytes() == b"original runtime"
    assert (
        original.read_bytes(),
        original.stat().st_ino,
        original.stat().st_mtime_ns,
    ) == before
    assert backend.states["bh-daemon.service"].active_state == "active"
    assert max(backend.active_counts) <= 1


def test_external_old_environment_edit_is_never_restored(
    upgrade_context: CutoverContext,
) -> None:
    """Attestation backups grant no authority to overwrite old venv files."""
    spec, backend, storage = upgrade_context
    old = backend.filesystem_root / "opt/old/bin/bh-daemon"
    original = backend.verify_health

    def changed(selected: ServiceSpec, started: UnitState) -> Provenance:
        old.write_bytes(b"operator changed environment")
        backend.fail_at = "health"
        return original(selected, started)

    backend.verify_health = changed
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "incomplete"
    assert old.read_bytes() == b"operator changed environment"
    assert all(s.active_state == "inactive" for s in backend.states.values())


def test_ownership_rollback_preserves_retained_lock_inode(
    cutover_context: CutoverContext,
) -> None:
    """Restore original access in place after failed activation."""
    spec, backend, storage = cutover_context
    root = backend.filesystem_root / "srv/project"
    lock = root / ".codereeve-migration.lock"
    lock.write_bytes(b"")
    storage.set_metadata(lock, 0o600, 901, 902)
    before = lock.stat().st_ino
    state = root / ".codereeve"
    original = storage.metadata(state)
    backend.fail_at = "health"
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert lock.stat().st_ino == before
    assert result.recovery == "complete"
    assert storage.metadata(lock) == (0o600, 901, 902)
    assert storage.metadata(state) == original


def test_preflight_failure_never_stops_original(
    upgrade_context: CutoverContext,
) -> None:
    """Failed preflight cleans jobs without changing old runtime."""
    spec, backend, storage = upgrade_context
    backend.fail_at = "preflight"
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert backend.states["bh-daemon.service"].active_state == "active"
    assert not any(e.startswith("stop:") for e in backend.events)
    assert (
        CutoverJournal.open(result.journal_path, storage=storage).phase
        == "aborted"
    )


def test_rollback_guards_new_before_restoring_inputs(
    cutover_context: CutoverContext,
) -> None:
    """A stopped new service cannot autonomously restart during restoration."""
    spec, backend, storage = cutover_context
    backend.fail_at = "health"
    result = cutover(spec, backend=backend, storage=storage)
    journal = CutoverJournal.open(result.journal_path, storage=storage)
    operations = [
        e["metadata"].get("operation")
        for e in journal.events
        if e["event"] == "effect_done"
    ]
    assert "guard_new" in operations
    names = [
        e["event"]
        if e["event"] != "effect_done"
        else e["metadata"]["operation"]
        for e in journal.events
    ]
    assert names.index("guard_new") < names.index("publication_restored")


def test_committed_recovery_revalidates_and_only_finishes_forward(
    cutover_context: CutoverContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable commit cannot be converted into pre-commit rollback."""
    from conftest import Interrupted

    from codereeve.service_cutover import coordinator

    spec, backend, storage = cutover_context
    original = CutoverJournal.record

    def interrupted(
        self: CutoverJournal, event: str, metadata: dict[str, object]
    ) -> None:
        original(self, event, metadata)
        if event == "committed":
            raise Interrupted()

    monkeypatch.setattr(CutoverJournal, "record", interrupted)
    with pytest.raises(Interrupted):
        cutover(spec, backend=backend, storage=storage)
    monkeypatch.setattr(CutoverJournal, "record", original)
    assert hasattr(coordinator, "recover")
    path = next(
        (backend.filesystem_root / "srv/project/.codereeve-cutover").glob(
            "*/journal.jsonl"
        )
    )
    backend._starts.clear()
    result = coordinator.recover(path, backend=backend, storage=storage)
    assert result.status == "committed"
    assert CutoverJournal.open(path, storage=storage).phase == "finalized"
    assert "old_started" not in backend.events


def test_repeated_cutover_revalidates_completed_invocation(
    cutover_context: CutoverContext,
) -> None:
    """Rerun verifies health under prior transaction authority."""
    spec, backend, storage = cutover_context
    first = cutover(spec, backend=backend, storage=storage)
    second = cutover(spec, backend=backend, storage=storage)
    assert second.status == "committed"
    assert second.journal_path == first.journal_path
    assert backend.events.count("start_new") == 1


@pytest.mark.parametrize("mode", ["fresh", "upgrade", "installed"])
@pytest.mark.parametrize("prefix", range(1, 56))
def test_recovery_of_each_durable_forward_prefix(
    cutover_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
    prefix: int,
    mode: str,
    request: pytest.FixtureRequest,
) -> None:
    """A new coordinator resumes every durable cutover record prefix safely."""
    from conftest import Interrupted

    from codereeve.service_cutover.coordinator import recover

    spec, backend, storage = (
        cutover_context
        if mode == "fresh"
        else request.getfixturevalue("upgrade_context")
    )
    if mode == "installed":
        from codereeve.service_cutover.coordinator import install_only

        assert (
            install_only(spec, backend=backend, storage=storage).status
            == "installed"
        )
    original = CutoverJournal.record
    count = 0

    def interrupted(
        self: CutoverJournal, event: str, metadata: dict[str, object]
    ) -> None:
        nonlocal count
        original(self, event, metadata)
        count += 1
        if count == prefix:
            raise Interrupted()

    monkeypatch.setattr(CutoverJournal, "record", interrupted)
    try:
        result = cutover(spec, backend=backend, storage=storage)
    except Interrupted:
        monkeypatch.setattr(CutoverJournal, "record", original)
        path = next(
            p
            for p in (
                backend.filesystem_root / "srv/project/.codereeve-cutover"
            ).glob("*/journal.jsonl")
            if CutoverJournal.open(p, storage=storage).mode == "cutover"
        )
        fresh = type(backend)()
        fresh.states = backend.states
        fresh.events = backend.events
        fresh.active_counts = backend.active_counts
        result = recover(path, backend=fresh, storage=storage)
    assert result.status in {"failed", "committed"}, prefix
    assert result.recovery in {None, "complete"}
    assert max(backend.active_counts, default=0) <= 1


@pytest.mark.parametrize(
    "boundary",
    [
        "filesystem_restored",
        "restore_migration",
        "restore_selection",
        "selection_restored",
        "restore_enablement",
        "restore_activation",
    ],
)
def test_interrupted_rollback_resumes_without_republishing_migration(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """Completed restoration milestones survive another coordinator loss."""
    from conftest import Interrupted

    from codereeve.service_cutover.coordinator import recover

    spec, backend, storage = upgrade_context
    backend.fail_at = "health"
    original = CutoverJournal.record

    def interrupted(
        self: CutoverJournal, event: str, metadata: dict[str, object]
    ) -> None:
        original(self, event, metadata)
        if event == boundary or (
            event == "effect_done" and metadata.get("operation") == boundary
        ):
            raise Interrupted()

    monkeypatch.setattr(CutoverJournal, "record", interrupted)
    with pytest.raises(Interrupted):
        cutover(spec, backend=backend, storage=storage)
    monkeypatch.setattr(CutoverJournal, "record", original)
    path = next(
        (backend.filesystem_root / "srv/project/.codereeve-cutover").glob(
            "*/journal.jsonl"
        )
    )
    result = recover(path, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert backend.states["bh-daemon.service"].active_state == "active"
    assert (
        backend.filesystem_root / "srv/project/.baton-harness/run.log"
    ).read_bytes() == b"original runtime"
    assert max(backend.active_counts) <= 1


def test_old_runtime_writes_after_restored_start_survive_recovery(
    upgrade_context: CutoverContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tail recovery preserves writes from the restored daemon."""
    from conftest import Interrupted

    from codereeve.service_cutover.coordinator import recover

    spec, backend, storage = upgrade_context
    backend.fail_at = "health"
    original = backend.restore_activation
    path = backend.filesystem_root / "srv/project/.baton-harness/run.log"

    def started(snapshot: ServiceSnapshot) -> None:
        original(snapshot)
        path.write_bytes(b"legitimate old runtime output")
        raise Interrupted()

    backend.restore_activation = started
    with pytest.raises(Interrupted):
        cutover(spec, backend=backend, storage=storage)
    backend.restore_activation = original
    journal = next(
        (backend.filesystem_root / "srv/project/.codereeve-cutover").glob(
            "*/journal.jsonl"
        )
    )
    assert (
        recover(journal, backend=backend, storage=storage).recovery
        == "complete"
    )
    assert path.read_bytes() == b"legitimate old runtime output"
    assert backend.events.count("old_started") == 1


def test_unaccounted_migration_blocks_no_action_recovery(
    cutover_context: CutoverContext,
) -> None:
    """A zero-action cutover still checks unknown transaction authority."""
    spec, backend, storage = cutover_context
    original = backend.verify_health

    def failed(selected: ServiceSpec, started: UnitState) -> Provenance:
        unknown = (
            backend.filesystem_root
            / "srv/project/.codereeve-migration/unknown"
        )
        unknown.mkdir(parents=True)
        (unknown / "journal.jsonl").write_bytes(b"corrupt")
        backend.fail_at = "health"
        return original(selected, started)

    backend.verify_health = failed
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "incomplete"
    assert all(s.active_state == "inactive" for s in backend.states.values())


def test_operator_unit_drift_prevents_any_runtime_quarantine(
    cutover_context: CutoverContext,
) -> None:
    """Unknown unit bytes must survive and block data restoration work."""
    spec, backend, storage = cutover_context
    original = backend.verify_health
    unit = backend.filesystem_root / "etc/systemd/system/codereeve.service"
    runtime = backend.filesystem_root / "srv/project/.codereeve/new.log"

    def failed(selected: ServiceSpec, started: UnitState) -> Provenance:
        unit.write_bytes(b"operator unit change")
        runtime.write_bytes(b"new runtime")
        backend.fail_at = "health"
        return original(selected, started)

    backend.verify_health = failed
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "incomplete"
    assert unit.read_bytes() == b"operator unit change"
    assert runtime.read_bytes() == b"new runtime"


def test_recover_installed_is_read_only_idempotent(
    cutover_context: CutoverContext,
) -> None:
    """A completed inactive installation retains its published authority."""
    from codereeve.service_cutover.coordinator import install_only, recover

    spec, backend, storage = cutover_context
    installed = install_only(spec, backend=backend, storage=storage)
    before = installed.journal_path.read_bytes()
    assert (
        recover(
            installed.journal_path, backend=backend, storage=storage
        ).status
        == "installed"
    )
    assert installed.journal_path.read_bytes() == before


def test_revalidation_refuses_changed_enablement(
    cutover_context: CutoverContext,
) -> None:
    """Forward recovery cannot claim a disabled committed service healthy."""
    from dataclasses import replace

    from codereeve.service_cutover.coordinator import recover

    spec, backend, storage = cutover_context
    result = cutover(spec, backend=backend, storage=storage)
    backend.states["codereeve.service"] = replace(
        backend.states["codereeve.service"], enabled_state="disabled"
    )
    assert (
        recover(result.journal_path, backend=backend, storage=storage).status
        == "incomplete"
    )


def test_pending_preflight_job_is_adopted_before_abort(
    upgrade_context: CutoverContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh recovery cleans the exact persisted transient identity first."""
    from conftest import Interrupted

    from codereeve.service_cutover.coordinator import recover

    spec, backend, storage = upgrade_context
    unit = "codereeve-verify-" + "d" * 32 + ".service"

    def interrupted(selected: ServiceSpec, **kwargs: object) -> None:
        backend._job_observer(unit, False)
        raise Interrupted()

    backend.preflight = interrupted
    with pytest.raises(Interrupted):
        cutover(spec, backend=backend, storage=storage)
    path = next(
        (backend.filesystem_root / "srv/project/.codereeve-cutover").glob(
            "*/journal.jsonl"
        )
    )
    cleaned = []
    backend.cleanup_verification = cleaned.append
    assert (
        recover(path, backend=backend, storage=storage).recovery == "complete"
    )
    assert cleaned == [unit]
    assert backend.states["bh-daemon.service"].active_state == "active"
    assert not CutoverJournal.open(path, storage=storage).jobs


def test_new_lock_restores_old_account_access_without_unlink(
    upgrade_context: CutoverContext,
) -> None:
    """A lock introduced by cutover stays usable by the restored old UID."""
    from dataclasses import replace

    spec, backend, storage = upgrade_context
    old = backend.states["bh-daemon.service"]
    backend.states[old.name] = replace(old, user="prior")
    unit = backend.filesystem_root / "etc/systemd/system/bh-daemon.service"
    unit.write_bytes(unit.read_bytes().replace(b"User=runner", b"User=prior"))
    backend.account = lambda user: (
        (901, 902, spec.home) if user == "prior" else (1001, 1001, spec.home)
    )
    lock = backend.target_path(spec.project_root) / ".codereeve-migration.lock"
    assert not lock.exists()
    backend.fail_at = "health"
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert storage.metadata(lock) == (0o600, 901, 902)


@pytest.mark.parametrize("selection", ["interpreter", "workflow"])
def test_selected_old_inputs_are_attestation_only(
    upgrade_context: CutoverContext, selection: str
) -> None:
    """Changed original inputs cannot authorize restart."""
    from dataclasses import replace

    spec, backend, storage = upgrade_context
    if selection == "interpreter":
        path = backend.filesystem_root / "opt/old/bin/python"
    else:
        path = backend.filesystem_root / "srv/project/old-workflow.md"
        path.write_bytes(b"original workflow")
        old = backend.states["bh-daemon.service"]
        backend.states[old.name] = replace(
            old,
            exec_start_argv=old.exec_start_argv
            + " --workflow /srv/project/old-workflow.md",
        )
    original = backend.verify_health

    def changed(selected: ServiceSpec, started: UnitState) -> Provenance:
        path.write_bytes(b"operator changed selected input")
        backend.fail_at = "health"
        return original(selected, started)

    backend.verify_health = changed
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "incomplete"
    assert path.read_bytes() == b"operator changed selected input"


def test_competing_recovery_authorities_refuse_before_service_actions(
    cutover_context: CutoverContext,
) -> None:
    """An explicit recovery path cannot bypass a competing transaction."""
    from codereeve.service_cutover.coordinator import recover
    from codereeve.service_cutover.selection import original_selection

    spec, backend, storage = cutover_context
    root = backend.target_path(spec.project_root)
    snapshot = original_selection(spec, backend)
    first = CutoverJournal.create(root, spec, snapshot, storage=storage)
    CutoverJournal.create(root, spec, snapshot, storage=storage)
    assert (
        recover(first.path, backend=backend, storage=storage).status
        == "incomplete"
    )
    assert not backend.events


@pytest.mark.parametrize("retarget", [False, True])
def test_old_venv_binary_resolution_is_attested(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
    retarget: bool,
) -> None:
    """Accept a standard venv link but refuse its changed resolved binary."""
    from codereeve.service_cutover.model import CutoverError

    spec, backend, storage = upgrade_context
    interpreter = backend.filesystem_root / "opt/old/bin/python"
    first = backend.filesystem_root / "opt/base-python"
    second = backend.filesystem_root / "opt/other-python"
    first.write_bytes(b"\x7fELFfirst binary")
    second.write_bytes(b"\x7fELFsecond binary")
    selected_binary = [first]
    resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, strict=False: resolve(
            selected_binary[0] if path == interpreter else path, strict=strict
        ),
    )
    capture = storage.capture

    def capture_regular(path: Path, *args: object, **kwargs: object) -> object:
        if path == interpreter:
            raise CutoverError("general symlink snapshots are unsupported")
        return capture(path, *args, **kwargs)

    monkeypatch.setattr(storage, "capture", capture_regular)
    health = backend.verify_health

    def failed(selected: ServiceSpec, started: UnitState) -> Provenance:
        if retarget:
            selected_binary[0] = second
        backend.fail_at = "health"
        return health(selected, started)

    backend.verify_health = failed
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == ("incomplete" if retarget else "complete")
    assert first.read_bytes() == b"\x7fELFfirst binary"
    assert second.read_bytes() == b"\x7fELFsecond binary"
