"""Production cutover coordination against disposable service contexts."""

from __future__ import annotations

import importlib

import pytest
from conftest import CutoverContext

from codereeve.service_cutover import model
from codereeve.service_cutover.model import (
    CutoverError,
    ServiceSpec,
    UnitState,
)
from codereeve.service_cutover.systemd import PreflightEvidence


def test_fresh_secrets_are_private_and_literal() -> None:
    """Secret handoff must never print contents or accept shell syntax."""
    assert hasattr(model, "FreshSecrets")
    secret = model.FreshSecrets(b"BWS_ACCESS_TOKEN=abc-123_=/+\n")
    assert "abc-123" not in repr(secret)
    for data in (
        b"",
        b"BWS_ACCESS_TOKEN=$(bad)\n",
        b"HOME=/other\n",
        b"BWS_ACCESS_TOKEN=a b\n",
        b"BWS_ACCESS_TOKEN=x\nBWS_ACCESS_TOKEN=y\n",
    ):
        with pytest.raises(CutoverError):
            model.FreshSecrets(data)


def test_fresh_cutover_runs_real_transaction_and_commits(
    cutover_context: CutoverContext,
) -> None:
    """A fresh installation must preserve writer exclusion at each effect."""
    assert importlib.util.find_spec("codereeve.service_cutover.coordinator")
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = cutover_context
    result = cutover(spec, backend=backend, storage=storage)
    assert result.status == "committed"
    assert max(backend.active_counts) <= 1
    from codereeve.service_cutover.journal import CutoverJournal

    journal = CutoverJournal.open(result.journal_path, storage=storage)
    assert journal.phase == "finalized"
    assert any(
        event["event"] == "migration_not_needed" for event in journal.events
    )


def test_upgrade_links_real_migration_before_data_changes(
    upgrade_context: CutoverContext,
) -> None:
    """Migrate legacy data while retaining the original environment."""
    from codereeve.service_cutover.coordinator import cutover
    from codereeve.service_cutover.journal import CutoverJournal

    spec, backend, storage = upgrade_context
    old = backend.filesystem_root / "opt/old/bin/bh-daemon"
    before = old.read_bytes(), old.stat().st_ino, old.stat().st_mtime_ns
    result = cutover(spec, backend=backend, storage=storage)
    assert result.status == "committed"
    assert (
        backend.filesystem_root / "srv/project/.codereeve/run.log"
    ).read_bytes() == b"original runtime"
    assert (
        old.read_bytes(),
        old.stat().st_ino,
        old.stat().st_mtime_ns,
    ) == before
    journal = CutoverJournal.open(result.journal_path, storage=storage)
    assert journal.migration_attempted
    assert max(backend.active_counts) <= 1


@pytest.mark.parametrize("fail", [False, True])
def test_fresh_secrets_are_staged_privately_and_rollback_owned(
    cutover_context: CutoverContext, fail: bool
) -> None:
    """Actual preflight sees only journal-owned staging before publication."""
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = cutover_context
    original = backend.preflight
    seen = []

    def preflight(
        selected: ServiceSpec, **kwargs: object
    ) -> PreflightEvidence:
        assert selected.secrets != spec.secrets
        stage = backend.target_path(selected.secrets)
        assert stage.read_bytes() == b"BWS_ACCESS_TOKEN=synthetic:+/=token\n"
        assert storage.metadata(stage) == (0o600, 0, 0)
        seen.append(stage)
        return original(selected, **kwargs)

    backend.preflight = preflight
    if fail:
        backend.fail_at = "health"
    result = cutover(
        spec,
        backend=backend,
        storage=storage,
        fresh_secrets=model.FreshSecrets(
            b"BWS_ACCESS_TOKEN=synthetic:+/=token\n"
        ),
    )
    assert seen
    assert b"synthetic" not in result.journal_path.read_bytes()
    if fail:
        assert result.recovery == "complete"
        assert not backend.target_path(spec.secrets).exists()
    else:
        assert result.status == "committed"
        assert (
            backend.target_path(spec.secrets).read_bytes()
            == seen[0].read_bytes()
        )


def test_install_only_then_cutover_preserves_real_migration_coexistence(
    upgrade_context: CutoverContext,
) -> None:
    """An owned inactive unit must be staged away before generic inventory."""
    from codereeve.service_cutover import coordinator

    assert hasattr(coordinator, "install_only")
    spec, backend, storage = upgrade_context
    installed = coordinator.install_only(
        spec, backend=backend, storage=storage
    )
    assert installed.status == "installed"
    assert not any(
        e.startswith("stop:") or e == "start_new" for e in backend.events
    )
    result = coordinator.cutover(spec, backend=backend, storage=storage)
    assert result.status == "committed"
    from codereeve.service_cutover.journal import CutoverJournal

    journal = CutoverJournal.open(result.journal_path, storage=storage)
    assert any(
        e["metadata"].get("operation") == "stage_canonical"
        for e in journal.events
    )
    assert max(backend.active_counts) <= 1
    assert (
        coordinator.cutover(spec, backend=backend, storage=storage).status
        == "committed"
    )


def test_old_home_change_refuses_before_journal_or_jobs(
    upgrade_context: CutoverContext,
) -> None:
    """Do not silently migrate a different account's host configuration."""
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = upgrade_context
    original = backend.effective_environment
    backend.effective_environment = lambda name: {
        **original(name),
        "Environment": "HOME=/home/other",
    }
    with pytest.raises(CutoverError):
        cutover(spec, backend=backend, storage=storage)
    assert "preflight" not in backend.events
    assert not (
        backend.filesystem_root / "srv/project/.codereeve-cutover"
    ).exists()


def test_external_runtime_override_refuses_before_old_stop(
    upgrade_context: CutoverContext,
) -> None:
    """Unsupported runtime destinations cannot evade publication coverage."""
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = upgrade_context
    original = backend.service_context
    backend.service_context = lambda selected, canonical: {
        **original(selected, canonical=canonical),
        "runtime_paths": ["/opt/old/unmanaged-output"],
    }
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert not any(e.startswith("stop:") for e in backend.events)


def test_identity_replacement_during_enable_never_commits(
    cutover_context: CutoverContext,
) -> None:
    """The verified candidate must remain the same invocation at commit."""
    from dataclasses import replace

    from codereeve.service_cutover.coordinator import cutover
    from codereeve.service_cutover.journal import CutoverJournal

    spec, backend, storage = cutover_context
    original = backend.set_enabled

    def enable(name: str, enabled: bool) -> UnitState:
        result = original(name, enabled)
        if name == "codereeve.service":
            backend.states[name] = replace(
                result, main_pid=77, invocation_id="c" * 32
            )
        return result

    backend.set_enabled = enable
    result = cutover(spec, backend=backend, storage=storage)
    assert not any(
        e["event"] == "committed"
        for e in CutoverJournal.open(
            result.journal_path, storage=storage
        ).events
    )


def test_fresh_secrets_reject_control_record_separators() -> None:
    """ASCII record delimiters cannot become an unquoted token value."""
    for value in (b"TOKEN=a\x1cOTHER=b", b"TOKEN=a\x1d", b"TOKEN=a\x1e"):
        with pytest.raises(CutoverError):
            model.FreshSecrets(value)


def test_operator_input_change_during_preflight_refuses_shutdown(
    upgrade_context: CutoverContext,
) -> None:
    """Snapshot authority cannot overwrite a later operator configuration."""
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = upgrade_context
    path = backend.filesystem_root / "srv/project/.bh/config.env"
    original = backend.preflight

    def changed(selected: ServiceSpec, **kwargs: object) -> PreflightEvidence:
        result = original(selected, **kwargs)
        path.write_bytes(b"BH_REPO_NAME=operator-change\n")
        return result

    backend.preflight = changed
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "complete"
    assert not any(e.startswith("stop:") for e in backend.events)
    assert path.read_bytes() == b"BH_REPO_NAME=operator-change\n"


def test_adoption_requires_existing_same_project_installation(
    cutover_context: CutoverContext,
) -> None:
    """A durable adoption reference must name verified installed authority."""
    from codereeve.service_cutover.coordinator import find_previous
    from codereeve.service_cutover.journal import CutoverJournal
    from codereeve.service_cutover.selection import original_selection

    spec, backend, storage = cutover_context
    root = backend.target_path(spec.project_root)
    journal = CutoverJournal.create(
        root, spec, original_selection(spec, backend), storage=storage
    )
    with pytest.raises(CutoverError, match="adoption"):
        journal.record(
            "adopt_installation", {"path": str(root / "missing/journal.jsonl")}
        )
    assert find_previous(root, storage).path == journal.path


def test_host_configuration_handoff_is_narrow(
    cutover_context: CutoverContext,
) -> None:
    """Transfer selected private host inputs without changing sibling files."""
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = cutover_context
    directory = backend.filesystem_root / "home/runner/.config/codereeve"
    directory.mkdir()
    host = directory / "host.env"
    other = directory / "unrelated"
    host.write_bytes(b"CODEREEVE_REPO_NAME=repo\n")
    other.write_bytes(b"operator data")
    storage.set_metadata(directory, 0o700, 901, 902)
    storage.set_metadata(host, 0o600, 901, 902)
    storage.set_metadata(other, 0o600, 901, 902)
    assert (
        cutover(spec, backend=backend, storage=storage).status == "committed"
    )
    assert storage.metadata(directory) == (0o700, 1001, 1001)
    assert storage.metadata(host) == (0o600, 1001, 1001)
    assert storage.metadata(other) == (0o600, 901, 902)


def test_operator_unit_edit_after_stop_is_not_overwritten(
    upgrade_context: CutoverContext,
) -> None:
    """Revalidate owned unit intent immediately before guard publication."""
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = upgrade_context
    path = backend.filesystem_root / "etc/systemd/system/bh-daemon.service"
    original = backend.stop_and_verify

    def stopped(name: str) -> None:
        original(name)
        if name == "bh-daemon.service":
            path.write_bytes(b"operator unit edit")

    backend.stop_and_verify = stopped
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "incomplete"
    assert path.read_bytes() == b"operator unit edit"


@pytest.mark.parametrize("held", [False, True])
def test_cutover_lock_requires_root_owned_exclusive_inode(
    cutover_context: CutoverContext, held: bool
) -> None:
    """Cutover serialization refuses foreign metadata and real contention."""
    from codereeve.migration.lease import WriterLease
    from codereeve.service_cutover.coordinator import cutover

    spec, backend, storage = cutover_context
    path = backend.target_path(spec.project_root) / ".codereeve-cutover.lock"
    if held:
        with WriterLease.acquire(path, purpose="other cutover"):
            with pytest.raises(CutoverError):
                cutover(spec, backend=backend, storage=storage)
    else:
        path.write_bytes(b"")
        storage.set_metadata(path, 0o600, 1001, 1001)
        with pytest.raises(CutoverError):
            cutover(spec, backend=backend, storage=storage)
    assert "preflight" not in backend.events


def test_review_r2_lock_authority_precedes_preflight(
    upgrade_context: CutoverContext,
) -> None:
    """The initial durable record carries absent-lock restoration authority."""
    from codereeve.service_cutover.coordinator import cutover
    from codereeve.service_cutover.journal import CutoverJournal

    spec, backend, storage = upgrade_context
    preflight = backend.preflight

    def inspect(selected: ServiceSpec, **kwargs: object) -> PreflightEvidence:
        root = backend.target_path(spec.project_root)
        journal = CutoverJournal.open(
            next((root / ".codereeve-cutover").glob("*/journal.jsonl")),
            storage=storage,
        )
        authority = journal.header["writer_lock"]
        assert authority["exists"] is False
        assert authority["inode"] is None
        assert (authority["restore_uid"], authority["restore_gid"]) == (
            1001,
            1001,
        )
        assert not (root / ".codereeve-migration.lock").exists()
        return preflight(selected, **kwargs)

    backend.preflight = inspect
    assert (
        cutover(spec, backend=backend, storage=storage).status == "committed"
    )
