"""Exact file selections and journaled publication for service cutover."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import cast

from codereeve.migration.lease import LeaseError, WriterLease
from codereeve.paths import PathLayout

from .journal import CutoverJournal
from .model import (
    CutoverError,
    FreshSecrets,
    ServiceSnapshot,
    ServiceSpec,
    UnitState,
)
from .storage import FileSnapshot, Node, Storage
from .systemd import NEW_UNIT, OLD_UNIT, SystemdBackend


def digest(value: object) -> str:
    """Hash canonical metadata without persisting private values."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def linux_path(value: str) -> Path:
    """Preserve strict Linux target spelling independently of the test host."""
    return cast(Path, PurePosixPath(value))


def layout_for(spec: ServiceSpec, backend: SystemdBackend) -> PathLayout:
    """Resolve fixed product locations against the explicit actual root."""
    return PathLayout.for_environment(
        backend.target_path(spec.project_root),
        {},
        home=backend.target_path(spec.home),
        etc_root=backend.target_path(linux_path("/etc")),
    )


@contextmanager
def effect(
    journal: CutoverJournal, operation: str, **metadata: object
) -> Iterator[None]:
    """Persist effect intent and verified completion."""
    record = {"operation": operation, **metadata}
    journal.record("effect_intent", record)
    yield
    journal.record("effect_done", record)


def observe(backend: SystemdBackend, name: str) -> UnitState:
    """Attest the loaded selection without logging its environment."""
    state = backend.inspect(name)
    if state.load_state == "loaded":
        state = replace(
            state,
            environment_digest=digest(backend.effective_environment(name)),
        )
    return state


def original_selection(
    spec: ServiceSpec, backend: SystemdBackend
) -> ServiceSnapshot:
    """Select mutable operator inputs separately from runtime output trees."""
    layout = layout_for(spec, backend)
    old, new = observe(backend, OLD_UNIT), observe(backend, NEW_UNIT)
    files = [
        layout.legacy_unit,
        layout.canonical_unit,
        layout.legacy_config,
        layout.canonical_config,
        layout.legacy_host,
        layout.canonical_host,
        layout.legacy_secrets,
        layout.canonical_secrets,
    ]
    if old.load_state == "loaded":
        selected = backend.effective_environment(OLD_UNIT)
        try:
            assignments = shlex.split(selected["Environment"])
            values = {}
            for assignment in assignments:
                key, separator, value = assignment.partition("=")
                if not separator or key in values:
                    raise ValueError
                values[key] = value
            actual_home = values.get(
                "HOME", backend.account(old.user)[2].as_posix()
            )
            if (
                actual_home != spec.home.as_posix()
                or selected["WorkingDirectory"] != spec.project_root.as_posix()
            ):
                raise ValueError
            files_selection = selected["EnvironmentFiles"]
            allowed = {
                "",
                "/etc/bh-daemon/secrets.env (ignore_errors=no)",
                "/etc/codereeve/secrets.env (ignore_errors=no)",
            }
            if (
                files_selection not in allowed
                or values.get(
                    "XDG_CONFIG_HOME", spec.home.as_posix() + "/.config"
                )
                != spec.home.as_posix() + "/.config"
            ):
                raise ValueError
        except ValueError:
            raise CutoverError(
                "original HOME or input selection is unsupported"
            ) from None
        environment = backend.legacy_environment(old)
        script = backend.target_path(linux_path(old.exec_start))
        first_line = script.read_text(encoding="utf-8").partition("\n")[0]
        interpreter = backend.target_path(linux_path(first_line[2:]))
        binary = interpreter.resolve(strict=True)
        files.append(binary)
        arguments = old.exec_start_argv.split()
        if len(arguments) == 3:
            files.append(backend.target_path(linux_path(arguments[2])))
        files.extend(
            (
                script,
                environment / "pyvenv.cfg",
            )
        )
    from codereeve.config_env import parse_env_text

    for path in files[2:8]:
        if path.exists():
            for parsed in parse_env_text(
                path.read_text(encoding="utf-8"), source="service input"
            ):
                if (
                    parsed.key == "HOME"
                    and parsed.value != spec.home.as_posix()
                ):
                    raise CutoverError("original HOME selection differs")
                if (
                    parsed.key == "XDG_CONFIG_HOME"
                    and parsed.value != spec.home.as_posix() + "/.config"
                ):
                    raise CutoverError(
                        "host configuration selection is unsupported"
                    )
    return ServiceSnapshot(old, new, tuple(files))


def publish(
    path: Path, data: bytes, storage: Storage, *, mode: int = 0o644
) -> None:
    """Durably replace an already-authorized selected regular file or mask."""
    storage.safe(path, leaf_link=True)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        storage.mkdir(directory)
        storage.set_metadata(directory, 0o755, 0, 0)
        storage.sync_directory(directory.parent)
    temporary = path.with_name(".codereeve-publish-" + uuid.uuid4().hex)
    storage.write(temporary, data)
    storage.set_metadata(temporary, mode, 0, 0)
    storage.sync_file(temporary)
    os.replace(temporary, path)
    storage.sync_file(path)
    storage.sync_directory(path.parent)
    if storage.read(path) != data or storage.metadata(path) != (mode, 0, 0):
        raise CutoverError("published service selection is unproven")


def publish_effect(
    journal: CutoverJournal,
    operation: str,
    path: Path,
    data: bytes,
    *,
    mode: int = 0o644,
) -> None:
    """Record exact publication bytes before changing an owned selection."""
    from .recovery import validate_units

    validate_units(journal)
    if operation == "publish_secrets":
        original = next(
            s for s in journal.original_snapshots if s.path == str(path)
        )
        if not journal.storage.matches(path, original):
            raise CutoverError("fresh secret publication target changed")
    with effect(
        journal,
        operation,
        path=str(path),
        digest=hashlib.sha256(data).hexdigest(),
    ):
        publish(path, data, journal.storage, mode=mode)


def handoff(
    journal: CutoverJournal,
    path: Path,
    uid: int,
    gid: int,
    *,
    lease: WriterLease | None = None,
    lock_restore: tuple[int, int] | None = None,
    recursive: bool = True,
) -> None:
    """Journal an in-place ownership transfer without replacing the inode."""
    if path.name == ".codereeve-migration.lock":
        if lease is None or lease.path != path.absolute():
            raise CutoverError(
                "writer lease handoff requires retained authority"
            )
        lease.verify_identity()
        mode, old_uid, old_gid = journal.storage.metadata(path)
        snapshot = FileSnapshot(
            str(path), (Node(".", "file", mode, old_uid, old_gid, "0" * 64),)
        )
    else:
        snapshot = journal.storage.inspect(path)
    extra = (
        {}
        if lock_restore is None
        else {
            "lock_restore_uid": lock_restore[0],
            "lock_restore_gid": lock_restore[1],
        }
    )
    for node in snapshot.nodes:
        if not recursive and node.name != ".":
            continue
        target = path if node.name == "." else path / node.name
        inode = target.lstat().st_ino
        with effect(
            journal,
            "ownership",
            path=str(target),
            uid=uid,
            gid=gid,
            mode=node.mode,
            inode=inode,
            previous_mode=node.mode,
            previous_uid=node.uid,
            previous_gid=node.gid,
            **extra,
        ):
            journal.storage.set_metadata(target, node.mode, uid, gid)
            if target.lstat().st_ino != inode or journal.storage.metadata(
                target
            ) != (node.mode, uid, gid):
                raise CutoverError("service ownership handoff is unproven")
            if lease is not None:
                lease.verify_identity()


def stage_secrets(
    journal: CutoverJournal,
    backend: SystemdBackend,
    fresh: FreshSecrets | None,
) -> ServiceSpec:
    """Stage private bytes or select existing compatible secrets."""
    spec = journal.spec
    layout = layout_for(spec, backend)
    if fresh is None:
        if (
            spec.secrets is not None
            and not backend.target_path(spec.secrets).exists()
            and layout.legacy_secrets.exists()
        ):
            return replace(
                spec, secrets=linux_path("/etc/bh-daemon/secrets.env")
            )
        return spec
    if (
        spec.secrets != linux_path("/etc/codereeve/secrets.env")
        or layout.canonical_secrets.exists()
        or layout.legacy_secrets.exists()
    ):
        raise CutoverError(
            "fresh secrets require an absent canonical selection"
        )
    path = journal.path.parent / "preflight.env"
    with effect(
        journal,
        "stage_secrets",
        path=str(path),
        digest=hashlib.sha256(fresh.content).hexdigest(),
    ):
        journal.storage.write(path, fresh.content)
        journal.storage.private(path)
    logical = linux_path(
        "/" + path.relative_to(backend.filesystem_root).as_posix()
    )
    return replace(spec, secrets=logical)


def verify_runtime_paths(
    context: dict[str, object], spec: ServiceSpec, *, canonical: bool
) -> tuple[str, ...]:
    """Refuse runtime destinations outside supported managed state roots."""
    allowed: tuple[PurePosixPath, ...] = (
        PurePosixPath(spec.project_root.as_posix()) / ".codereeve",
    )
    if not canonical:
        allowed += (
            PurePosixPath(spec.project_root.as_posix()) / ".baton-harness",
        )
    paths = context.get("runtime_paths")
    if not isinstance(paths, list) or not paths:
        raise CutoverError("runtime publication selection is unavailable")
    for value in paths:
        if not isinstance(value, str) or not any(
            PurePosixPath(value).is_relative_to(root) for root in allowed
        ):
            raise CutoverError(
                "external runtime publication path is unsupported"
            )
    return tuple(paths)


def verify_activation(backend: SystemdBackend, started: UnitState) -> None:
    """Require retained invocation, enablement, and exclusive old shutdown."""
    actual = backend.inspect(NEW_UNIT)
    if (
        actual.active_state,
        actual.main_pid,
        actual.invocation_id,
        actual.enabled_state,
    ) != ("active", started.main_pid, started.invocation_id, "enabled"):
        raise CutoverError("candidate activation authority changed")
    backend.verify_shutdown(OLD_UNIT)
    if backend.inspect(OLD_UNIT).load_state != "masked":
        raise CutoverError("old restart guard is unproven")
    backend.verify_disabled(OLD_UNIT)


def verify_original_inputs(
    journal: CutoverJournal, *, units: bool = True
) -> None:
    """Require snapshot inputs still match before destructive migration."""
    for snapshot in journal.original_snapshots:
        path = Path(snapshot.path)
        if not units and path.name in {OLD_UNIT, NEW_UNIT}:
            continue
        if not journal.storage.matches(path, snapshot):
            raise CutoverError("original operator selection drifted")


@contextmanager
def cutover_lease(root: Path, storage: Storage) -> Iterator[WriterLease]:
    """Retain root-owned cutover exclusion without reading lock contents."""
    path = root / ".codereeve-cutover.lock"
    try:
        storage.safe(path)
        with WriterLease.acquire(path, purpose="service cutover") as lease:
            if storage.metadata(path) != (0o600, 0, 0):
                raise CutoverError("cutover lock ownership is unproven")
            lease.verify_identity()
            yield lease
            lease.verify_identity()
    except LeaseError:
        raise CutoverError("cutover writer exclusion is unavailable") from None
