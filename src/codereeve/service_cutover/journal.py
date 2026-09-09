"""Checksummed, closed-schema service recovery journal.

The coordinator supplies service proofs. This module enforces ordering and
retains unresolved intents; a filename alone never completes an effect.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, cast

from codereeve.service_cutover.model import (
    CutoverError,
    ServiceSnapshot,
    ServiceSpec,
    UnitState,
)
from codereeve.service_cutover.storage import (
    FileSnapshot,
    Node,
    Storage,
    fixed_errors,
)

_TERMINAL = {"aborted", "rolled_back", "finalized", "installed"}
# Effects are legal only in their explicitly declared transaction phases.
_VERIFICATION_PHASES = {
    "created",
    "migrated",
    "published",
    "activated",
    "verified",
    "committed",
}
_OPERATIONS = {
    "stage_secrets": {"created"},
    "prepare_gate": {"migrated"},
    "capture_publication": {"migrated"},
    "quarantine": {"all_writers_stopped"},
    "restore_publication": {"all_writers_stopped"},
    "verify_job": _VERIFICATION_PHASES,
    "cleanup_job": _VERIFICATION_PHASES | {"aborting", "rolling_back"},
    "stop_old": {"preflight_passed"},
    "guard_old": {"stopped", "rolling_back"},
    "guard_new": {"rolling_back"},
    "stage_canonical": {"stopped", "guarded"},
    "migrate": {"guarded"},
    "ownership": {
        "migrated",
        "published",
        "rolling_back",
        "filesystem_restored",
    },
    "publish_unit": {"created", "migrated", "published", "committed"},
    "publish_secrets": {"created", "migrated"},
    "reload": {
        "aborting",
        "created",
        "stopped",
        "guarded",
        "migrated",
        "published",
        "committed",
        "rolling_back",
    },
    "start_new": {"published"},
    "enable_new": {"verified"},
    "disable_old": {"verified"},
    "release_gate": {"committed"},
    "stop_new": {"rolling_back"},
    "stop_all": {"rolling_back"},
    "restore_migration": {"filesystem_restored"},
    "restore_selection": {"filesystem_restored", "aborting"},
    "restore_enablement": {"selection_restored"},
    "restore_activation": {"selection_restored"},
}
_TRANSITIONS = {
    "preflight_passed": ({"created"}, set()),
    "stopped": ({"preflight_passed"}, {"stop_old"}),
    "guarded": ({"stopped"}, {"guard_old", "reload"}),
    "migrated": ({"guarded"}, {"migrate"}),
    "published": ({"migrated"}, {"publish_unit", "reload"}),
    "activated": ({"published"}, {"start_new"}),
    "verified": ({"activated"}, set()),
    "committed": ({"verified"}, {"enable_new", "disable_old"}),
    "finalized": ({"committed"}, {"release_gate", "publish_unit", "reload"}),
    "aborted": ({"aborting"}, set()),
    "all_writers_stopped": ({"rolling_back"}, {"stop_all"}),
    "filesystem_restored": ({"all_writers_stopped"}, set()),
    "selection_restored": ({"filesystem_restored"}, {"restore_selection"}),
    "rolled_back": (
        {"selection_restored"},
        {"restore_enablement", "restore_activation"},
    ),
    "installed": ({"created"}, {"publish_unit", "reload"}),
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_UNIT = re.compile(r"codereeve-verify-[a-z0-9-]{1,80}\.service\Z")


def _json(value: object) -> bytes:
    """Produce canonical checksum input without permissive numeric values."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _hash(value: object) -> str:
    """Hash a canonical metadata object."""
    return hashlib.sha256(_json(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse duplicate keys, including in nested JSON metadata."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CutoverError("duplicate journal field")
        result[key] = value
    return result


def _path(value: object) -> str:
    """Validate a declared filesystem selection without copying its values."""
    if (
        not isinstance(value, str)
        or not value
        or any(ord(c) < 32 for c in value)
    ):
        raise CutoverError("invalid journal path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CutoverError("invalid journal path")
    return value


def _integer(value: object) -> bool:
    """Accept nonnegative exact integers only, excluding JSON booleans."""
    return type(value) is int and value >= 0


def _unit(state: UnitState) -> dict[str, object]:
    """Persist only validated state fields and a command-selection digest."""
    safe = {
        key: value
        for key, value in asdict(state).items()
        if key not in {"exec_start", "exec_start_argv", "job"}
    }
    safe["selection_digest"] = _hash([state.exec_start, state.exec_start_argv])
    safe["dropin_paths"] = list(state.dropin_paths)
    _validate_unit(safe)
    return safe


def _validate_unit(value: object) -> None:
    """Reject malformed manager identities before accepting recovery."""
    keys = {
        "name",
        "load_state",
        "active_state",
        "enabled_state",
        "main_pid",
        "invocation_id",
        "control_group",
        "fragment_path",
        "dropin_paths",
        "kill_mode",
        "user",
        "sub_state",
        "selection_digest",
        "environment_digest",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise CutoverError("invalid service snapshot")
    if (
        not isinstance(value["environment_digest"], str)
        or value["environment_digest"]
        and (
            not isinstance(value["environment_digest"], str)
            or not _HEX.fullmatch(value["environment_digest"])
        )
    ):
        raise CutoverError("invalid effective environment digest")
    allowed = {
        "name": {"bh-daemon.service", "codereeve.service"},
        "load_state": {"loaded", "masked", "not-found"},
        "active_state": {
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
        },
        "enabled_state": {
            "",
            "enabled",
            "disabled",
            "static",
            "masked",
            "masked-runtime",
            "enabled-runtime",
            "indirect",
            "not-found",
        },
        "kill_mode": {"", "control-group", "mixed", "process", "none"},
        "sub_state": {
            "",
            "running",
            "dead",
            "failed",
            "exited",
            "start",
            "stop",
            "stop-sigterm",
            "stop-sigkill",
            "auto-restart",
        },
    }
    if any(value[key] not in choices for key, choices in allowed.items()):
        raise CutoverError("invalid service snapshot state")
    if not _integer(value["main_pid"]) or not isinstance(
        value["invocation_id"], str
    ):
        raise CutoverError("invalid service process identity")
    if value["invocation_id"] and not re.fullmatch(
        r"[0-9a-f]{32}", value["invocation_id"]
    ):
        raise CutoverError("invalid service invocation identity")
    if value["main_pid"] and (
        not value["invocation_id"] or not value["control_group"]
    ):
        raise CutoverError("incomplete service process identity")
    if value["load_state"] == "not-found" and (
        value["main_pid"]
        or value["fragment_path"]
        or value["active_state"] != "inactive"
    ):
        raise CutoverError("inconsistent absent service")
    if not isinstance(value["user"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]*", value["user"]
    ):
        raise CutoverError("invalid service account metadata")
    for key in ("control_group", "fragment_path"):
        if value[key]:
            # Manager paths are Linux paths even in portable storage tests.
            if (
                not isinstance(value[key], str)
                or not value[key].startswith("/")
                or ".." in value[key].split("/")
                or any(ord(c) < 32 for c in value[key])
            ):
                raise CutoverError("invalid service selection metadata")
    if not isinstance(value["dropin_paths"], list) or any(
        not isinstance(p, str) or not p.startswith("/") or ".." in p.split("/")
        for p in value["dropin_paths"]
    ):
        raise CutoverError("invalid service drop-in metadata")
    if not isinstance(value["selection_digest"], str) or not _HEX.fullmatch(
        value["selection_digest"]
    ):
        raise CutoverError("invalid service selection digest")


def _snapshot(value: object) -> FileSnapshot:
    """Decode a closed tree schema; prevent path escape through node names."""
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "nodes"}
        or not isinstance(value["nodes"], list)
    ):
        raise CutoverError("invalid filesystem snapshot")
    path = _path(value["path"])
    nodes = []
    names: set[str] = set()
    directories: set[str] = set()
    for raw in value["nodes"]:
        if not isinstance(raw, dict) or set(raw) != {
            "name",
            "kind",
            "mode",
            "uid",
            "gid",
            "digest",
        }:
            raise CutoverError("invalid filesystem node")
        name, kind = raw["name"], raw["kind"]
        if (
            not isinstance(name, str)
            or name in names
            or "\\" in name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or (not names and name != ".")
        ):
            raise CutoverError("unsafe filesystem node")
        if names and Path(name).parent.as_posix() not in directories:
            raise CutoverError("unordered filesystem node")
        if kind not in {"file", "directory", "mask"} or (
            kind == "mask" and name != "."
        ):
            raise CutoverError("unsupported filesystem node")
        if (
            any(not _integer(raw[k]) for k in ("mode", "uid", "gid"))
            or raw["mode"] > 0o7777
        ):
            raise CutoverError("invalid filesystem ownership")
        if (
            not isinstance(raw["digest"], str)
            or (kind == "file" and not _HEX.fullmatch(raw["digest"]))
            or (kind != "file" and raw["digest"])
        ):
            raise CutoverError("invalid filesystem digest")
        names.add(name)
        if kind == "directory":
            directories.add(name)
        nodes.append(Node(**raw))
    return FileSnapshot(path, tuple(nodes))


def _spec_metadata(spec: ServiceSpec) -> dict[str, object]:
    """Serialize declared paths without environment values."""
    return {
        key: value.as_posix() if isinstance(value, PurePath) else value
        for key, value in asdict(spec).items()
    }


def _service_spec(value: object) -> ServiceSpec:
    """Validate the exact declarative service specification on recovery."""
    if not isinstance(value, dict) or set(value) != {
        "project_root",
        "environment",
        "run_user",
        "workflow",
        "secrets",
        "home",
        "timeout_s",
    }:
        raise CutoverError("invalid saved service specification")
    paths: dict[str, Path | None] = {}
    for key in ("project_root", "environment", "workflow", "secrets", "home"):
        raw = value[key]
        if raw is None and key in {"workflow", "secrets"}:
            paths[key] = None
        elif isinstance(raw, str):
            paths[key] = cast(Path, PurePosixPath(raw))
        else:
            raise CutoverError("invalid saved service path")
    return ServiceSpec(
        cast(Path, paths["project_root"]),
        cast(Path, paths["environment"]),
        value["run_user"],
        paths["workflow"],
        paths["secrets"],
        cast(Path, paths["home"]),
        value["timeout_s"],
    )


class CutoverJournal:
    """Durable single-writer journal held under the coordinator's lease."""

    def __init__(self, path: Path, storage: Storage) -> None:
        """Initialize replay state; use create/open to obtain authority."""
        self.path = path
        self.storage = storage
        self.phase = ""
        self.mode = "cutover"
        self.pending: dict[str, Any] | None = None
        self.interrupted: list[dict[str, Any]] = []
        self.completed: set[str] = set()
        self.jobs: set[str] = set()
        self.events: list[dict[str, Any]] = []
        self.header: dict[str, Any] = {}
        self.publication: list[dict[str, Any]] = []
        self.deferred_release: dict[str, Any] | None = None
        self.writer_lock_identity: tuple[int, int] | None = None
        self.restoring_inputs: dict[int, FileSnapshot] = {}
        self.publication_restored = False
        self.selection_mutated = False
        self.old_stop_attempted = False
        self.migration_attempted = False
        self._poisoned = False

    @classmethod
    def create(
        cls,
        root: Path,
        spec: ServiceSpec,
        snapshot: ServiceSnapshot,
        *,
        storage: Storage | None = None,
        mode: str = "cutover",
        predecessor: Path | None = None,
        writer_lock: dict[str, object] | None = None,
    ) -> CutoverJournal:
        """Retain original inputs privately before any service mutation."""
        store = storage or Storage()
        try:
            if predecessor is not None:
                prior = cls.open(predecessor, storage=store)
                if (
                    mode != "cutover"
                    or prior.phase != "installed"
                    or prior.spec != spec
                    or prior.header["project"] != str(root)
                ):
                    raise CutoverError(
                        "installation adoption authority is invalid"
                    )
            if mode not in {"cutover", "install_only"}:
                raise CutoverError("unknown service transaction mode")
            base = root / ".codereeve-cutover"
            if not base.exists():
                store.mkdir(base)
            store.private(base, directory=True)
            directory = base / uuid.uuid4().hex
            store.mkdir(directory)
            store.mkdir(directory / "original")
            original = []
            if len(set(snapshot.files)) != len(snapshot.files):
                raise CutoverError("duplicate original file selection")
            for index, path in enumerate(snapshot.files):
                backup = directory / "original" / str(index)
                store.mkdir(backup)
                captured = store.capture(
                    path,
                    backup,
                    mask=path.name
                    in {"bh-daemon.service", "codereeve.service"},
                )
                original.append(captured.metadata())
            header = {
                "version": 1,
                "mode": mode,
                "project": str(root),
                "spec": _spec_metadata(spec),
                "spec_digest": _hash(_spec_metadata(spec)),
                "old": _unit(snapshot.old),
                "new": _unit(snapshot.new),
                "original": original,
                "writer_lock": writer_lock,
                "predecessor": str(predecessor)
                if predecessor is not None
                else None,
            }
            journal = cls(directory / "journal.jsonl", store)
            store.write(journal.path, b"")
            journal.record("created", header)
            return journal
        except (OSError, ValueError, TypeError):
            raise CutoverError("journal creation incomplete") from None

    @classmethod
    def open(
        cls, path: Path, *, storage: Storage | None = None
    ) -> CutoverJournal:
        """Replay only complete checksummed records and verified backups."""
        store = storage or Storage()
        try:
            store.private(path.parent.parent, directory=True)
            store.private(path.parent, directory=True)
            store.private(path)
            journal = cls(path, store)
            data = store.read(path)
            if not data or not data.endswith(b"\n"):
                raise CutoverError("truncated service journal")
            previous = "0" * 64
            for number, line in enumerate(data.splitlines()):
                record = json.loads(line, object_pairs_hook=_pairs)
                if not isinstance(record, dict) or set(record) != {
                    "sequence",
                    "previous",
                    "event",
                    "metadata",
                    "checksum",
                }:
                    raise CutoverError("unknown service journal record")
                checksum = record.pop("checksum")
                if (
                    type(record["sequence"]) is not int
                    or record["sequence"] != number
                    or record["previous"] != previous
                    or _hash(record) != checksum
                ):
                    raise CutoverError("service journal checksum mismatch")
                journal._accept(record["event"], record["metadata"])
                record["checksum"] = checksum
                journal.events.append(record)
                previous = checksum
            if (
                path.name != "journal.jsonl"
                or path.parent.parent.name != ".codereeve-cutover"
                or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
                or Path(journal.header["project"]) != path.parent.parent.parent
            ):
                raise CutoverError(
                    "journal location does not match transaction"
                )
            store.private(path.parent / "original", directory=True)
            if journal.publication:
                store.private(path.parent / "publication", directory=True)
            for index, raw in enumerate(journal.header["original"]):
                store.verify_backup(
                    _snapshot(raw), path.parent / "original" / str(index)
                )
            for index, raw in enumerate(journal.publication):
                store.verify_backup(
                    _snapshot(raw["snapshot"]),
                    path.parent / "publication" / str(index),
                )
            return journal
        except (OSError, ValueError, TypeError, KeyError):
            raise CutoverError("service journal recovery refused") from None

    @fixed_errors
    def record(self, event: str, metadata: Mapping[str, object]) -> None:
        """Append validated intent/completion and fsync before returning."""
        if self._poisoned:
            raise CutoverError("reopen journal after durability failure")
        # Replay into a separate state before changing disk or live authority.
        probe = CutoverJournal(self.path, self.storage)
        for existing in self.events:
            probe._accept(existing["event"], existing["metadata"])
        copied = json.loads(_json(dict(metadata)))
        probe._accept(event, copied)
        record = {
            "sequence": len(self.events),
            "previous": self.events[-1]["checksum"]
            if self.events
            else "0" * 64,
            "event": event,
            "metadata": copied,
        }
        record["checksum"] = _hash(record)
        try:
            self.storage.private(self.path)
            expected = b"".join(_json(item) + b"\n" for item in self.events)
            if self.storage.read(self.path) != expected:
                raise CutoverError("journal changed outside transaction")
            temporary = self.path.parent / ("record-" + uuid.uuid4().hex)
            self.storage.write(temporary, expected + _json(record) + b"\n")
            self.storage.replace_journal(temporary, self.path)
        except (OSError, CutoverError):
            self._poisoned = True
            raise CutoverError("journal durability incomplete") from None
        self._accept(event, copied)
        self.events.append(record)

    def _accept(self, event: str, metadata: object) -> None:
        """Validate one event's schema and recovery-order prerequisites."""
        if not isinstance(event, str) or not isinstance(metadata, dict):
            raise CutoverError("invalid journal event")
        if event == "created":
            if (
                self.phase
                or set(metadata)
                != {
                    "version",
                    "mode",
                    "project",
                    "spec_digest",
                    "spec",
                    "old",
                    "new",
                    "original",
                    "predecessor",
                    "writer_lock",
                }
                or type(metadata["version"]) is not int
                or metadata["version"] != 1
                or metadata["mode"] not in {"cutover", "install_only"}
            ):
                raise CutoverError("unknown journal manifest")
            _path(metadata["project"])
            lock = metadata["writer_lock"]
            if lock is not None:
                keys = {
                    "path",
                    "exists",
                    "inode",
                    "device",
                    "mode",
                    "uid",
                    "gid",
                    "restore_uid",
                    "restore_gid",
                }
                if (
                    not isinstance(lock, dict)
                    or set(lock) != keys
                    or type(lock["exists"]) is not bool
                ):
                    raise CutoverError("invalid writer lock authority")
                _path(lock["path"])
                if (
                    Path(lock["path"])
                    != Path(metadata["project"]) / ".codereeve-migration.lock"
                    or any(
                        not _integer(lock[k])
                        for k in (
                            "mode",
                            "uid",
                            "gid",
                            "restore_uid",
                            "restore_gid",
                        )
                    )
                    or lock["mode"] > 0o7777
                ):
                    raise CutoverError("invalid writer lock selection")
                if lock["exists"]:
                    if any(
                        not _integer(lock[k]) for k in ("inode", "device")
                    ) or (lock["restore_uid"], lock["restore_gid"]) != (
                        lock["uid"],
                        lock["gid"],
                    ):
                        raise CutoverError(
                            "invalid existing writer lock authority"
                        )
                elif (
                    lock["inode"] is not None
                    or lock["device"] is not None
                    or (lock["mode"], lock["uid"], lock["gid"])
                    != (0o600, 0, 0)
                ):
                    raise CutoverError("invalid absent writer lock authority")
            predecessor = metadata["predecessor"]
            if predecessor is not None:
                _path(predecessor)
                selected = Path(predecessor)
                if (
                    metadata["mode"] != "cutover"
                    or selected.name != "journal.jsonl"
                    or selected.parent.parent
                    != Path(metadata["project"]) / ".codereeve-cutover"
                    or selected == self.path
                    or re.fullmatch(r"[0-9a-f]{32}", selected.parent.name)
                    is None
                ):
                    raise CutoverError("invalid installation predecessor")
            if not isinstance(
                metadata["spec_digest"], str
            ) or not _HEX.fullmatch(metadata["spec_digest"]):
                raise CutoverError("invalid service specification digest")
            _service_spec(metadata["spec"])
            if _hash(metadata["spec"]) != metadata["spec_digest"]:
                raise CutoverError("saved service specification changed")
            _validate_unit(metadata["old"])
            _validate_unit(metadata["new"])
            if (
                metadata["old"]["name"] != "bh-daemon.service"
                or metadata["new"]["name"] != "codereeve.service"
                or not isinstance(metadata["original"], list)
            ):
                raise CutoverError("invalid original selection")
            for raw in metadata["original"]:
                _snapshot(raw)
            self.header, self.mode, self.phase = (
                metadata,
                metadata["mode"],
                "created",
            )
            return
        if event in {"release_deferred", "release_resumed"}:
            if self.phase != "committed" or metadata:
                raise CutoverError("invalid deferred receipt authority")
            if event == "release_deferred":
                if (
                    self.deferred_release is not None
                    or self.pending is None
                    or self.pending.get("operation") != "release_gate"
                ):
                    raise CutoverError(
                        "receipt deferral has no exact pending intent"
                    )
                self.deferred_release = self.pending
                self.pending = None
            else:
                if self.pending is not None or self.deferred_release is None:
                    raise CutoverError(
                        "receipt resumption has no exact deferred intent"
                    )
                self.pending = self.deferred_release
                self.deferred_release = None
            return
        if event == "finalized" and self.deferred_release is not None:
            raise CutoverError("receipt authority remains deferred")
        if event == "writer_lock_acquired":
            if (
                self.phase not in {"guarded", "all_writers_stopped"}
                or self.pending
                or self.writer_lock_identity is not None
                or self.header["writer_lock"] is None
                or set(metadata) != {"inode", "device"}
                or any(not _integer(v) for v in metadata.values())
            ):
                raise CutoverError("invalid acquired writer lock identity")
            lock = self.header["writer_lock"]
            if lock["exists"] and (metadata["device"], metadata["inode"]) != (
                lock["device"],
                lock["inode"],
            ):
                raise CutoverError("acquired writer lock differs")
            self.writer_lock_identity = (metadata["device"], metadata["inode"])
            return
        if event in {"restore_input_intent", "restore_input_done"}:
            index = metadata.get("index")
            if (
                self.phase not in {"filesystem_restored", "aborting"}
                or not self.pending
                or self.pending.get("operation") != "restore_selection"
                or type(index) is not int
                or index < 0
                or index >= len(self.header["original"])
            ):
                raise CutoverError("invalid input restoration authority")
            if event == "restore_input_intent":
                if (
                    set(metadata) != {"index", "snapshot"}
                    or index in self.restoring_inputs
                ):
                    raise CutoverError("duplicate input restoration authority")
                input_snapshot = _snapshot(metadata["snapshot"])
                if (
                    input_snapshot.path
                    != self.header["original"][index]["path"]
                ):
                    raise CutoverError("input restoration selection differs")
                self.restoring_inputs[index] = input_snapshot
            else:
                if (
                    set(metadata) != {"index"}
                    or index not in self.restoring_inputs
                ):
                    raise CutoverError("unknown input restoration completion")
                del self.restoring_inputs[index]
            return
        if (
            event in {"selection_restored", "aborted"}
            and self.restoring_inputs
        ):
            raise CutoverError("input restoration remains incomplete")
        if event == "revalidation_intent":
            if (
                self.phase != "finalized"
                or metadata
                or self.pending
                or self.interrupted
                or self.jobs
            ):
                raise CutoverError("unsafe finalized revalidation")
            self.phase = "committed"
            self.completed.clear()
            return
        if not self.phase or self.phase in _TERMINAL:
            raise CutoverError("terminal or uninitialized service journal")
        if event in {"effect_intent", "effect_done", "effect_cancelled"}:
            self._effect(event, metadata)
            return
        if event == "adopt_installation":
            if (
                self.phase != "created"
                or self.mode != "cutover"
                or self.pending
                or set(metadata) != {"path"}
            ):
                raise CutoverError("invalid installation adoption")
            _path(metadata["path"])
            if metadata["path"] != self.header["predecessor"]:
                raise CutoverError("installation adoption selection differs")
            if any(e["event"] == "adopt_installation" for e in self.events):
                raise CutoverError("duplicate installation adoption")
            return
        if event == "effect_resolved":
            resolved = dict(metadata)
            resolution = resolved.pop("resolution", None)
            if (
                resolution
                not in {
                    "observed_completed",
                    "observed_not_applied",
                    "compensated",
                }
                or resolved not in self.interrupted
            ):
                raise CutoverError("invalid interrupted effect resolution")
            self.interrupted.remove(resolved)
            if resolved["operation"] in {"verify_job", "cleanup_job"}:
                self.jobs.discard(resolved["unit"])
            return
        if self.pending and event not in {"abort_intent", "rollback_intent"}:
            raise CutoverError("unresolved service effect")
        if event == "abort_intent":
            if (
                metadata
                or self.phase not in {"created", "preflight_passed"}
                or self.old_stop_attempted
            ):
                raise CutoverError("unsafe preflight abort")
            if self.pending:
                self.interrupted.append(self.pending)
                self.pending = None
            self.phase = "aborting"
            self.completed.clear()
            return
        if event == "rollback_intent":
            if metadata or self.phase in {"created", "aborting", "committed"}:
                raise CutoverError("unsafe rollback phase")
            if self.pending:
                self.interrupted.append(self.pending)
                self.pending = None
            self.phase = "rolling_back"
            self.completed.clear()
            return
        if event == "migration_not_needed":
            if (
                self.phase != "guarded"
                or self.mode != "cutover"
                or self.migration_attempted
                or self.jobs
                or self.interrupted
                or set(metadata) != {"inventory_digest", "action_count"}
                or type(metadata["action_count"]) is not int
                or metadata["action_count"] != 0
                or not isinstance(metadata["inventory_digest"], str)
                or not _HEX.fullmatch(metadata["inventory_digest"])
            ):
                raise CutoverError("invalid no-action migration proof")
            self.phase = "migrated"
            self.completed.clear()
            return
        if event == "publication_restored":
            if (
                metadata
                or self.phase != "all_writers_stopped"
                or not self.publication
            ):
                raise CutoverError("invalid publication verification phase")
            self.publication_restored = True
            return
        if event == "publication":
            if (
                "capture_publication" not in self.completed
                or self.phase != "migrated"
                or self.publication
                or set(metadata) != {"files"}
                or not isinstance(metadata["files"], list)
            ):
                raise CutoverError("invalid publication capture")
            for entry in metadata["files"]:
                if (
                    not isinstance(entry, dict)
                    or set(entry) != {"role", "snapshot"}
                    or entry["role"] not in {"runtime", "operator"}
                ):
                    raise CutoverError("invalid publication role")
                _snapshot(entry["snapshot"])
            self.publication = metadata["files"]
            return
        if event == "preflight_passed":
            if (
                set(metadata)
                != {
                    "old_uid",
                    "old_gid",
                    "new_uid",
                    "new_gid",
                    "provenance_digest",
                }
                or any(
                    not _integer(metadata[k]) for k in ("new_uid", "new_gid")
                )
                or not isinstance(metadata["provenance_digest"], str)
                or not _HEX.fullmatch(metadata["provenance_digest"])
            ):
                raise CutoverError("invalid preflight identity proof")
            old_identity = (metadata["old_uid"], metadata["old_gid"])
            if self.header["old"]["load_state"] == "not-found":
                if old_identity != (None, None):
                    raise CutoverError(
                        "absent service has fabricated identity"
                    )
            elif not all(_integer(value) for value in old_identity):
                raise CutoverError("missing prior service identity")
        elif event == "activated":
            if (
                set(metadata) != {"pid", "invocation_id", "started_ns"}
                or any(
                    not _integer(metadata[k]) or metadata[k] == 0
                    for k in ("pid", "started_ns")
                )
                or not isinstance(metadata["invocation_id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", metadata["invocation_id"])
            ):
                raise CutoverError("invalid startup identity proof")
        elif metadata:
            raise CutoverError("unexpected journal event metadata")
        if event not in _TRANSITIONS:
            raise CutoverError("unknown service event")
        phases, required = _TRANSITIONS[event]
        if (
            self.phase not in phases
            or not required <= self.completed
            or self.jobs
        ):
            raise CutoverError("illegal service transition")
        if (
            event
            in {
                "aborted",
                "rolled_back",
                "committed",
                "installed",
                "selection_restored",
            }
            and self.interrupted
        ):
            raise CutoverError("interrupted effects require reconciliation")
        if (
            event == "aborted"
            and self.selection_mutated
            and not {"restore_selection", "reload"} <= self.completed
        ):
            raise CutoverError("published selection restoration required")
        if event == "installed" and self.mode != "install_only":
            raise CutoverError("invalid install-only completion")
        if self.mode == "install_only" and event not in {
            "installed",
            "aborted",
        }:
            raise CutoverError("activation prohibited for install-only")
        if (
            event == "filesystem_restored"
            and self.publication
            and not self.publication_restored
        ):
            raise CutoverError("publication restoration proof required")
        if event == "published" and not self.publication:
            raise CutoverError("publication snapshot required")
        self.phase = event
        self.completed.clear()

    def _effect(self, event: str, metadata: dict[str, Any]) -> None:
        """Validate bounded effect metadata and match exact pending intent."""
        operation = metadata.get("operation")
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            raise CutoverError("unknown service operation")
        if (
            event == "effect_done"
            and operation == "restore_selection"
            and self.restoring_inputs
        ):
            raise CutoverError("input restoration remains incomplete")
        required = {"operation"}
        fields = {
            "quarantine": {"index", "path", "snapshot"},
            "restore_publication": {"index"},
            "verify_job": {"unit"},
            "cleanup_job": {"unit"},
            "migrate": {"path"},
            "stage_secrets": {"path", "digest"},
            "prepare_gate": {"path", "digest"},
            "ownership": {"path", "uid", "gid", "mode", "inode"},
        }
        required |= fields.get(operation, set())
        previous = {"previous_mode", "previous_uid", "previous_gid"}
        if operation == "ownership" and previous & set(metadata):
            required |= previous
        if operation == "ownership" and {
            "lock_restore_uid",
            "lock_restore_gid",
        } & set(metadata):
            required |= {"lock_restore_uid", "lock_restore_gid"}
            if Path(
                metadata["path"]
            ).name != ".codereeve-migration.lock" or not previous <= set(
                metadata
            ):
                raise CutoverError("invalid new lock restoration authority")
        if operation in {
            "publish_unit",
            "publish_secrets",
            "guard_old",
            "guard_new",
            "stage_canonical",
            "release_gate",
            "enable_new",
        } and ({"path", "digest"} & set(metadata)):
            required |= {"path", "digest"}
        if set(metadata) != required:
            raise CutoverError("invalid service effect metadata")
        for key, value in metadata.items():
            if key == "unit" and (
                not isinstance(value, str) or not _UNIT.fullmatch(value)
            ):
                raise CutoverError("invalid verification job identity")
            if key == "path":
                _path(value)
            if key == "digest" and (
                not isinstance(value, str) or not _HEX.fullmatch(value)
            ):
                raise CutoverError("invalid publication selection digest")
            if key == "snapshot":
                _snapshot(value)
            if key in {
                "index",
                "uid",
                "gid",
                "mode",
                "inode",
                "previous_mode",
                "previous_uid",
                "previous_gid",
                "lock_restore_uid",
                "lock_restore_gid",
            } and not _integer(value):
                raise CutoverError("invalid ownership effect")
        if event == "effect_intent":
            if (
                operation == "release_gate"
                and self.deferred_release is not None
            ):
                raise CutoverError("receipt authority is already deferred")
            if self.pending or self.phase not in _OPERATIONS[operation]:
                raise CutoverError("illegal service effect")
            if (
                self.phase == "created"
                and self.mode == "cutover"
                and operation
                not in {"verify_job", "cleanup_job", "stage_secrets"}
            ):
                raise CutoverError(
                    "service mutation requires preflight shutdown"
                )
            if self.mode == "install_only" and operation not in {
                "stage_secrets",
                "restore_selection",
                "publish_unit",
                "publish_secrets",
                "reload",
            }:
                raise CutoverError("activation prohibited for install-only")
            if operation == "verify_job":
                self.jobs.add(metadata["unit"])
            if (
                operation == "restore_selection"
                and self.phase == "filesystem_restored"
                and self.migration_attempted
                and "restore_migration" not in self.completed
            ):
                raise CutoverError(
                    "migration restoration must precede selection"
                )
            if operation == "stop_old":
                self.old_stop_attempted = True
            if operation == "migrate":
                self.migration_attempted = True
            if operation in {"publish_unit", "publish_secrets"}:
                self.selection_mutated = True
            self.pending = metadata
        else:
            if self.pending != metadata:
                raise CutoverError("service effect does not match intent")
            # Cancellation means the coordinator proved no uncleaned effect.
            if event == "effect_done":
                self.completed.add(operation)
            if operation in {"verify_job", "cleanup_job"}:
                self.jobs.discard(metadata["unit"])
            self.pending = None

    @fixed_errors
    def capture_publication(self, paths: Mapping[Path, str]) -> None:
        """Capture migration outputs; nested operator selections take priority.

        Args:
            paths: Complete paths with either runtime or operator ownership.
                Runtime ancestors may contain protected operator children.
        """
        if not paths or self.publication or self.phase != "migrated":
            raise CutoverError("invalid publication capture phase")
        for path, role in paths.items():
            _path(str(path))
            if role not in {"runtime", "operator"}:
                raise CutoverError("invalid publication ownership role")
        effect: dict[str, object] = {"operation": "capture_publication"}
        self.record("effect_intent", effect)
        directory = self.path.parent / "publication"
        self.storage.mkdir(directory)
        captured = []
        for index, (path, role) in enumerate(paths.items()):
            backup = directory / str(index)
            self.storage.mkdir(backup)
            snapshot = self.storage.capture(path, backup)
            captured.append({"role": role, "snapshot": snapshot.metadata()})
        # Recheck every source after copying the complete selection.
        for entry in captured:
            snapshot = _snapshot(entry["snapshot"])
            if not self.storage.matches(Path(snapshot.path), snapshot):
                raise CutoverError("publication source changed")
        self.record("effect_done", effect)
        self.record("publication", {"files": captured})

    def verify_original(self) -> None:
        """Refuse unrelated edits to any original unit/config selection."""
        for raw in self.header["original"]:
            snapshot = _snapshot(raw)
            if not self.storage.matches(Path(snapshot.path), snapshot):
                raise CutoverError("original selection drift")

    @fixed_errors
    def restore_publication(self) -> None:
        """Preserve runtime changes before exact generic-migration rollback.

        Requires all-writer shutdown proof. Checks every protected selection
        before any runtime move. Interrupted effects remain pending and are
        resumed only after verifying the recorded pre-effect representation.
        """
        if self.phase != "all_writers_stopped" or not self.publication:
            raise CutoverError("publication restoration lacks shutdown proof")
        for entry in self.publication:
            snapshot = _snapshot(entry["snapshot"])
            if entry["role"] == "operator" and not self._protected_matches(
                snapshot
            ):
                raise CutoverError("operator-controlled publication drift")
        for index, entry in enumerate(self.publication):
            if entry["role"] != "runtime":
                continue
            snapshot = _snapshot(entry["snapshot"])
            source = Path(snapshot.path)
            # An ancestor's exact restoration includes nested runtime paths.
            if any(
                other["role"] == "runtime"
                and source != Path(other["snapshot"]["path"])
                and source.is_relative_to(Path(other["snapshot"]["path"]))
                for other in self.publication
            ):
                continue
            backup = self.path.parent / "publication" / str(index)
            self.storage.verify_backup(snapshot, backup)
            if self.pending and self.pending.get("operation") not in {
                "quarantine",
                "restore_publication",
            }:
                raise CutoverError("unresolved non-storage recovery effect")
            if self.pending and self.pending.get("index") != index:
                # Earlier entries already restored may be safely rechecked.
                if self.storage.matches(source, snapshot):
                    continue
                raise CutoverError("unexpected pending publication effect")
            if self.pending and self.pending["operation"] == "quarantine":
                intent = dict(self.pending)
                preserved = _snapshot(intent["snapshot"])
                target = Path(intent["path"])
                self._quarantine_directory(target.parent)
                if source.exists() or source.is_symlink():
                    if (
                        not self.storage.matches(source, preserved)
                        or target.exists()
                    ):
                        raise CutoverError("quarantine source drift")
                    self.storage.move(source, target)
                self.storage.make_durable(
                    target, preserved, namespace_parents=(source.parent,)
                )
                self.record("effect_done", intent)
            if self.pending is None and self.storage.matches(source, snapshot):
                self.storage.make_durable(source, snapshot)
                continue
            if self.pending is None and (
                source.exists() or source.is_symlink()
            ):
                preserved = self.storage.inspect(source)
                target = (
                    source.parent
                    / ".codereeve-cutover"
                    / self.path.parent.name
                    / f"quarantine-{index}"
                )
                intent = {
                    "operation": "quarantine",
                    "index": index,
                    "path": str(target),
                    "snapshot": preserved.metadata(),
                }
                self.record("effect_intent", intent)
                self._quarantine_directory(target.parent)
                self.storage.move(source, target)
                self.storage.make_durable(
                    target, preserved, namespace_parents=(source.parent,)
                )
                self.record("effect_done", intent)
            effect = {"operation": "restore_publication", "index": index}
            if self.pending is None:
                self.record("effect_intent", effect)
            elif self.pending != effect:
                raise CutoverError("unexpected publication restoration intent")
            # Existing partial output is retained and never overwritten.
            if not self.storage.matches(source, snapshot):
                if source.exists() or source.is_symlink():
                    self.storage.resume_restore(snapshot, backup)
                else:
                    self.storage.restore(snapshot, backup)
            self.storage.make_durable(source, snapshot)
            self.record("effect_done", effect)
        for entry in self.publication:
            snapshot = _snapshot(entry["snapshot"])
            if not self.storage.matches(Path(snapshot.path), snapshot):
                raise CutoverError("publication restoration incomplete")

        if not self.publication_restored:
            self.record("publication_restored", {})

    def _quarantine_directory(self, directory: Path) -> None:
        """Create private source-filesystem recovery storage after intent."""
        for path in (directory.parent, directory):
            if not path.exists():
                self.storage.mkdir(path)
            self.storage.private(path, directory=True)
            self.storage.sync_directory(path)
            self.storage.sync_directory(path.parent)

    def _protected_matches(self, snapshot: FileSnapshot) -> bool:
        """Verify a protected child in place or in its recorded quarantine."""
        path = Path(snapshot.path)
        if path.exists() or path.is_symlink() or not snapshot.nodes:
            return self.storage.matches(path, snapshot)
        for event in reversed(self.events):
            meta = event["metadata"]
            if (
                event["event"] != "effect_intent"
                or meta.get("operation") != "quarantine"
            ):
                continue
            original = Path(meta["snapshot"]["path"])
            if path.is_relative_to(original):
                target = Path(meta["path"]) / path.relative_to(original)
                return self.storage.matches(target, snapshot)
        return False

    @property
    def spec(self) -> ServiceSpec:
        """Return validated immutable recovery inputs."""
        return _service_spec(self.header["spec"])

    @property
    def original_snapshots(self) -> tuple[FileSnapshot, ...]:
        """Return immutable originals indexed by private backup directory."""
        return tuple(_snapshot(raw) for raw in self.header["original"])

    def original_backup(self, index: int) -> Path:
        """Return one verified private backup directory by snapshot index."""
        if not _integer(index) or index >= len(self.original_snapshots):
            raise CutoverError("invalid original backup index")
        path = self.path.parent / "original" / str(index)
        self.storage.verify_backup(self.original_snapshots[index], path)
        return path
