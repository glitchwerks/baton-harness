"""Fail-closed rollback policy for durable service-cutover transactions."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, replace
from pathlib import Path

from codereeve.migration.journal import JournalError
from codereeve.migration.lease import LeaseError
from codereeve.migration.transaction import MigrationError

from .journal import CutoverJournal
from .model import (
    CutoverError,
    CutoverResult,
    ServiceSnapshot,
    StartEvidence,
)
from .selection import (
    digest,
    effect,
    layout_for,
    linux_path,
    observe,
    original_selection,
    project_lease,
    publish_effect,
    restore_writer_lock,
    verify_activation,
)
from .storage import FileSnapshot
from .systemd import (
    NEW_ENABLEMENT,
    NEW_ENABLEMENT_TARGET,
    NEW_UNIT,
    OLD_UNIT,
    SystemdBackend,
)


def validate_units(journal: CutoverJournal) -> None:
    """Reject unrelated unit edits before quarantining any runtime output."""
    for index, snapshot in enumerate(journal.original_snapshots):
        path = Path(snapshot.path)
        if path.name not in {OLD_UNIT, NEW_UNIT}:
            continue
        candidates: list[str | None] = ["original"]
        for event in journal.events:
            metadata = event["metadata"]
            if (
                metadata.get("operation") == "restore_selection"
                and event["event"] == "effect_intent"
            ):
                candidates.append("original")
            if metadata.get("path") != str(path) or metadata.get(
                "operation"
            ) not in {
                "publish_unit",
                "guard_old",
                "guard_new",
                "stage_canonical",
            }:
                continue
            intended = (
                None
                if metadata["operation"] == "stage_canonical"
                else metadata["digest"]
            )
            if event["event"] == "effect_intent":
                candidates.append(intended)
            elif event["event"] == "effect_done":
                candidates = [intended]
        matched = False
        for intended in candidates:
            if intended == "original":
                matched |= journal.storage.matches(path, snapshot)
            elif intended is None:
                matched |= not path.exists() and not path.is_symlink()
            elif path.is_file() and not path.is_symlink():
                matched |= hashlib.sha256(
                    journal.storage.read(path)
                ).hexdigest() == intended and journal.storage.metadata(
                    path
                ) == (0o644, 0, 0)
        if index in journal.restoring_inputs:
            matched |= partial_original(journal, snapshot)
        if not matched:
            raise CutoverError("owned unit selection drifted")


def restored_snapshot(
    journal: CutoverJournal, backend: SystemdBackend
) -> ServiceSnapshot:
    """Reconstruct typed original activation only from restored selections."""
    states = []
    for key, name in (("old", OLD_UNIT), ("new", NEW_UNIT)):
        saved = journal.header[key]
        actual = observe(backend, name)
        if (
            digest([actual.exec_start, actual.exec_start_argv])
            != saved["selection_digest"]
            or actual.environment_digest != saved["environment_digest"]
            or any(
                getattr(actual, field) != saved[field]
                for field in (
                    "load_state",
                    "fragment_path",
                    "user",
                    "kill_mode",
                )
            )
            or tuple(saved["dropin_paths"]) != actual.dropin_paths
        ):
            raise CutoverError("original service selection is unproven")
        states.append(
            replace(
                actual,
                active_state=saved["active_state"],
                enabled_state=saved["enabled_state"],
                main_pid=saved["main_pid"],
                invocation_id=saved["invocation_id"],
                control_group=saved["control_group"],
                sub_state=saved["sub_state"],
            )
        )
    if original_selection(journal.spec, backend).files != tuple(
        Path(s.path) for s in journal.original_snapshots
    ):
        raise CutoverError("original interpreter or input selection changed")
    return ServiceSnapshot(states[0], states[1])


def partial_original(journal: CutoverJournal, snapshot: FileSnapshot) -> bool:
    """Observe a possible interrupted original copy without changing it."""
    actual = journal.storage.inspect(Path(snapshot.path), mask=True)
    expected = {node.name: node for node in snapshot.nodes}
    for node in actual.nodes:
        original = expected.get(node.name)
        if original is None or (node.kind, node.digest) != (
            original.kind,
            original.digest,
        ):
            return False
        if (node.mode, node.uid, node.gid) not in {
            (original.mode, original.uid, original.gid),
            (
                0o700 if node.kind == "directory" else 0o600,
                *journal.storage.owner(),
            ),
        }:
            return False
    return True


def restore_selection(
    journal: CutoverJournal, backend: SystemdBackend
) -> None:
    """Validate every operator input before restoring any selected file."""
    layout = layout_for(journal.spec, backend)
    units = {layout.legacy_unit, layout.canonical_unit}
    validate_units(journal)
    planned = []
    for index, snapshot in enumerate(journal.original_snapshots):
        path = Path(snapshot.path)
        backup = journal.original_backup(index)
        original = journal.storage.matches(path, snapshot)
        pending = journal.restoring_inputs.get(index)
        resume = pending is not None and not journal.storage.matches(
            path, pending
        )
        if not original:
            if pending is not None:
                if resume and not partial_original(journal, snapshot):
                    raise CutoverError("partial operator restoration drifted")
            elif path not in units:
                publications = [
                    e["metadata"]
                    for e in journal.events
                    if e["event"] == "effect_intent"
                    and e["metadata"].get("operation") == "publish_secrets"
                    and e["metadata"].get("path") == str(path)
                ]
                if (
                    not publications
                    or not path.is_file()
                    or path.is_symlink()
                    or hashlib.sha256(journal.storage.read(path)).hexdigest()
                    != publications[-1]["digest"]
                    or journal.storage.metadata(path) != (0o600, 0, 0)
                ):
                    raise CutoverError("original operator input drifted")
        authorized = journal.storage.inspect(path, mask=True)
        planned.append((index, snapshot, backup, original, resume, authorized))
    for index, snapshot, backup, original, resume, authorized in planned:
        if not journal.storage.matches(Path(snapshot.path), authorized):
            raise CutoverError("operator input changed during restoration")
        path = Path(snapshot.path)
        if original:
            journal.storage.make_durable(path, snapshot)
        else:
            if index not in journal.restoring_inputs:
                journal.record(
                    "restore_input_intent",
                    {"index": index, "snapshot": authorized.metadata()},
                )
            if not journal.storage.matches(path, authorized):
                raise CutoverError(
                    "operator input changed after restoration intent"
                )
            if not resume and (path.exists() or path.is_symlink()):
                journal.storage.safe(path, leaf_link=True)
                if path.is_dir():
                    raise CutoverError(
                        "selection restoration target is not a file"
                    )
                path.unlink()
                journal.storage.sync_directory(path.parent)
            journal.storage.resume_restore(snapshot, backup)
        if index in journal.restoring_inputs:
            journal.record("restore_input_done", {"index": index})


def rollback(
    journal: CutoverJournal, backend: SystemdBackend
) -> CutoverResult:
    """Stop writers and preserve output before restoring activation."""
    filesystem_done = any(
        e["event"] == "filesystem_restored" for e in journal.events
    )
    journal.record("rollback_intent", {})
    cleanup_jobs(journal, backend)
    with effect(journal, "stop_new"):
        backend.stop_and_verify(NEW_UNIT)
    with effect(journal, "stop_all"):
        backend.stop_and_verify(NEW_UNIT)
        backend.stop_and_verify(OLD_UNIT)
        preflight = next(
            e["metadata"]
            for e in journal.events
            if e["event"] == "preflight_passed"
        )
        uids = frozenset(
            v
            for v in (preflight["old_uid"], preflight["new_uid"])
            if v is not None
        )
        backend.verify_process_ownership(journal.spec, uids, allowed_groups=())
    validate_units(journal)
    layout = layout_for(journal.spec, backend)
    publish_effect(journal, "guard_new", layout.canonical_unit, b"")
    publish_effect(journal, "guard_old", layout.legacy_unit, b"")
    with effect(journal, "reload"):
        backend.reload()
        if any(
            backend.inspect(name).load_state != "masked"
            for name in (OLD_UNIT, NEW_UNIT)
        ):
            raise CutoverError("recovery restart guards are unproven")
    for name in (OLD_UNIT, NEW_UNIT):
        backend.verify_shutdown(name)
    journal.record("all_writers_stopped", {})
    root = Path(journal.header["project"])
    with project_lease(journal) as lease:
        validate_units(journal)
        from .coordinator import MigrationOperations

        MigrationOperations(journal, backend, uids).verify_quiescence(
            root, lease
        )
        if journal.publication and not filesystem_done:
            journal.restore_publication()
        journal.record("filesystem_restored", {})
        lease.verify_identity()
        restore_ownership(journal)
        restore_writer_lock(journal, lease)
        lease.verify_identity()
        if journal.migration_attempted:
            from codereeve.migration.transaction import (
                RestorationStatus,
                restore_migration,
            )

            from .coordinator import MigrationOperations

            manifest = next(
                Path(e["metadata"]["path"])
                for e in journal.events
                if e["event"] == "effect_intent"
                and e["metadata"].get("operation") == "migrate"
            )
            with effect(journal, "restore_migration"):
                journal.storage.safe(manifest)
                try:
                    manifest.parent.lstat()
                except FileNotFoundError:
                    # The durable intent can precede child creation. An
                    # absent directory proves no retained child artifacts;
                    # original inputs must independently remain unchanged.
                    from .selection import verify_original_inputs

                    verify_original_inputs(journal, units=False)
                else:
                    restored = restore_migration(
                        manifest,
                        operations=MigrationOperations(journal, backend, uids),
                        lease=lease,
                    )
                    if restored.status not in {
                        RestorationStatus.COMPLETE,
                        RestorationStatus.NOT_NEEDED,
                    }:
                        raise CutoverError(
                            "generic migration restoration is incomplete"
                        )
        with effect(journal, "restore_selection"):
            restore_selection(journal, backend)
            backend.reload()
            snapshot = restored_snapshot(journal, backend)
        for pending in tuple(journal.interrupted):
            journal.record(
                "effect_resolved", {**pending, "resolution": "compensated"}
            )
        journal.record("selection_restored", {})
        with effect(journal, "restore_enablement"):
            restore_enablement(journal, backend, snapshot)
    with effect(journal, "restore_activation"):
        verify_enablement(backend, snapshot)
        backend.restore_activation(snapshot)
    journal.record("rolled_back", {})
    return CutoverResult("failed", journal.path, "complete")


def recover_locked(
    journal: CutoverJournal, backend: SystemdBackend
) -> CutoverResult:
    """Recover a verified journal while the caller holds cutover exclusion."""
    try:
        if journal.phase == "installed":
            validate_units(journal)
            state = backend.verify_selection(journal.spec, gate=None)
            if state.active_state != "inactive":
                raise CutoverError("installed service is unexpectedly active")
            return CutoverResult("installed", journal.path, None)
        if journal.phase == "selection_restored":
            return finish_rollback(journal, backend)
        if journal.phase in {"rolled_back", "aborted"}:
            return CutoverResult("failed", journal.path, "complete")
        if journal.phase in {"committed", "finalized"}:
            return finalize(journal, backend)
        if (
            journal.phase in {"created", "preflight_passed", "aborting"}
            and not journal.old_stop_attempted
        ):
            return abort(journal, backend)
        return rollback(journal, backend)
    except (
        CutoverError,
        MigrationError,
        JournalError,
        LeaseError,
        OSError,
        ValueError,
    ):
        return CutoverResult("incomplete", journal.path, "incomplete")


def restore_ownership(journal: CutoverJournal) -> None:
    """Reverse only recorded ownership changes, preserving the lease inode."""
    original = []
    for event in journal.events:
        if event["event"] == "rollback_intent":
            break
        metadata = event["metadata"]
        if (
            event["event"] == "effect_intent"
            and metadata.get("operation") == "ownership"
        ):
            original.append(metadata)
    for metadata in reversed(original):
        path = Path(metadata["path"])
        if path.name == ".codereeve-migration.lock":
            continue
        if not all(
            k in metadata
            for k in ("previous_mode", "previous_uid", "previous_gid")
        ):
            raise CutoverError("original ownership metadata is missing")
        mode, uid, gid = (
            metadata[k]
            for k in ("previous_mode", "previous_uid", "previous_gid")
        )
        uid = metadata.get("lock_restore_uid", uid)
        gid = metadata.get("lock_restore_gid", gid)
        completed = any(
            e["event"] == "effect_done"
            and all(
                e["metadata"].get(k) == v
                for k, v in {
                    "operation": "ownership",
                    "path": str(path),
                    "mode": mode,
                    "uid": uid,
                    "gid": gid,
                }.items()
            )
            for e in journal.events
        )
        if completed:
            if path.exists() and journal.storage.metadata(path) != (
                mode,
                uid,
                gid,
            ):
                raise CutoverError("restored ownership drifted")
            continue
        with effect(
            journal,
            "ownership",
            path=str(path),
            mode=mode,
            uid=uid,
            gid=gid,
            inode=path.lstat().st_ino,
        ):
            journal.storage.set_metadata(path, mode, uid, gid)
            if journal.storage.metadata(path) != (mode, uid, gid):
                raise CutoverError(
                    "original ownership restoration is unproven"
                )


def cleanup_jobs(journal: CutoverJournal, backend: SystemdBackend) -> None:
    """Prove every durable pending verification job has fully exited."""
    for name in tuple(journal.jobs):
        with effect(journal, "cleanup_job", unit=name):
            backend.cleanup_verification(name)
        for metadata in tuple(journal.interrupted):
            if metadata.get("unit") == name:
                journal.record(
                    "effect_resolved",
                    {**metadata, "resolution": "compensated"},
                )


def abort(journal: CutoverJournal, backend: SystemdBackend) -> CutoverResult:
    """Abort pre-stop work while preserving the old running invocation."""
    if journal.phase != "aborting":
        journal.record("abort_intent", {})
    cleanup_jobs(journal, backend)
    if journal.selection_mutated:
        validate_units(journal)
        with effect(journal, "restore_selection"):
            restore_selection(journal, backend)
        with effect(journal, "reload"):
            backend.reload()
    for metadata in tuple(journal.interrupted):
        if metadata["operation"] not in {
            "stage_secrets",
            "publish_unit",
            "publish_secrets",
            "reload",
        }:
            raise CutoverError("unresolved preflight effect")
        journal.record(
            "effect_resolved", {**metadata, "resolution": "compensated"}
        )
    journal.record("aborted", {})
    return CutoverResult("failed", journal.path, "complete")


def finalize(
    journal: CutoverJournal, backend: SystemdBackend
) -> CutoverResult:
    """Revalidate committed authority and finish only the forward release."""
    from . import readiness
    from .render import render_unit

    validate_units(journal)
    if journal.phase == "finalized":
        journal.record("revalidation_intent", {})
    recorded = next(
        e["metadata"] for e in journal.events if e["event"] == "activated"
    )
    evidence = StartEvidence(**recorded)
    backend.adopt_start(evidence)
    started = backend.inspect(NEW_UNIT)
    if started.active_state != "active" or (
        started.main_pid,
        started.invocation_id,
    ) != (evidence.pid, evidence.invocation_id):
        raise CutoverError(
            "committed invocation requires operator verification"
        )
    verify_activation(backend, started)
    layout = layout_for(journal.spec, backend)
    gate = linux_path(
        "/run/codereeve/cutover/"
        + journal.path.parent.name
        + "/committed.json"
    )
    permanent = render_unit(journal.spec).encode()
    gated = render_unit(journal.spec, gate=gate).encode()
    current = journal.storage.read(layout.canonical_unit)
    if current not in {permanent, gated}:
        raise CutoverError("committed unit selection drifted")
    if journal.pending:
        pending = dict(journal.pending)
        operation = pending["operation"]
        if operation in {"verify_job", "cleanup_job"}:
            backend.cleanup_verification(pending["unit"])
        elif operation == "publish_unit":
            from .selection import publish

            if (
                pending.get("path") != str(layout.canonical_unit)
                or pending.get("digest")
                != hashlib.sha256(permanent).hexdigest()
            ):
                raise CutoverError("committed publication intent differs")
            publish(layout.canonical_unit, permanent, journal.storage)
            current = permanent
        elif operation == "reload":
            backend.reload()
        elif operation == "release_gate":
            actual_gate = backend.target_path(gate)
            if pending.get("path") != str(actual_gate) or pending.get(
                "digest"
            ) != digest(asdict(evidence)):
                raise CutoverError("committed receipt intent differs")
            journal.record("release_deferred", {})
            pending = {}
        else:
            raise CutoverError("unknown committed effect")
        if pending:
            journal.record("effect_done", pending)
    backend.verify_selection(
        journal.spec, gate=gate if current == gated else None
    )

    def job(name: str, completed: bool) -> None:
        journal.record(
            "effect_done" if completed else "effect_intent",
            {"operation": "verify_job", "unit": name},
        )

    with backend.verification_lifecycle(job):
        observed = backend.verify_health(journal.spec, started)
    expected = next(
        e["metadata"]["provenance_digest"]
        for e in journal.events
        if e["event"] == "preflight_passed"
    )
    if digest(observed.as_dict()) != expected:
        raise CutoverError("committed artifact identity changed")
    verify_activation(backend, started)
    publish_effect(journal, "publish_unit", layout.canonical_unit, permanent)
    with effect(journal, "reload"):
        backend.reload()
        backend.verify_selection(journal.spec, gate=None)
    actual_gate = backend.target_path(gate)
    release = {
        "operation": "release_gate",
        "path": str(actual_gate),
        "digest": digest(asdict(evidence)),
    }
    if journal.deferred_release is not None:
        if journal.deferred_release != release:
            raise CutoverError("deferred receipt selection differs")
        journal.record("release_resumed", {})
    else:
        journal.record("effect_intent", release)
    actual = backend.inspect(NEW_UNIT)
    if (actual.main_pid, actual.invocation_id) != (
        evidence.pid,
        evidence.invocation_id,
    ):
        raise CutoverError("committed invocation changed before release")
    readiness.publish_commit_receipt(
        actual_gate,
        pid=evidence.pid,
        invocation_id=evidence.invocation_id,
        storage=journal.storage,
    )
    journal.record("effect_done", release)
    journal.record("finalized", {})
    return CutoverResult("committed", journal.path, None)


def finish_rollback(
    journal: CutoverJournal, backend: SystemdBackend
) -> CutoverResult:
    """Resume the final prior-selection stage without republishing data."""
    for original in journal.original_snapshots:
        if not journal.storage.matches(Path(original.path), original):
            raise CutoverError("restored original selection drifted")
    snapshot = restored_snapshot(journal, backend)
    pending = journal.pending
    if pending is not None:
        if pending["operation"] == "restore_enablement":
            restore_enablement(journal, backend, snapshot)
        elif pending["operation"] == "restore_activation":
            verify_enablement(backend, snapshot)
            backend.restore_activation(snapshot)
        else:
            raise CutoverError("unexpected final restoration effect")
        journal.record("effect_done", pending)
    if "restore_enablement" not in journal.completed:
        with effect(journal, "restore_enablement"):
            restore_enablement(journal, backend, snapshot)
    verify_enablement(backend, snapshot)
    if "restore_activation" not in journal.completed:
        with effect(journal, "restore_activation"):
            backend.restore_activation(snapshot)
    else:
        backend.restore_activation(snapshot)
    journal.record("rolled_back", {})
    return CutoverResult("failed", journal.path, "complete")


def verify_enablement(
    backend: SystemdBackend, snapshot: ServiceSnapshot
) -> None:
    """Prove restored enablement remains valid immediately before activation.

    Args:
        backend: Independent manager and filesystem enablement observation.
        snapshot: Verified original service selections.

    Raises:
        CutoverError: If restored old enablement or canonical absence drifted.
    """
    backend.verify_disabled(NEW_UNIT)
    if snapshot.old.load_state == "loaded":
        if (
            backend.inspect(OLD_UNIT).enabled_state
            != snapshot.old.enabled_state
        ):
            raise CutoverError("restored old enablement drifted")
        if snapshot.old.enabled_state == "enabled":
            return
    backend.verify_disabled(OLD_UNIT)


def restore_enablement(
    journal: CutoverJournal,
    backend: SystemdBackend,
    snapshot: ServiceSnapshot,
) -> None:
    """Compensate only the prospective link owned by durable enable intent.

    The generated unit has one persistent WantedBy link. Runtime and other
    target links are never ours; validate all of them before any removal.
    Intent remains authoritative across unlink/fsync interruptions even if
    restoring the fragment has already made the unit disappear.

    Args:
        journal: Durable original selections and exact enablement intent.
        backend: Complete persistent/runtime link observation boundary.
        snapshot: Verified original unit selections for old enablement.

    Raises:
        CutoverError: If any link or the original state is ambiguous.
    """
    if snapshot.new.enabled_state not in {"", "disabled"}:
        raise CutoverError("original canonical enablement is unsupported")
    link = backend.target_path(linux_path(NEW_ENABLEMENT))
    intents = [
        event["metadata"]
        for event in journal.events
        if event["event"] == "effect_intent"
        and event["metadata"].get("operation") == "enable_new"
    ]
    expected = {
        "operation": "enable_new",
        "path": str(link),
        "digest": digest(NEW_ENABLEMENT_TARGET),
    }
    if intents and intents != [expected]:
        raise CutoverError("canonical enablement authority is unproven")
    links = backend.enablement_links(NEW_UNIT)
    if links and (not intents or links != {link: NEW_ENABLEMENT_TARGET}):
        raise CutoverError("canonical enablement links drifted")
    journal.storage.safe(link, leaf_link=True)
    if links:
        identity = link.lstat()
        if backend.enablement_links(NEW_UNIT) != links or (
            link.lstat().st_dev,
            link.lstat().st_ino,
        ) != (identity.st_dev, identity.st_ino):
            raise CutoverError("canonical enablement changed during cleanup")
        link.unlink()
    # Flush even when replay observes absence after an interrupted unlink.
    parent = link.parent
    while not parent.exists():
        parent = parent.parent
    journal.storage.safe(parent)
    journal.storage.sync_directory(parent)
    backend.verify_disabled(NEW_UNIT)
    backend.reload()
    if snapshot.old.load_state == "loaded":
        backend.set_enabled(OLD_UNIT, snapshot.old.enabled_state == "enabled")
    else:
        backend.verify_disabled(OLD_UNIT)
    backend.verify_disabled(NEW_UNIT)
