"""Forward policy for durable exclusive CodeReeve service cutover."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from codereeve.migration.inventory import inventory_migration
from codereeve.migration.journal import (
    MigrationJournal,
    load_incomplete_journal,
    probe_journals,
)
from codereeve.migration.lease import LeaseError, WriterLease
from codereeve.migration.model import (
    EvidenceState,
    MigrationContext,
    MigrationEvidence,
    MigrationReport,
    MigrationStatus,
)
from codereeve.migration.transaction import (
    FileOperations,
    MigrationError,
    apply_migration,
)

from . import readiness
from .journal import CutoverJournal
from .model import CutoverError, CutoverResult, FreshSecrets, ServiceSpec
from .render import render_unit
from .selection import (
    cutover_lease,
    digest,
    effect,
    handoff,
    layout_for,
    linux_path,
    original_selection,
    project_lease,
    publish_effect,
    stage_secrets,
    verify_activation,
    verify_original_inputs,
    verify_runtime_paths,
    writer_lock_authority,
)
from .storage import Storage
from .systemd import (
    NEW_ENABLEMENT,
    NEW_ENABLEMENT_TARGET,
    NEW_UNIT,
    OLD_UNIT,
    SystemdBackend,
)


def render_only(spec: ServiceSpec) -> str:
    """Render the permanent unit without observing or changing the host."""
    return render_unit(spec)


def cutover(
    spec: ServiceSpec,
    *,
    backend: SystemdBackend | None = None,
    storage: Storage | None = None,
    fresh_secrets: FreshSecrets | None = None,
) -> CutoverResult:
    """Activate an independently verified installation under durable exclusion.

    Args:
        spec: Exact Linux service selection.
        backend: Bounded system-service context; real host by default.
        storage: Durable private filesystem operations; real host by default.
        fresh_secrets: Optional ephemeral encoded literal system secrets.

    Returns:
        Durable terminal transaction status and retained recovery path.
    """
    render_unit(spec)
    backend = backend or SystemdBackend(timeout_s=spec.timeout_s)
    storage = storage or Storage()
    backend.platform_check()
    storage.owner()
    root = backend.target_path(spec.project_root)
    with cutover_lease(root, storage):
        previous = find_previous(root, storage)
        if previous is not None and previous.phase != "installed":
            if previous.spec != spec:
                raise CutoverError("existing cutover selection differs")
            from .recovery import recover_locked

            return recover_locked(previous, backend)
        snapshot = original_selection(spec, backend)
        if previous is not None:
            from .recovery import validate_units

            if previous.spec != spec:
                raise CutoverError("installed service selection differs")
            validate_units(previous)
            backend.verify_selection(spec, gate=None)
        if snapshot.new.load_state != "not-found" and previous is None:
            raise CutoverError(
                "canonical service requires verified prior ownership"
            )
        old_account = (
            backend.account(snapshot.old.user)
            if snapshot.old.load_state == "loaded"
            else None
        )
        journal = CutoverJournal.create(
            root,
            spec,
            snapshot,
            storage=storage,
            predecessor=previous.path if previous is not None else None,
            writer_lock=writer_lock_authority(root, storage, old_account),
        )
        if previous is not None:
            journal.record("adopt_installation", {"path": str(previous.path)})

        def job(name: str, completed: bool) -> None:
            journal.record(
                "effect_done" if completed else "effect_intent",
                {"operation": "verify_job", "unit": name},
            )

        try:
            with backend.verification_lifecycle(job):
                preflight_spec = stage_secrets(journal, backend, fresh_secrets)
                evidence = backend.preflight(
                    preflight_spec,
                    owned_snapshot=snapshot if previous else None,
                )
                verify_runtime_paths(
                    backend.service_context(preflight_spec, canonical=False),
                    spec,
                    canonical=False,
                )
                uid, gid, _ = backend.account(spec.run_user)
                journal.record(
                    "preflight_passed",
                    {
                        "old_uid": old_account[0] if old_account else None,
                        "old_gid": old_account[1] if old_account else None,
                        "new_uid": uid,
                        "new_gid": gid,
                        "provenance_digest": digest(
                            evidence.provenance.as_dict()
                        ),
                    },
                )
                verify_original_inputs(journal)
                with effect(journal, "stop_old"):
                    backend.stop_and_verify(OLD_UNIT)
                journal.record("stopped", {})
                layout = layout_for(spec, backend)
                publish_effect(journal, "guard_old", layout.legacy_unit, b"")
                with effect(journal, "reload"):
                    backend.reload()
                    if backend.inspect(OLD_UNIT).load_state != "masked":
                        raise CutoverError(
                            "effective old restart guard is unproven"
                        )
                journal.record("guarded", {})
                if previous is not None:
                    import hashlib

                    with effect(
                        journal,
                        "stage_canonical",
                        path=str(layout.canonical_unit),
                        digest=hashlib.sha256(
                            storage.read(layout.canonical_unit)
                        ).hexdigest(),
                    ):
                        layout.canonical_unit.unlink()
                        storage.sync_directory(layout.canonical_unit.parent)
                    with effect(journal, "reload"):
                        backend.reload()
                        if backend.inspect(NEW_UNIT).load_state != "not-found":
                            raise CutoverError(
                                "owned canonical staging is unproven"
                            )
                lock = root / ".codereeve-migration.lock"
                with project_lease(journal) as lease:
                    backend.verify_shutdown(OLD_UNIT)
                    backend.verify_shutdown(NEW_UNIT)
                    backend.verify_process_ownership(
                        spec, evidence.service_uids, allowed_groups=()
                    )
                    verify_original_inputs(journal, units=False)
                    journals = probe_journals(root / ".codereeve-migration")
                    proof = MigrationEvidence(
                        EvidenceState.CLEAR,
                        EvidenceState.CLEAR,
                        EvidenceState.CLEAR,
                        journals.journals,
                        journals.verified_transactions,
                    )
                    context = MigrationContext(layout, evidence=proof)
                    report = inventory_migration(context)
                    if report.status == MigrationStatus.BLOCKED:
                        raise CutoverError("migration inventory is blocked")
                    if report.actions:
                        operations = MigrationOperations(
                            journal, backend, evidence.service_uids
                        )
                        applied = apply_migration(
                            context, operations=operations, lease=lease
                        )
                        journal.record(
                            "effect_done",
                            {
                                "operation": "migrate",
                                "path": str(applied.manifest_path),
                            },
                        )
                        journal.record("migrated", {})
                    else:
                        journal.record(
                            "migration_not_needed",
                            {
                                "inventory_digest": digest(report.as_dict()),
                                "action_count": 0,
                            },
                        )
                    if fresh_secrets is not None:
                        publish_effect(
                            journal,
                            "publish_secrets",
                            layout.canonical_secrets,
                            fresh_secrets.content,
                            mode=0o600,
                        )
                    handoff(journal, layout.canonical_state, uid, gid)
                    handoff(
                        journal,
                        lock,
                        uid,
                        gid,
                        lease=lease,
                    )
                    if layout.canonical_host.exists():
                        handoff(
                            journal,
                            layout.canonical_host.parent,
                            uid,
                            gid,
                            recursive=False,
                        )
                        handoff(journal, layout.canonical_host, uid, gid)
                    selected = backend.service_context(spec, canonical=True)
                    runtime_paths = verify_runtime_paths(
                        selected, spec, canonical=True
                    )
                    publication = {
                        layout.canonical_state: "runtime",
                        layout.canonical_config: "operator",
                        layout.canonical_host: "operator",
                    }
                    for path in runtime_paths:
                        publication[backend.target_path(linux_path(path))] = (
                            "runtime"
                        )
                    if spec.secrets:
                        publication[backend.target_path(spec.secrets)] = (
                            "operator"
                        )
                    journal.capture_publication(publication)
                    gate = linux_path(
                        "/run/codereeve/cutover/"
                        + journal.path.parent.name
                        + "/committed.json"
                    )
                    actual_gate = backend.target_path(gate)
                    with effect(
                        journal,
                        "prepare_gate",
                        path=str(actual_gate),
                        digest=digest(gate.as_posix()),
                    ):
                        if (
                            readiness.prepare_gate(
                                journal.path.parent.name, storage=storage
                            )
                            != actual_gate
                        ):
                            raise CutoverError(
                                "gate filesystem selection differs"
                            )
                    publish_effect(
                        journal,
                        "publish_unit",
                        layout.canonical_unit,
                        render_unit(spec, gate=gate).encode(),
                    )
                    with effect(journal, "reload"):
                        backend.reload()
                        backend.verify_selection(spec, gate=gate)
                    journal.record("published", {})
                with effect(journal, "start_new"):
                    started = backend.start(spec)
                journal.record(
                    "activated", asdict(backend.start_evidence(started))
                )
                backend.verify_selection(spec, gate=gate)
                provenance = backend.verify_health(spec, started)
                if provenance != evidence.provenance:
                    raise CutoverError("candidate artifact identity changed")
                journal.record("verified", {})
                backend.verify_disabled(NEW_UNIT)
                with effect(
                    journal,
                    "enable_new",
                    path=str(backend.target_path(linux_path(NEW_ENABLEMENT))),
                    digest=digest(NEW_ENABLEMENT_TARGET),
                ):
                    backend.verify_disabled(NEW_UNIT)
                    backend.set_enabled(NEW_UNIT, True)
                with effect(journal, "disable_old"):
                    backend.set_enabled(OLD_UNIT, False)
                backend.verify_selection(spec, gate=gate)
                verify_activation(backend, started)
                if backend.verify_health(spec, started) != evidence.provenance:
                    raise CutoverError("candidate artifact identity changed")
                verify_activation(backend, started)
                journal.record("committed", {})
                publish_effect(
                    journal,
                    "publish_unit",
                    layout.canonical_unit,
                    render_unit(spec).encode(),
                )
                with effect(journal, "reload"):
                    backend.reload()
                    backend.verify_selection(spec, gate=None)
                with effect(
                    journal,
                    "release_gate",
                    path=str(actual_gate),
                    digest=digest(asdict(backend.start_evidence(started))),
                ):
                    current = backend.inspect(NEW_UNIT)
                    if (current.main_pid, current.invocation_id) != (
                        started.main_pid,
                        started.invocation_id,
                    ):
                        raise CutoverError(
                            "candidate invocation changed before release"
                        )
                    readiness.publish_commit_receipt(
                        actual_gate,
                        pid=current.main_pid,
                        invocation_id=current.invocation_id,
                        storage=storage,
                    )
                journal.record("finalized", {})
                return CutoverResult("committed", journal.path, None)
        except (CutoverError, MigrationError, LeaseError, OSError, ValueError):
            from .recovery import recover_locked

            return recover_locked(
                CutoverJournal.open(journal.path, storage=storage), backend
            )


class MigrationOperations(FileOperations):
    """Bind generic migration to durable shutdown authority."""

    def __init__(
        self,
        journal: CutoverJournal,
        backend: SystemdBackend,
        uids: frozenset[int],
    ) -> None:
        """Retain the owning transaction and its exact service identities."""
        self.journal = journal
        self.backend = backend
        self.uids = uids

    def sync_directory(self, path: Path) -> None:
        """Use the same actual durability boundary as cutover storage."""
        self.journal.storage.sync_directory(path)

    def create_journal(
        self, root: Path, report: MigrationReport
    ) -> MigrationJournal:
        """Link the real manifest durably before generic staging can begin."""
        created = super().create_journal(root, report)
        self.journal.record(
            "effect_intent",
            {"operation": "migrate", "path": str(created.manifest_path)},
        )
        return created

    def verify_quiescence(
        self, project: Path, lease: WriterLease
    ) -> MigrationEvidence:
        """Reobserve services, external writers, and transactions."""
        if project != Path(self.journal.header["project"]):
            raise MigrationError("migration project selection differs")
        with WriterLease.hold(
            project / ".codereeve-migration.lock",
            purpose="cutover proof",
            lease=lease,
        ):
            for name in (OLD_UNIT, NEW_UNIT):
                self.backend.verify_shutdown(name)
            if self.backend.inspect(OLD_UNIT).load_state not in {
                "masked",
                "not-found",
            }:
                raise MigrationError("old restart guard is unproven")
            self.backend.verify_process_ownership(
                self.journal.spec, self.uids, allowed_groups=()
            )
            known = {
                Path(e["metadata"]["path"]).parent
                for e in self.journal.events
                if e["event"] == "effect_intent"
                and e["metadata"].get("operation") == "migrate"
            }
            root = project / ".codereeve-migration"
            verified = []
            if root.exists():
                for directory in root.iterdir():
                    state = load_incomplete_journal(
                        directory / "journal.jsonl"
                    )
                    if directory not in known and (
                        state.manual_recovery or not state.terminal
                    ):
                        raise MigrationError(
                            "unaccounted migration transaction"
                        )
                    verified.append(directory)
            return MigrationEvidence(
                EvidenceState.CLEAR,
                EvidenceState.CLEAR,
                EvidenceState.CLEAR,
                EvidenceState.CLEAR,
                tuple(verified),
            )


def find_previous(root: Path, storage: Storage) -> CutoverJournal | None:
    """Find verified transaction authority without guessing order."""
    selected = []
    transactions = root / ".codereeve-cutover"
    if transactions.exists():
        storage.private(transactions, directory=True)
        for directory in transactions.iterdir():
            journal = CutoverJournal.open(
                directory / "journal.jsonl", storage=storage
            )
            if journal.phase not in {"aborted", "rolled_back"}:
                selected.append(journal)
    authorities = {journal.path: journal for journal in selected}
    superseded = set()
    for journal in selected:
        predecessor = journal.header["predecessor"]
        if predecessor is None:
            continue
        path = Path(predecessor)
        prior = authorities.get(path)
        if (
            prior is None
            or prior.phase != "installed"
            or prior.spec != journal.spec
            or prior.header["project"] != str(root)
        ):
            raise CutoverError("installation adoption authority is invalid")
        superseded.add(path)
    selected = [
        journal for journal in selected if journal.path not in superseded
    ]
    if len(selected) > 1:
        raise CutoverError("multiple cutover authorities require recovery")
    return selected[0] if selected else None


def recover(
    journal_path: Path,
    *,
    backend: SystemdBackend | None = None,
    storage: Storage | None = None,
) -> CutoverResult:
    """Recover only from a verified durable journal under cutover exclusion."""
    backend = backend or SystemdBackend()
    storage = storage or Storage()
    try:
        journal = CutoverJournal.open(journal_path, storage=storage)
        root = Path(journal.header["project"])
        if root != backend.target_path(journal.spec.project_root):
            raise CutoverError("recovery filesystem selection differs")
        backend.platform_check()
        with cutover_lease(root, storage):
            from .recovery import recover_locked

            journal = CutoverJournal.open(journal_path, storage=storage)
            authority = find_previous(root, storage)
            if journal.phase not in {"aborted", "rolled_back"} and (
                authority is None or authority.path != journal.path
            ):
                raise CutoverError("recovery transaction authority differs")
            return recover_locked(journal, backend)
    except (CutoverError, OSError, ValueError):
        return CutoverResult("incomplete", journal_path, "incomplete")


def install_only(
    spec: ServiceSpec,
    *,
    backend: SystemdBackend | None = None,
    storage: Storage | None = None,
    fresh_secrets: FreshSecrets | None = None,
) -> CutoverResult:
    """Publish a reversible inactive unit without changing activation."""
    render_unit(spec)
    backend = backend or SystemdBackend(timeout_s=spec.timeout_s)
    storage = storage or Storage()
    backend.platform_check()
    storage.owner()
    root = backend.target_path(spec.project_root)
    with cutover_lease(root, storage):
        previous = find_previous(root, storage)
        if previous is not None:
            if previous.spec != spec or previous.phase != "installed":
                raise CutoverError("existing transaction requires recovery")
            from .recovery import validate_units

            validate_units(previous)
            backend.verify_selection(spec, gate=None)
            return CutoverResult("installed", previous.path, None)
        snapshot = original_selection(spec, backend)
        if snapshot.new.load_state != "not-found":
            raise CutoverError("canonical service already exists")
        journal = CutoverJournal.create(
            root, spec, snapshot, storage=storage, mode="install_only"
        )
        try:
            stage_secrets(journal, backend, fresh_secrets)
            layout = layout_for(spec, backend)
            if fresh_secrets is not None:
                publish_effect(
                    journal,
                    "publish_secrets",
                    layout.canonical_secrets,
                    fresh_secrets.content,
                    mode=0o600,
                )
            publish_effect(
                journal,
                "publish_unit",
                layout.canonical_unit,
                render_unit(spec).encode(),
            )
            with effect(journal, "reload"):
                backend.reload()
                state = backend.verify_selection(spec, gate=None)
                if state.active_state != "inactive":
                    raise CutoverError("installed canonical service is active")
            journal.record("installed", {})
            return CutoverResult("installed", journal.path, None)
        except (CutoverError, OSError, ValueError):
            from .recovery import recover_locked

            return recover_locked(
                CutoverJournal.open(journal.path, storage=storage), backend
            )
