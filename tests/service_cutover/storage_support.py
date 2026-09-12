"""Observe production storage primitives without replacing transactions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest
from process_support import (
    Observation,
    assert_recovered,
    build,
    is_committed,
    journal_path,
)

from codereeve.migration.journal import load_incomplete_journal
from codereeve.service_cutover.coordinator import cutover, recover
from codereeve.service_cutover.journal import CutoverJournal
from codereeve.service_cutover.model import CutoverError, FreshSecrets

_ORIGINS = {
    "codereeve.service_cutover.storage",
    "codereeve.service_cutover.selection",
    "codereeve.service_cutover.readiness",
    "codereeve.service_cutover.coordinator",
    "codereeve.service_cutover.recovery",
    "codereeve.migration.journal",
}


def build_storage_context(
    root: Path, mode: str, patch: pytest.MonkeyPatch
) -> tuple:
    """Add the distinct fresh-secret workflow to the shared host fixture."""
    return build(root, "fresh" if mode == "secrets" else mode, patch)


def execute(context: tuple, mode: str) -> object:
    """Invoke the production coordinator with optional ephemeral secrets."""
    return cutover(
        context[0],
        backend=context[1],
        storage=context[2],
        fresh_secrets=FreshSecrets(b"BWS_ACCESS_TOKEN=portable-fixture\n")
        if mode == "secrets"
        else None,
    )


def site_key(event: list[str]) -> tuple[str, str]:
    """Compare call site, record context, target role and side across modes.

    Numeric snapshot/blob indices identify repeated fixture entries, not a
    different implementation branch. All other normalized path components
    and the originating production function remain part of the key.
    """
    return re.sub(r"/\d+(?=/|$)", "/INDEX", event[0]), event[1]


def assert_storage_recovered(
    context: tuple, committed: bool, mode: str, events_before: int
) -> None:
    """Require exact recovery or independently untrusted on-disk authority."""
    _, backend, storage = context
    path = authority_path(context)
    try:
        journal = CutoverJournal.open(path, storage=storage)
    except CutoverError:
        result = recover(path, backend=backend, storage=storage)
        assert result.status == result.recovery == "incomplete"
        assert len(backend.events) == events_before
        for relative, content in backend.original_files.items():
            assert (
                backend.filesystem_root / relative
            ).read_bytes().hex() == content
        return
    child = next(
        (
            Path(e["metadata"]["path"])
            for e in journal.events
            if e["event"] == "effect_intent"
            and e["metadata"].get("operation") == "migrate"
        ),
        None,
    )
    if (
        child is not None
        and child.parent.exists()
        and load_incomplete_journal(
            child.parent / "journal.jsonl"
        ).manual_recovery
    ):
        assert not committed
        result = recover(path, backend=backend, storage=storage)
        assert result.status == result.recovery == "incomplete", result
        assert all(
            s.active_state == "inactive" for s in backend.states.values()
        )
        assert "old_started" not in backend.events[events_before:]
        for index in range(len(journal.original_snapshots)):
            journal.original_backup(index)
        assert max(backend.active_counts, default=0) <= 1
        return
    untrusted = untrusted_restoration(context, journal)
    if untrusted is not None:
        before = storage.inspect(untrusted)
        content = untrusted.read_bytes()
        for _ in range(2):
            result = recover(path, backend=backend, storage=storage)
            assert result.status == result.recovery == "incomplete", result
            assert storage.inspect(untrusted) == before
            assert untrusted.read_bytes() == content
            assert all(
                s.active_state == "inactive" for s in backend.states.values()
            )
            assert "old_started" not in backend.events[events_before:]
            for index in range(len(journal.original_snapshots)):
                journal.original_backup(index)
        assert max(backend.active_counts, default=0) <= 1
        return
    assert_recovered(
        context,
        committed,
        "fresh" if mode == "secrets" else mode,
        events_before,
    )
    if mode == "secrets":
        secret = backend.filesystem_root / "etc/codereeve/secrets.env"
        if committed:
            assert (
                secret.read_bytes() == b"BWS_ACCESS_TOKEN=portable-fixture\n"
            )
        else:
            assert not secret.exists()


def untrusted_restoration(
    context: tuple, journal: CutoverJournal
) -> Path | None:
    """Identify only injected public restore writes with untrusted bytes.

    This independently compares captured originals and public bytes/metadata;
    it does not call production partial_original or implement recovery.
    Missing nodes and matching content with original/private metadata remain
    under the strict complete-recovery oracle.
    """
    _, backend, storage = context
    fault = getattr(backend, "storage_fault", None)
    if not fault:
        return None
    name, _ = fault
    parts = name.split(":")
    if (
        parts[0] not in {"open_create", "metadata", "write", "flush", "fsync"}
        or parts[1] != "codereeve.service_cutover.storage.write"
    ):
        return None
    for index, snapshot in enumerate(journal.original_snapshots):
        path = Path(snapshot.path)
        relative = path.relative_to(backend.filesystem_root).as_posix()
        if index not in journal.restoring_inputs or not name.endswith(
            ":" + relative
        ):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        expected = next(
            (
                node
                for node in snapshot.nodes
                if node.name == "." and node.kind == "file"
            ),
            None,
        )
        if expected is None:
            continue
        known_metadata = {
            (expected.mode, expected.uid, expected.gid),
            (0o600, *storage.owner()),
        }
        if (
            hashlib.sha256(path.read_bytes()).hexdigest() != expected.digest
            or storage.metadata(path) not in known_metadata
        ):
            return path
    return None


def authority_path(context: tuple) -> Path:
    """Name authority even when interruption precedes its first file."""
    try:
        return journal_path(context[1], context[2])
    except AssertionError:
        return (
            context[1].filesystem_root
            / "srv/project/.codereeve-cutover"
            / ("0" * 32)
            / "journal.jsonl"
        )


class StorageObservation:
    """Record actual storage effects, excluding helper and read-only IO."""

    def __init__(
        self,
        context: tuple,
        patch: pytest.MonkeyPatch,
        *,
        target: int = -1,
        death: bool = False,
        state_path: Path | None = None,
    ) -> None:
        """Instrument real primitives while preserving existing observers."""
        self.context = context
        self.trace: list[list[str]] = []
        self.target = target
        self.death = death
        self.state_path = state_path
        self.fired = False
        self.committed = False
        self.commit_at_failure = False
        self.record_event = ""
        self.descriptors: dict[int, Path] = {}
        self.descriptor_origins: dict[int, str] = {}
        self.busy = False
        record = CutoverJournal.record

        def recording(
            journal: CutoverJournal, event: str, metadata: dict
        ) -> None:
            """Label internal journal publication without duplicating rows."""
            previous = self.record_event
            self.record_event = (
                event + ":" + str(metadata.get("operation", ""))
            )
            try:
                record(journal, event, metadata)
            finally:
                self.record_event = previous

        patch.setattr(CutoverJournal, "record", recording)
        opened, closed = os.open, os.close

        def open_file(
            path: object, flags: int, *args: object, **kwargs: object
        ) -> int:
            """Track descriptors and creation separately from reads."""
            origin = self.origin()
            selected = bool(origin and flags & os.O_CREAT)
            if selected:
                self.visit("open_create", "before", path, origin)
            fd = opened(path, flags, *args, **kwargs)
            if selected and flags & os.O_EXCL:
                self.created_metadata(
                    Path(path),
                    int(args[0] if args else kwargs.get("mode", 0o777)),
                )
            if origin:
                self.descriptors[fd] = Path(path)
                self.descriptor_origins[fd] = origin
            try:
                if selected:
                    self.visit("open_create", "after", path, origin)
            except BaseException:
                # Process death closes descriptors; caught injection must do
                # the same without removing any created filesystem evidence.
                closed(fd)
                self.descriptors.pop(fd, None)
                self.descriptor_origins.pop(fd, None)
                raise
            return fd

        def close_file(fd: int) -> None:
            """Forget descriptors before their number can be reused."""
            self.descriptors.pop(fd, None)
            self.descriptor_origins.pop(fd, None)
            closed(fd)

        patch.setattr(os, "open", open_file)
        patch.setattr(os, "close", close_file)
        for name in (
            "write",
            "fsync",
            "mkdir",
            "replace",
            "rename",
            "symlink",
            "unlink",
            "link",
            "rmdir",
        ):
            original = getattr(os, name)

            def primitive(
                *args: object,
                _name: str = name,
                _original: object = original,
                **kwargs: object,
            ) -> object:
                """Observe each side of one production primitive."""
                origin = self.origin()
                path = (
                    self.descriptors.get(args[0])
                    if _name in {"write", "fsync"}
                    else args[1]
                    if _name == "symlink"
                    else args[0]
                )
                if _name == "fsync" and not origin:
                    # Child-journal file sync is forwarded through the
                    # generic adapter; descriptor provenance retains it.
                    origin = self.descriptor_origins.get(args[0], "")
                if origin and path is not None:
                    self.visit(_name, "before", path, origin)
                result = _original(*args, **kwargs)
                if origin and _name == "mkdir":
                    self.created_metadata(
                        Path(path),
                        int(
                            args[1]
                            if len(args) > 1
                            else kwargs.get("mode", 0o777)
                        ),
                    )
                if origin and path is not None:
                    self.visit(_name, "after", path, origin)
                return result

            patch.setattr(os, name, primitive)
        fdopen = os.fdopen

        def stream_for(fd: int, *args: object, **kwargs: object) -> object:
            """Observe buffered write/flush calls inside real Storage.write."""
            stream = fdopen(fd, *args, **kwargs)
            origin = self.origin()
            if origin and stream.writable() and fd in self.descriptors:
                return ObservedStream(
                    stream, self, self.descriptors[fd], origin
                )
            return stream

        patch.setattr(os, "fdopen", stream_for)
        storage = context[2]
        for attribute, name in (
            ("set_metadata", "metadata"),
            ("sync_directory", "directory_sync"),
            ("sync_file", "file_sync"),
        ):
            original = getattr(storage, attribute)

            def storage_call(
                path: Path,
                *args: object,
                _name: str = name,
                _original: object = original,
                **kwargs: object,
            ) -> object:
                """Observe modeled POSIX capabilities without inventing IO."""
                origin = self.origin()
                if origin:
                    self.visit(_name, "before", path, origin)
                result = _original(path, *args, **kwargs)
                if origin:
                    self.visit(_name, "after", path, origin)
                return result

            patch.setattr(storage, attribute, storage_call)

    def created_metadata(self, path: Path, mode: int) -> None:
        """Model initial POSIX mode before the later metadata callback.

        The portable fixture otherwise defaults new directories to 0755 and
        retains metadata by inode. Creation already applies its requested
        mode and a reused inode must not inherit a deleted node's metadata.
        This updates only test model state, not native filesystem permissions.
        """
        storage = self.context[2]
        storage.fixture_metadata[path.lstat().st_ino] = (
            mode,
            *storage.owner(),
        )

    def origin(self) -> str:
        """Find the direct production caller, allowing pathlib indirection."""
        if self.busy:
            return ""
        frame = sys._getframe(2)
        while frame is not None:
            name = frame.f_globals.get("__name__", "")
            if name.startswith("pathlib") or name == "tempfile":
                frame = frame.f_back
                continue
            if (
                name == "codereeve.service_cutover.coordinator"
                and frame.f_code.co_name == "sync_directory"
            ):
                frame = frame.f_back
                continue
            return (
                name + "." + frame.f_code.co_name if name in _ORIGINS else ""
            )
        return ""

    def visit(
        self, operation: str, phase: str, path: object, origin: str
    ) -> None:
        """Record the exact occurrence and capture authority at failure."""
        if self.busy:
            return
        relative = (
            Path(path).relative_to(self.context[1].filesystem_root).as_posix()
        )
        relative = re.sub(
            r"\d{8}T\d{12}Z-[0-9a-f]{32}", "TRANSACTION", relative
        )
        relative = re.sub(r"[0-9a-f]{32}", "IDENTITY", relative)
        relative = re.sub(r"\.receipt-[^/]+", ".receipt-IDENTITY", relative)
        self.trace.append(
            [
                operation
                + ":"
                + origin
                + ":"
                + self.record_event
                + ":"
                + relative,
                phase,
            ]
        )
        if self.fired or len(self.trace) - 1 != self.target:
            return
        self.fired = True
        self.context[1].storage_fault = self.trace[-1]
        self.busy = True
        try:
            self.committed = is_committed(
                authority_path(self.context), self.context[2]
            )
            self.commit_at_failure = self.committed
            if self.state_path is not None:
                self.save()
        finally:
            self.busy = False
        if self.death:
            os._exit(73)
        raise CutoverError("injected storage primitive failure")

    def save(self) -> None:
        """Persist only model state, outside production fault observation."""
        previous = self.busy
        self.busy = True
        try:
            self.committed = is_committed(
                authority_path(self.context), self.context[2]
            )
            Observation.save(self)
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            payload["storage_fault"] = getattr(
                self.context[1], "storage_fault", None
            )
            with self.state_path.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            self.busy = previous


class ObservedStream:
    """Delegate a real buffered file while observing explicit persistence."""

    def __init__(
        self,
        stream: object,
        observation: StorageObservation,
        path: Path,
        origin: str,
    ) -> None:
        """Retain the original descriptor and its production caller."""
        self.stream = stream
        self.observation = observation
        self.path = path
        self.origin = origin

    def __enter__(self) -> ObservedStream:
        """Enter the real stream context."""
        self.stream.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        """Close exactly as the production context manager would."""
        descriptor = self.stream.fileno()
        try:
            return self.stream.__exit__(*args)
        finally:
            self.observation.descriptors.pop(descriptor, None)
            self.observation.descriptor_origins.pop(descriptor, None)

    def fileno(self) -> int:
        """Expose the real descriptor for the caller's explicit fsync."""
        return self.stream.fileno()

    def write(self, data: bytes) -> int:
        """Observe buffered data writes separately from subsequent flush."""
        self.observation.visit("write", "before", self.path, self.origin)
        result = self.stream.write(data)
        self.observation.visit("write", "after", self.path, self.origin)
        return result

    def flush(self) -> None:
        """Observe when buffered bytes reach the filesystem API."""
        self.observation.visit("flush", "before", self.path, self.origin)
        self.stream.flush()
        self.observation.visit("flush", "after", self.path, self.origin)


def main() -> None:
    """Run killed storage and fresh-process recovery on the model."""
    action, location, mode, direction, target = sys.argv[1:]
    root = Path(location)
    if action == "run":
        with pytest.MonkeyPatch.context() as patch:
            context = build_storage_context(root, mode, patch)
            if direction == "reverse":
                context[1].fail_at = "health"
            observation = StorageObservation(
                context,
                patch,
                target=int(target),
                death=True,
                state_path=root / "external-state.json",
            )
            result = execute(context, mode)
            assert result.status == (
                "committed" if direction == "forward" else "failed"
            )
            observation.save()
    else:
        # Reuse the existing reconstruction of persisted manager/POSIX
        # state. Only the authority selection/oracle differs for earlier
        # primitive failures; no transaction steps are reimplemented here.
        import process_support

        process_support.journal_path = lambda backend, storage: authority_path(
            (None, backend, storage)
        )
        payload = json.loads(
            (root / "external-state.json").read_text(encoding="utf-8")
        )

        def recovered(
            context: tuple, committed: bool, selected: str, events: int
        ) -> None:
            """Reload fault provenance only as a test oracle input."""
            context[1].storage_fault = payload["storage_fault"]
            assert_storage_recovered(context, committed, selected, events)

        process_support.assert_recovered = recovered
        process_support.main()


if __name__ == "__main__":
    main()
