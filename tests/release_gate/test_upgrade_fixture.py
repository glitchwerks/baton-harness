"""Durable legacy-to-canonical upgrade fixture contracts."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import cast

import pytest

from codereeve.config_env import (
    PRODUCT_ALIASES,
    EnvLayer,
    parse_env_file,
    resolve_environment,
)
from codereeve.migration.inventory import inventory_migration
from codereeve.migration.lease import WriterLease
from codereeve.migration.model import MigrationContext, MigrationStatus
from codereeve.migration.transaction import (
    RestorationStatus,
    apply_migration,
    restore_migration,
)
from codereeve.paths import PathLayout
from tests.release_gate.fixture import (
    PortableFixtureOperations,
    load_fixture,
    materialize_fixture,
)

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "codereeve-upgrade-v1.json"
)


def test_upgrade_fixture_is_committed_and_nonempty() -> None:
    """A missing release input must fail before migration validation runs."""
    assert FIXTURE_PATH.is_file()
    assert FIXTURE_PATH.stat().st_size > 0


def test_load_fixture_exposes_versioned_release_contract() -> None:
    """Schema drift or an incomplete fixture must fail at its load boundary."""
    fixture = load_fixture(FIXTURE_PATH)
    assert fixture["schema_version"] == 1
    assert fixture["legacy_source_revision"] == (
        "fb503ba3a5bc89e447a37983e3e0eff1af2419a4"
    )
    assert fixture["files"]
    assert fixture["canonical_assertions"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "schema_version": 2,
            "legacy_source_revision": "revision",
            "files": [],
            "canonical_assertions": {},
        },
    ],
)
def test_load_fixture_rejects_unknown_or_incomplete_schema(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    """Unsupported schema input must never reach filesystem materialization."""
    import json

    candidate = tmp_path / "fixture.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_fixture(candidate)


@pytest.mark.parametrize(
    "path",
    [
        "/absolute/file",
        "C:/drive/file",
        "//server/share/file",
        "project/../escaped",
        "project\\windows-path",
    ],
)
def test_materialize_fixture_rejects_nonportable_paths_before_writes(
    tmp_path: Path, path: str
) -> None:
    """A malformed entry must leave the disposable destination absent."""
    fixture = load_fixture(FIXTURE_PATH)
    files = list(fixture["files"])
    files.append({"path": path, "content": "bad", "permissions": 0o600})
    malformed = dict(fixture, files=files)
    root = tmp_path / "host"
    with pytest.raises(ValueError):
        materialize_fixture(root, malformed)
    assert not root.exists()


def test_materialize_fixture_rejects_duplicate_paths_before_writes(
    tmp_path: Path,
) -> None:
    """Duplicate destinations must not create a partially seeded tree."""
    fixture = load_fixture(FIXTURE_PATH)
    files = list(fixture["files"])
    files.append(dict(files[0]))
    root = tmp_path / "host"
    with pytest.raises(ValueError):
        materialize_fixture(root, dict(fixture, files=files))
    assert not root.exists()


def test_materialize_fixture_rejects_late_nul_path_before_any_write(
    tmp_path: Path,
) -> None:
    """A later NUL path must not leave an earlier valid file behind."""
    fixture = load_fixture(FIXTURE_PATH)
    files = [
        {"path": "valid/file", "content": "valid", "permissions": 0o600},
        {"path": "invalid/nu\x00l", "content": "bad", "permissions": 0o600},
    ]
    root = tmp_path / "host"
    with pytest.raises(ValueError):
        materialize_fixture(root, dict(fixture, files=files))
    assert not root.exists()


def test_fixture_refuses_existing_root(tmp_path: Path) -> None:
    """Operator-owned destination content must remain byte-for-byte intact."""
    root = tmp_path / "host"
    root.mkdir()
    (root / "operator-file").write_bytes(b"preserve")
    fixture = load_fixture(FIXTURE_PATH)
    with pytest.raises(ValueError):
        materialize_fixture(root, fixture)
    assert (root / "operator-file").read_bytes() == b"preserve"


def test_materialize_fixture_rejects_symlink_destination_parent(
    tmp_path: Path,
) -> None:
    """A linked parent must not redirect writes outside the disposable tree."""
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    root = linked / "host"
    with pytest.raises(ValueError):
        materialize_fixture(root, load_fixture(FIXTURE_PATH))
    assert not (outside / "host").exists()


def _fixture_layout(root: Path) -> PathLayout:
    """Build the production layout against only synthetic fixture roots."""
    return PathLayout.for_environment(
        root / "project",
        {"XDG_CONFIG_HOME": str(root / "home" / ".config")},
        home=root / "home",
        etc_root=root / "etc",
    )


def _assignment_values(path: Path) -> dict[str, str]:
    """Parse one migrated file into its final assignment values."""
    return {item.key: item.value for item in parse_env_file(path)}


def test_portable_operations_rejects_unexpected_lease_path(
    tmp_path: Path,
) -> None:
    """Reject a wrong lease path with a stable exception."""
    lease = cast(
        WriterLease, type("Lease", (), {"path": tmp_path / "wrong"})()
    )
    with pytest.raises(
        RuntimeError, match="portable fixture lease path mismatch"
    ):
        PortableFixtureOperations().verify_quiescence(tmp_path, lease)


def test_portable_operations_rejects_unheld_expected_lease(
    tmp_path: Path,
) -> None:
    """Portable evidence must fail when the expected lease is not held."""
    lease = cast(
        WriterLease,
        type(
            "Lease",
            (),
            {"path": tmp_path / ".codereeve-migration.lock"},
        )(),
    )
    with pytest.raises(
        RuntimeError, match="portable fixture lease is not held"
    ):
        PortableFixtureOperations().verify_quiescence(tmp_path, lease)


def test_fixture_migrates_canonically_and_restores_exact_originals(
    tmp_path: Path,
) -> None:
    """Lossy conversion, compatibility residue, or rollback drift fails."""
    fixture = load_fixture(FIXTURE_PATH)
    root = tmp_path / "fixture-host"
    materialize_fixture(root, fixture)
    layout = _fixture_layout(root)
    context = MigrationContext(layout)
    originals = {
        entry["path"]: (root / entry["path"]).read_bytes()
        for entry in fixture["files"]
    }
    original_modes = {
        entry["path"]: stat.S_IMODE((root / entry["path"]).stat().st_mode)
        for entry in fixture["files"]
    }
    sentinel = originals["project/.symphony/sentinel.json"]

    report = inventory_migration(context)
    assert report.status is MigrationStatus.READY
    assert {action.scope for action in report.actions} == {
        "baseline",
        "config",
        "host",
        "secrets",
        "state",
    }
    operations = PortableFixtureOperations()
    result = apply_migration(context, operations=operations)

    legacy_keys = {alias.legacy for alias in PRODUCT_ALIASES}
    assertions = fixture["canonical_assertions"]
    for relative_path, expected in assertions.items():
        values = _assignment_values(root / relative_path)
        assert values == expected
        assert legacy_keys.isdisjoint(values)
        resolved = resolve_environment(
            (EnvLayer(relative_path, values),), export_legacy=False
        )
        assert resolved.legacy_uses == ()
        assert legacy_keys.isdisjoint(resolved.values)

    assert (
        layout.canonical_state.joinpath("runlog.jsonl").read_bytes()
        == (originals["project/.baton-harness/runlog.jsonl"])
    )
    assert (
        layout.canonical_state.joinpath("heartbeat").read_bytes()
        == (originals["project/.baton-harness/heartbeat"])
    )
    assert (
        layout.canonical_state.joinpath("ruleset-baseline.json").read_bytes()
        == originals["project/.bh/ruleset-baseline.json"]
    )
    assert (
        layout.symphony_state.joinpath("sentinel.json").read_bytes()
        == sentinel
    )
    backup_bytes = {
        file.read_bytes()
        for backup in result.backups
        for file in ([backup] if backup.is_file() else backup.rglob("*"))
        if file.is_file()
    }
    migrated_originals = {
        content
        for path, content in originals.items()
        if "/.symphony/" not in f"/{path}"
    }
    assert migrated_originals <= backup_bytes

    restored = restore_migration(
        result.manifest_path, operations=PortableFixtureOperations()
    )
    assert restored.status is RestorationStatus.COMPLETE
    for relative_path, content in originals.items():
        assert (root / relative_path).read_bytes() == content
        assert (
            stat.S_IMODE((root / relative_path).stat().st_mode)
            == (original_modes[relative_path])
        )
    assert not layout.canonical_state.exists()
    assert not layout.canonical_host.exists()
    assert not layout.canonical_secrets.exists()
