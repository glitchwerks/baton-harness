"""Actual service storage mutation and persistence interruption coverage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import CutoverContext, Interrupted
from storage_support import (
    StorageObservation,
    assert_storage_recovered,
    build_storage_context,
    execute,
    site_key,
)

from codereeve.service_cutover.coordinator import cutover, recover
from codereeve.service_cutover.journal import CutoverJournal
from codereeve.service_cutover.model import CutoverError


@pytest.mark.parametrize("directory", [False, True])
def test_creation_fault_retains_initial_private_metadata(
    tmp_path: Path, directory: bool
) -> None:
    """Raw creation precedes explicit metadata but already has private mode."""
    with pytest.MonkeyPatch.context() as patch:
        context = build_storage_context(tmp_path, "fresh", patch)
        target = context[1].filesystem_root / "private-created-node"
        observation = StorageObservation(context, patch, target=1)
        with pytest.raises(CutoverError):
            if directory:
                context[2].mkdir(target)
            else:
                context[2].write(target, b"data")
        assert observation.fired
        assert context[2].metadata(target) == (
            0o700 if directory else 0o600,
            0,
            0,
        )


@pytest.mark.parametrize("direction", ["forward", "reverse"])
@pytest.mark.parametrize("mode", ["fresh", "upgrade", "installed", "secrets"])
def test_observation_reaches_service_storage_primitives(
    tmp_path: Path, direction: str, mode: str
) -> None:
    """Observe internal creation, write, replacement, and flush effects."""
    with pytest.MonkeyPatch.context() as patch:
        context = build_storage_context(tmp_path, mode, patch)
        if direction == "reverse":
            context[1].fail_at = "health"
        observation = StorageObservation(context, patch)
        result = execute(context, mode)
        assert result.status == (
            "committed" if direction == "forward" else "failed"
        )
    (tmp_path / "storage-trace.json").write_text(
        json.dumps(observation.trace), encoding="utf-8"
    )
    names = {event[0].split(":")[0] for event in observation.trace}
    assert {
        "open_create",
        "mkdir",
        "write",
        "flush",
        "fsync",
        "replace",
        "metadata",
        "directory_sync",
        "file_sync",
    } <= names
    if direction == "forward":
        assert "link" in names
        assert any(
            name.startswith("write:") and ".receipt-" in name
            for name, _ in observation.trace
        )
    if mode in {"fresh", "secrets"}:
        return
    assert any(
        name.startswith("fsync:") and "manifest.tmp" in name
        for name, _ in observation.trace
    )
    assert any(
        name.startswith("directory_sync:")
        and "codereeve.migration.journal.create" in name
        for name, _ in observation.trace
    )


def observe(root: Path, mode: str, direction: str) -> list[list[str]]:
    """Obtain the complete production sequence without fault injection."""
    with pytest.MonkeyPatch.context() as patch:
        context = build_storage_context(root, mode, patch)
        if direction == "reverse":
            context[1].fail_at = "health"
        observation = StorageObservation(context, patch)
        result = execute(context, mode)
        assert result.status == (
            "committed" if direction == "forward" else "failed"
        )
    (root / "storage-trace.json").write_text(
        json.dumps(observation.trace), encoding="utf-8"
    )
    return observation.trace


def inject_rows(
    root: Path,
    mode: str,
    direction: str,
    trace: list[list[str]],
    rows: list[int],
) -> None:
    """Require each selected occurrence to fire and use real recovery."""
    completed = []
    for index in rows:
        with pytest.MonkeyPatch.context() as patch:
            context = build_storage_context(root / str(index), mode, patch)
            if direction == "reverse":
                context[1].fail_at = "health"
            injection_patch = pytest.MonkeyPatch()
            observation = StorageObservation(
                context, injection_patch, target=index
            )
            try:
                execute(context, mode)
            except (CutoverError, OSError, ValueError):
                pass
            finally:
                injection_patch.undo()
            assert observation.fired, (index, trace[index])
            assert observation.trace[: index + 1] == trace[: index + 1], (
                index,
                trace[index],
            )
            (root / str(index) / "fault.json").write_text(
                json.dumps(
                    {
                        "index": index,
                        "event": trace[index],
                        "committed": observation.commit_at_failure,
                    }
                ),
                encoding="utf-8",
            )
            context[1].fail_at = None
            assert_storage_recovered(
                context,
                observation.commit_at_failure,
                mode,
                len(context[1].events),
            )
        completed.append(index)
        (root / "completed-rows.json").write_text(
            json.dumps(completed), encoding="utf-8"
        )


@pytest.mark.parametrize(
    "window", ["publication", "child", "reverse", "secrets"]
)
def test_focused_new_storage_windows_recover(
    tmp_path: Path, window: str
) -> None:
    """Probe new publication, child-authority and restoration seams first."""
    mode = "secrets" if window == "secrets" else "upgrade"
    direction = "reverse" if window in {"reverse", "secrets"} else "forward"
    trace = observe(tmp_path / "observe", mode, direction)
    selected: dict[tuple[str, str], int] = {}
    for index, event in enumerate(trace):
        name = event[0]
        if window == "publication":
            wanted = (
                ".codereeve-publish-" in name
                or "codereeve.service_cutover.selection.publish:" in name
            )
        elif window == "child":
            wanted = (
                "codereeve.migration.journal.create:" in name
                or "codereeve.migration.journal._private_directory:" in name
                or ":effect_intent:migrate:" in name
            )
        elif window == "reverse":
            wanted = name.startswith("rename:") or (
                ":etc/systemd/system/bh-daemon.service" in name
                and "codereeve.service_cutover.storage." in name
            )
        else:
            wanted = "secrets" in name
        if wanted:
            selected.setdefault(site_key(event), index)
    assert selected
    inject_rows(tmp_path, mode, direction, trace, list(selected.values()))


@pytest.mark.parametrize(
    "content", [b"", b"[Service]\nUs", b"operator-owned foreign input\n"]
)
def test_interrupted_public_restoration_preserves_untrusted_bytes(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    """Partial or foreign public bytes require safe manual recovery."""
    spec, backend, storage = upgrade_context
    target = backend.filesystem_root / "etc/systemd/system/bh-daemon.service"
    original_bytes = target.read_bytes()
    original = os.open
    fired = False

    def interrupted(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        """Leave the created file but emulate descriptor closure at death."""
        nonlocal fired
        descriptor = original(path, flags, *args, **kwargs)
        if Path(path) == target and flags & os.O_CREAT:
            fired = True
            os.close(descriptor)
            raise Interrupted()
        return descriptor

    backend.fail_at = "health"
    monkeypatch.setattr(os, "open", interrupted)
    with pytest.raises(Interrupted):
        cutover(spec, backend=backend, storage=storage)
    monkeypatch.setattr(os, "open", original)
    assert fired
    assert target.read_bytes() == b""
    target.write_bytes(content)
    journal = next(
        (backend.filesystem_root / "srv/project/.codereeve-cutover").glob(
            "*/journal.jsonl"
        )
    )
    authority = CutoverJournal.open(journal, storage=storage)
    index = next(
        i
        for i, snapshot in enumerate(authority.original_snapshots)
        if snapshot.path == str(target)
    )
    backup = authority.original_backup(index)
    assert (backup / "0").read_bytes() == original_bytes
    metadata = storage.metadata(target)
    for _ in range(2):
        result = recover(journal, backend=backend, storage=storage)
        assert result.status == result.recovery == "incomplete", result
        assert target.read_bytes() == content
        assert storage.metadata(target) == metadata
        assert (
            authority.original_backup(index) / "0"
        ).read_bytes() == original_bytes
        assert all(
            s.active_state == "inactive" for s in backend.states.values()
        )
        assert "old_started" not in backend.events


def additional_rows(
    trace: list[list[str]], covered: set[tuple[str, str]]
) -> list[int]:
    """Include every occurrence at a site not proved by an earlier mode."""
    rows = [
        i for i, event in enumerate(trace) if site_key(event) not in covered
    ]
    covered.update(site_key(event) for event in trace)
    return rows


@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_every_observed_service_storage_effect_recovers(
    tmp_path: Path, direction: str
) -> None:
    """Exhaust upgrade plus distinct fresh/installed/secret sites."""
    covered: set[tuple[str, str]] = set()
    inventory = {}
    for mode in ("upgrade", "fresh", "installed", "secrets"):
        root = tmp_path / mode
        trace = observe(root / "observe", mode, direction)
        rows = additional_rows(trace, covered)
        if mode == "upgrade":
            assert rows == list(range(len(trace)))
        inventory[mode] = {"observed": len(trace), "selected": rows}
        (tmp_path / "coverage.json").write_text(
            json.dumps(inventory), encoding="utf-8"
        )
        inject_rows(root, mode, direction, trace, rows)
        with pytest.MonkeyPatch.context() as patch:
            context = build_storage_context(root / "exhaustion", mode, patch)
            if direction == "reverse":
                context[1].fail_at = "health"
            observation = StorageObservation(context, patch, target=len(trace))
            result = execute(context, mode)
            assert result.status == (
                "committed" if direction == "forward" else "failed"
            )
            assert not observation.fired
            assert observation.trace == trace


def run_storage_child(
    action: str, root: Path, mode: str, direction: str, target: int
) -> subprocess.CompletedProcess:
    """Execute only disposable storage/model code in a separate interpreter."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'tests/service_cutover'); "
            "from storage_support import main; main()",
            action,
            str(root),
            mode,
            direction,
            str(target),
        ],
        capture_output=True,
        timeout=120,
    )


@pytest.mark.parametrize("direction", ["forward", "reverse"])
@pytest.mark.skipif(
    sys.platform != "linux",
    reason="exhaustive storage process-death matrix requires Linux",
)
def test_linux_process_death_at_each_covered_storage_occurrence(
    tmp_path: Path, direction: str
) -> None:
    """Execute the storage plan with killed and fresh interpreters."""
    covered: set[tuple[str, str]] = set()
    for mode in ("upgrade", "fresh", "installed", "secrets"):
        root = tmp_path / mode
        baseline = root / "observe"
        observed = run_storage_child("run", baseline, mode, direction, -1)
        assert observed.returncode == 0, observed.stderr.decode()
        trace = json.loads((baseline / "external-state.json").read_text())[
            "trace"
        ]
        assert trace
        rows = additional_rows(trace, covered)
        if mode == "upgrade":
            assert rows == list(range(len(trace)))
        for index in rows:
            location = root / str(index)
            child = run_storage_child("run", location, mode, direction, index)
            assert child.returncode == 73, (index, child.stderr.decode())
            payload = json.loads(
                (location / "external-state.json").read_text()
            )
            assert payload["fired"]
            assert payload["trace"] == trace[: index + 1]
            restored = run_storage_child(
                "recover", location, mode, direction, -1
            )
            assert restored.returncode == 0, (
                index,
                trace[index],
                restored.stderr.decode(),
            )
        exhausted = run_storage_child(
            "run", root / "exhaustion", mode, direction, len(trace)
        )
        assert exhausted.returncode == 0, exhausted.stderr.decode()
        payload = json.loads(
            (root / "exhaustion/external-state.json").read_text()
        )
        assert not payload["fired"]
        assert payload["trace"] == trace


@pytest.mark.parametrize(
    "window", ["publication", "child", "reverse", "receipt"]
)
def test_portable_storage_process_death_smoke(
    tmp_path: Path, window: str
) -> None:
    """Prove representative internal effects survive actual process loss."""
    direction = "reverse" if window == "reverse" else "forward"
    mode = "upgrade"
    baseline = tmp_path / "observe"
    observed = run_storage_child("run", baseline, mode, direction, -1)
    assert observed.returncode == 0, observed.stderr.decode()
    trace = json.loads((baseline / "external-state.json").read_text())["trace"]
    matches = {
        "publication": lambda name: name.startswith(
            "replace:codereeve.service_cutover.selection.publish:"
        ),
        "child": lambda name: (
            name.startswith("open_create:codereeve.migration.journal.create:")
            and "manifest.tmp" in name
        ),
        "reverse": lambda name: (
            name.startswith(
                "open_create:codereeve.service_cutover.storage.write:"
            )
            and name.endswith(":etc/systemd/system/bh-daemon.service")
        ),
        "receipt": lambda name: name.startswith(
            "link:codereeve.service_cutover.readiness.publish_commit_receipt:"
        ),
    }
    index = next(
        i
        for i, (name, phase) in enumerate(trace)
        if phase == "after" and matches[window](name)
    )
    location = tmp_path / "crash"
    child = run_storage_child("run", location, mode, direction, index)
    assert child.returncode == 73, child.stderr.decode()
    payload = json.loads((location / "external-state.json").read_text())
    assert payload["fired"]
    assert payload["trace"] == trace[: index + 1]
    restored = run_storage_child("recover", location, mode, direction, -1)
    assert restored.returncode == 0, restored.stderr.decode()
