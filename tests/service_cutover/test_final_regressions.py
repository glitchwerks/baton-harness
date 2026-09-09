"""Default legacy upgrade and durable enablement compensation regressions."""

from __future__ import annotations

from pathlib import Path, PurePath
from types import SimpleNamespace

import pytest
from conftest import CutoverContext, Interrupted

from codereeve.chain.app_private_key import AppPrivateKeyProvider
from codereeve.chain.sandbox_config import SandboxConfig
from codereeve.config_env import ResolvedEnvironment
from codereeve.paths import SelectedPath, select_compatible_file
from codereeve.service_cutover import cli
from codereeve.service_cutover.coordinator import (
    cutover,
    install_only,
    recover,
)
from codereeve.service_cutover.journal import CutoverJournal
from codereeve.service_cutover.model import ServiceSnapshot, ServiceSpec
from codereeve.service_cutover.selection import linux_path
from codereeve.service_cutover.systemd import NEW_UNIT, OLD_UNIT


@pytest.mark.parametrize("adopt", [False, True])
def test_default_cli_legacy_bws_upgrade(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
    adopt: bool,
) -> None:
    """Existing legacy tokens feed preflight and migrate to the final unit."""
    _, backend, storage = upgrade_context
    root = backend.filesystem_root
    monkeypatch.setattr(
        cli,
        "ServiceSpec",
        lambda **kw: ServiceSpec(
            **{
                k: linux_path(v.as_posix()) if isinstance(v, PurePath) else v
                for k, v in kw.items()
            }
        ),
    )
    legacy = root / "etc/bh-daemon/secrets.env"
    legacy.parent.mkdir()
    legacy.write_bytes(b"BWS_ACCESS_TOKEN=fixture-token\n")
    workflow = root / "srv/harness/config/WORKFLOW.md"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(b"fixture workflow")
    values = {"CODEREEVE_PROJECT_ROOT": "/srv/project"}
    configured = SimpleNamespace(
        config=SandboxConfig(
            "owner",
            "repo",
            "1",
            "2",
            AppPrivateKeyProvider.BWS,
            "11111111-1111-1111-1111-111111111111",
            None,
        ),
        environment=ResolvedEnvironment(values, ()),
    )
    monkeypatch.setattr(cli, "resolve_config_sources", lambda *a: configured)
    monkeypatch.setattr(
        cli, "_account_home", lambda user: linux_path("/home/runner")
    )

    # Keep CLI selections logical while the compatibility resolver reads the
    # disposable host. Only the host filesystem boundary is translated.
    def compatible(canonical: Path, old: Path, **kwargs: str) -> SelectedPath:
        selected = select_compatible_file(
            backend.target_path(canonical), backend.target_path(old), **kwargs
        )
        return SelectedPath(
            old if selected.uses_legacy else canonical, selected.uses_legacy
        )

    monkeypatch.setattr(cli, "select_compatible_file", compatible)
    exists = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda p: (
            exists(backend.target_path(p))
            if p.as_posix().startswith("/srv/harness/")
            else exists(p)
        ),
    )
    fresh = cli._fresh_secrets
    monkeypatch.setattr(
        cli,
        "_fresh_secrets",
        lambda *a, **kw: fresh(
            *a,
            **kw,
            path_exists=lambda p: exists(backend.target_path(p)),
            interactive=lambda: pytest.fail("existing token must not prompt"),
        ),
    )
    original_preflight = backend.preflight
    original_context = backend.service_context

    def preflight(selected: ServiceSpec, **kwargs: object) -> object:
        assert selected.secrets.as_posix() == "/etc/bh-daemon/secrets.env"
        assert (
            backend.target_path(selected.secrets).read_bytes()
            == b"BWS_ACCESS_TOKEN=fixture-token\n"
        )
        return original_preflight(selected, **kwargs)

    def context(
        selected: ServiceSpec, *, canonical: bool
    ) -> dict[str, object]:
        if canonical:
            assert selected.secrets.as_posix() == "/etc/codereeve/secrets.env"
            assert (
                backend.target_path(selected.secrets).read_bytes()
                == b"BWS_ACCESS_TOKEN=fixture-token\n"
            )
        return original_context(selected, canonical=canonical)

    monkeypatch.setattr(backend, "preflight", preflight)
    monkeypatch.setattr(backend, "service_context", context)
    monkeypatch.setattr(
        cli,
        "cutover",
        lambda selected, **kw: cutover(
            selected, backend=backend, storage=storage, **kw
        ),
    )
    monkeypatch.setattr(
        cli,
        "install_only",
        lambda selected, **kw: install_only(
            selected, backend=backend, storage=storage, **kw
        ),
    )
    args = [
        "--harness-dir",
        "/srv/harness",
        "--project-root",
        "/srv/project",
        "--environment",
        "/opt/new",
        "--user",
        "runner",
    ]
    if adopt:
        assert cli.main([*args, "--no-start"], environment=values) == 0
        assert legacy.exists()
    assert cli.main(args, environment=values) == 0
    unit = (root / "etc/systemd/system/codereeve.service").read_text()
    assert "EnvironmentFile=/etc/codereeve/secrets.env" in unit
    assert "/etc/bh-daemon/secrets.env" not in unit
    assert not legacy.exists()


@pytest.mark.parametrize("adopt", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "normal",
        "enable_effect",
        "enable_done",
        "restore_intent",
        "before_unlink",
        "after_unlink",
        "restore_done",
    ],
)
def test_owned_enablement_removed_before_old_activation(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
    adopt: bool,
    boundary: str,
) -> None:
    """Link ownership survives missing fragments and fresh recovery replay."""
    spec, backend, storage = upgrade_context
    root = backend.filesystem_root
    link = (
        root / "etc/systemd/system/multi-user.target.wants/codereeve.service"
    )
    if adopt:
        assert (
            install_only(spec, backend=backend, storage=storage).status
            == "installed"
        )
    activate = type(backend).restore_activation

    def verified_activation(self: object, snapshot: ServiceSnapshot) -> None:
        assert not link.is_symlink()
        assert link.parent in storage.flushes
        assert (link.parent / OLD_UNIT).is_symlink()
        activate(self, snapshot)

    monkeypatch.setattr(
        type(backend), "restore_activation", verified_activation
    )
    backend.fail_at = "enable:bh-daemon.service:False"
    record = CutoverJournal.record
    unlink = Path.unlink
    enable = backend.set_enabled

    def interrupted_enable(name: str, enabled: bool) -> object:
        """Lose the process after the link exists but before effect_done."""
        state = enable(name, enabled)
        if name == NEW_UNIT and enabled and boundary == "enable_effect":
            raise Interrupted()
        return state

    monkeypatch.setattr(backend, "set_enabled", interrupted_enable)

    def interrupted_record(
        self: CutoverJournal, event: str, metadata: dict[str, object]
    ) -> None:
        record(self, event, metadata)
        if (
            (
                boundary == "enable_done"
                and event == "effect_done"
                and metadata.get("operation") == "enable_new"
            )
            or (
                boundary == "restore_intent"
                and event == "effect_intent"
                and metadata.get("operation") == "restore_enablement"
            )
            or (
                boundary == "restore_done"
                and event == "effect_done"
                and metadata.get("operation") == "restore_enablement"
            )
        ):
            raise Interrupted()

    def interrupted_unlink(
        path: Path, *args: object, **kwargs: object
    ) -> None:
        if path == link and boundary == "before_unlink":
            raise Interrupted()
        unlink(path, *args, **kwargs)
        if path == link and boundary == "after_unlink":
            raise Interrupted()

    monkeypatch.setattr(CutoverJournal, "record", interrupted_record)
    monkeypatch.setattr(Path, "unlink", interrupted_unlink)
    try:
        result = cutover(spec, backend=backend, storage=storage)
    except Interrupted:
        monkeypatch.setattr(CutoverJournal, "record", record)
        monkeypatch.setattr(Path, "unlink", unlink)
        path = next(
            p
            for p in (root / "srv/project/.codereeve-cutover").glob(
                "*/journal.jsonl"
            )
            if CutoverJournal.open(p, storage=storage).mode == "cutover"
        )
        fresh_backend = type(backend)()
        fresh_backend.states = backend.states
        fresh_backend.events = backend.events
        fresh_backend.active_counts = backend.active_counts
        result = recover(path, backend=fresh_backend, storage=storage)
    assert result.recovery == "complete"
    assert not link.is_symlink()
    assert link.parent in storage.flushes
    assert (link.parent / OLD_UNIT).is_symlink()
    assert backend.states[OLD_UNIT].active_state == "active"
    assert backend.states[NEW_UNIT].load_state == (
        "loaded" if adopt else "not-found"
    )


@pytest.mark.parametrize("kind", ["runtime", "persistent", "target", "file"])
def test_unowned_enablement_is_preserved_during_recovery(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Unexpected operator links prevent all enablement cleanup/activation."""
    spec, backend, storage = upgrade_context
    root = backend.filesystem_root
    owned = (
        root / "etc/systemd/system/multi-user.target.wants/codereeve.service"
    )
    operator = (
        owned
        if kind in {"target", "file"}
        else root
        / (
            ("run" if kind == "runtime" else "etc")
            + "/systemd/system/operator.target.requires/codereeve.service"
        )
    )
    record = CutoverJournal.record

    def interrupt(
        self: CutoverJournal, event: str, metadata: dict[str, object]
    ) -> None:
        record(self, event, metadata)
        if (
            event == "effect_done"
            and metadata.get("operation") == "enable_new"
        ):
            operator.parent.mkdir(parents=True, exist_ok=True)
            if operator == owned:
                owned.unlink()
            if kind == "file":
                operator.write_bytes(b"operator input")
            else:
                operator.symlink_to("/operator/unit.service")
            raise Interrupted()

    monkeypatch.setattr(CutoverJournal, "record", interrupt)
    with pytest.raises(Interrupted):
        cutover(spec, backend=backend, storage=storage)
    monkeypatch.setattr(CutoverJournal, "record", record)
    path = next(
        (root / "srv/project/.codereeve-cutover").glob("*/journal.jsonl")
    )
    fresh = type(backend)()
    fresh.states = backend.states
    assert (
        recover(path, backend=fresh, storage=storage).recovery == "incomplete"
    )
    assert (
        operator.read_bytes() == b"operator input"
        if kind == "file"
        else operator.readlink().as_posix() == "/operator/unit.service"
    )
    assert owned.is_symlink() or kind == "file"
    assert "old_started" not in fresh.events


def test_enablement_flush_failure_defers_activation_until_fresh_retry(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Visible absence after unlink requires a successful directory fsync."""
    spec, backend, storage = upgrade_context
    root = backend.filesystem_root
    link = (
        root / "etc/systemd/system/multi-user.target.wants/codereeve.service"
    )
    sync = storage.sync_directory

    def fail_flush(path: Path) -> None:
        if path == link.parent:
            raise OSError("injected directory flush failure")
        sync(path)

    monkeypatch.setattr(storage, "sync_directory", fail_flush)
    backend.fail_at = "enable:bh-daemon.service:False"
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "incomplete"
    assert not link.is_symlink()
    assert "old_started" not in backend.events
    monkeypatch.setattr(storage, "sync_directory", sync)
    fresh = type(backend)()
    fresh.states = backend.states
    assert (
        recover(result.journal_path, backend=fresh, storage=storage).recovery
        == "complete"
    )
    assert link.parent in storage.flushes
    assert "old_started" in fresh.events


def test_completed_enablement_drift_blocks_old_activation(
    upgrade_context: CutoverContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed restore record cannot hide later old enablement drift."""
    spec, backend, storage = upgrade_context
    backend.fail_at = "enable:bh-daemon.service:False"
    record = CutoverJournal.record

    def interrupt(
        self: CutoverJournal, event: str, metadata: dict[str, object]
    ) -> None:
        """Crash immediately after durable enablement restoration."""
        record(self, event, metadata)
        if (
            event == "effect_done"
            and metadata.get("operation") == "restore_enablement"
        ):
            raise Interrupted()

    monkeypatch.setattr(CutoverJournal, "record", interrupt)
    with pytest.raises(Interrupted):
        cutover(spec, backend=backend, storage=storage)
    monkeypatch.setattr(CutoverJournal, "record", record)
    root = backend.filesystem_root
    old_link = (
        root / "etc/systemd/system/multi-user.target.wants/bh-daemon.service"
    )
    old_link.unlink()
    fresh = type(backend)()
    fresh.states = backend.states
    fresh.reload()
    path = next(
        (root / "srv/project/.codereeve-cutover").glob("*/journal.jsonl")
    )
    assert (
        recover(path, backend=fresh, storage=storage).recovery == "incomplete"
    )
    assert "old_started" not in fresh.events


def test_preexisting_canonical_link_is_not_claimed(
    upgrade_context: CutoverContext,
) -> None:
    """An operator link present before enable intent is never ours to undo."""
    spec, backend, storage = upgrade_context
    link = (
        backend.filesystem_root
        / "etc/systemd/system/multi-user.target.wants/codereeve.service"
    )
    link.symlink_to("/etc/systemd/system/codereeve.service")
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == "incomplete"
    assert link.is_symlink()
    journal = CutoverJournal.open(result.journal_path, storage=storage)
    assert not any(
        e["metadata"].get("operation") == "enable_new" for e in journal.events
    )
    assert "old_started" not in backend.events
