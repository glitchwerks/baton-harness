"""Disposable persisted service model around the production coordinator.

Only model state and POSIX metadata are serialized; transaction authority
always comes from production journals on disk. This is process-death
evidence, not a systemd, cgroup, power-loss, or directory-fsync proof.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from conftest import make_cutover_context, spec, upgrade_context

from codereeve.service_cutover.coordinator import (
    cutover,
    install_only,
    recover,
)
from codereeve.service_cutover.journal import CutoverJournal
from codereeve.service_cutover.model import CutoverError, UnitState


def build(root: Path, mode: str, patch: pytest.MonkeyPatch) -> tuple:
    """Materialize a fresh host for one independently observed run."""
    context = make_cutover_context(root, spec.__wrapped__(), patch)
    if mode != "fresh":
        context = upgrade_context.__wrapped__(context)
    selected, backend, storage = context
    if mode == "installed":
        assert (
            install_only(selected, backend=backend, storage=storage).status
            == "installed"
        )
    backend.original_files = {
        str(path.relative_to(backend.filesystem_root)): path.read_bytes().hex()
        for directory in (
            "srv/project/.bh",
            "srv/project/.baton-harness",
            "opt/old",
            "etc/systemd/system",
        )
        for path in (backend.filesystem_root / directory).rglob("*")
        if path.is_file()
    }
    backend.original_links = {
        str(path.relative_to(backend.filesystem_root)): os.readlink(path)
        for path in (backend.filesystem_root / "etc/systemd/system").rglob("*")
        if path.is_symlink()
    }
    guard_old_activation(backend)
    return context


def guard_old_activation(backend: object) -> None:
    """Assert original bytes and writer exclusion before old activation."""
    original = backend.restore_activation

    def restore(snapshot: object) -> None:
        if snapshot.old.active_state == "active":
            assert backend.states[snapshot.new.name].active_state == "inactive"
            for relative, expected in backend.original_files.items():
                assert (
                    backend.filesystem_root / relative
                ).read_bytes().hex() == expected
            for relative, expected in backend.original_links.items():
                assert (
                    os.readlink(backend.filesystem_root / relative) == expected
                )
        original(snapshot)

    backend.restore_activation = restore


def journal_path(backend: object, storage: object) -> Path:
    """Select the cutover authority, excluding install-only predecessors."""
    for path in (
        backend.filesystem_root / "srv/project/.codereeve-cutover"
    ).glob("*/journal.jsonl"):
        try:
            if CutoverJournal.open(path, storage=storage).mode == "cutover":
                return path
        except CutoverError:
            return path
    raise AssertionError("no cutover journal")


def is_committed(path: Path, storage: object) -> bool:
    """Only a valid durable commit can select forward completion."""
    try:
        return any(
            e["event"] == "committed"
            for e in CutoverJournal.open(path, storage=storage).events
        )
    except CutoverError:
        return False


class Observation:
    """Observe every record and service mutation occurrence, in order."""

    def __init__(
        self,
        context: tuple,
        patch: pytest.MonkeyPatch,
        *,
        target: int = -1,
        death: bool = False,
        caught: bool = False,
        state_path: Path | None = None,
    ) -> None:
        """Install one-shot injection around production API calls."""
        self.context = context
        self.trace: list[list[str]] = []
        self.target = target
        self.death = death
        self.caught = caught
        self.state_path = state_path
        self.fired = False
        self.committed = False
        self.commit_at_failure = False
        original = CutoverJournal.record

        def record(
            journal: CutoverJournal, event: str, metadata: dict
        ) -> None:
            name = "record:" + event + ":" + str(metadata.get("operation", ""))
            self.visit(name, "before")
            original(journal, event, metadata)
            if event == "committed":
                self.committed = True
            self.visit(name, "after")

        patch.setattr(CutoverJournal, "record", record)
        backend = context[1]
        for name in (
            "stop_and_verify",
            "reload",
            "start",
            "verify_health",
            "set_enabled",
            "restore_activation",
        ):
            method = getattr(backend, name)

            def wrapped(
                *args: object,
                _name: str = name,
                _method: object = method,
                **kwargs: object,
            ) -> object:
                self.visit("service:" + _name, "before")
                result = _method(*args, **kwargs)
                self.visit("service:" + _name, "after")
                return result

            patch.setattr(backend, name, wrapped)

    def visit(self, name: str, phase: str) -> None:
        """Record the precise occurrence and require actual selected death."""
        self.trace.append([name, phase])
        if len(self.trace) - 1 != self.target or self.fired:
            return
        self.fired = True
        self.commit_at_failure = self.committed
        if self.state_path is not None:
            self.save()
        if self.death:
            os._exit(73)
        if self.caught:
            raise CutoverError("injected boundary failure")
        from conftest import Interrupted

        raise Interrupted()

    def save(self) -> None:
        """Persist external model state separately from journal authority."""
        _, backend, storage = self.context
        payload = {
            "states": {
                name: asdict(state) for name, state in backend.states.items()
            },
            "events": backend.events,
            "active_counts": backend.active_counts,
            "metadata": storage.fixture_metadata,
            "trace": self.trace,
            "fired": self.fired,
            "committed": self.committed,
            "original_files": backend.original_files,
            "original_links": backend.original_links,
        }
        with self.state_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())


def assert_recovered(
    context: tuple, committed: bool, mode: str, events_before: int
) -> None:
    """Require the exact commit side, retained backup and safe selection."""
    _, backend, storage = context
    path = journal_path(backend, storage)
    result = recover(path, backend=backend, storage=storage)
    assert max(backend.active_counts, default=0) <= 1
    try:
        journal = CutoverJournal.open(path, storage=storage)
    except CutoverError:
        assert result.status == result.recovery == "incomplete"
        assert len(backend.events) == events_before
        return
    assert path.parent.is_dir()
    for index in range(len(journal.original_snapshots)):
        assert journal.original_backup(index).is_dir()
    if committed:
        assert result.status == "committed", result
        assert journal.phase == "finalized"
        assert backend.states["codereeve.service"].active_state == "active"
        assert "old_started" not in backend.events[events_before:]
    else:
        assert result.status == "failed", result
        assert result.recovery == "complete", result
        assert backend.states["codereeve.service"].active_state == "inactive"
        if mode != "fresh":
            assert backend.states["bh-daemon.service"].active_state == "active"
            root = backend.filesystem_root
            assert (
                root / "srv/project/.baton-harness/run.log"
            ).read_bytes() == b"original runtime"
            assert (
                root / "srv/project/.bh/config.env"
            ).read_bytes() == b"BH_REPO_OWNER=owner\nBH_REPO_NAME=repo\n"
            assert (
                root / "etc/systemd/system/bh-daemon.service"
            ).read_bytes() == (
                b"[Service]\nUser=runner\nExecStart=/opt/old/bin/bh-daemon\n"
            )


def main() -> None:
    """Crash or recover in a separate interpreter with freshly loaded state."""
    action, location, mode, direction, target = sys.argv[1:]
    root = Path(location)
    state_path = root / "external-state.json"
    with pytest.MonkeyPatch.context() as patch:
        if action == "run":
            context = build(root, mode, patch)
            if direction == "reverse":
                context[1].fail_at = "health"
            observation = Observation(
                context,
                patch,
                target=int(target),
                death=True,
                state_path=state_path,
            )
            result = cutover(
                context[0], backend=context[1], storage=context[2]
            )
            assert result.status == (
                "committed" if direction == "forward" else "failed"
            )
            observation.save()
        else:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            context = make_cutover_context(
                root,
                spec.__wrapped__(),
                patch,
                initialize=False,
                metadata_by_inode={
                    int(k): tuple(v) for k, v in payload["metadata"].items()
                },
            )
            _, backend, storage = context
            backend.states = {
                name: UnitState(
                    **{**state, "dropin_paths": tuple(state["dropin_paths"])}
                )
                for name, state in payload["states"].items()
            }
            backend.events = payload["events"]
            backend.active_counts = payload["active_counts"]
            backend.original_files = payload["original_files"]
            backend.original_links = payload["original_links"]
            guard_old_activation(backend)
            path = journal_path(backend, storage)
            committed = is_committed(path, storage)
            assert committed == payload["committed"]
            assert_recovered(context, committed, mode, len(backend.events))


if __name__ == "__main__":
    main()
