"""Read-only migration inventory and schema contracts."""

from __future__ import annotations

import builtins
import io
import json
import os
import socket
import stat
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from codereeve.config_env import EnvLayer
from codereeve.migration import (
    EvidenceState,
    MigrationContext,
    MigrationEvidence,
    inventory_migration,
)
from codereeve.migration.model import (
    MigrationAction,
    MigrationFinding,
    MigrationReport,
    MigrationStatus,
)
from codereeve.paths import PathLayout


@pytest.fixture
def context(tmp_path: Path) -> MigrationContext:
    """Isolate every filesystem scope from the machine configuration."""
    return MigrationContext(
        PathLayout.for_environment(
            tmp_path / "project",
            {},
            home=tmp_path / "home",
            etc_root=tmp_path / "etc",
        )
    )


def put(path: Path, text: str = "") -> None:
    """Create a regular fixture file before effect guards are installed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def codes(report: MigrationReport) -> set[str]:
    """Return stable finding identifiers."""
    return {finding.code for finding in report.findings}


@pytest.mark.parametrize(
    "name",
    [
        None,
        "canonical_config",
        "canonical_host",
        "canonical_secrets",
        "canonical_state",
    ],
)
def test_current(context: MigrationContext, name: str | None) -> None:
    """Fresh and canonical-only installations need no migration."""
    if name == "canonical_state":
        context.layout.canonical_state.mkdir(parents=True)
    elif name:
        put(getattr(context.layout, name))
    report = inventory_migration(context)
    assert (report.status.value, report.exit_code, report.actions) == (
        "current",
        0,
        (),
    )


@pytest.mark.parametrize(
    "name",
    [
        "legacy_config",
        "legacy_host",
        "legacy_secrets",
        "legacy_state",
        "baseline",
        "all",
    ],
)
def test_legacy_ready(context: MigrationContext, name: str) -> None:
    """Each legacy data scope and their combination yields a local plan."""
    names = (
        [
            "legacy_config",
            "legacy_host",
            "legacy_secrets",
            "legacy_state",
            "baseline",
        ]
        if name == "all"
        else [name]
    )
    for item in names:
        path = (
            context.layout.legacy_config.parent / "ruleset-baseline.json"
            if item == "baseline"
            else getattr(context.layout, item)
        )
        if item == "legacy_state":
            put(path / "nested" / "heartbeat", "opaque-state")
        else:
            put(path)
    put(context.layout.symphony_state / "untouched", "private")
    report = inventory_migration(context)
    assert (report.status.value, report.exit_code) == ("ready", 2)
    assert len(report.actions) == len(names)
    assert "writer_quiescence_unverified" in codes(report)
    assert ".symphony" not in json.dumps(report.as_dict())


@pytest.mark.parametrize(
    "scope", ["config", "state", "host", "secrets", "unit"]
)
def test_coexistence_blocks(context: MigrationContext, scope: str) -> None:
    """Migration never merges canonical and legacy scopes."""
    for prefix in ("legacy", "canonical"):
        path = getattr(context.layout, f"{prefix}_{scope}")
        if scope == "state":
            path.mkdir(parents=True)
        else:
            put(path)
    assert "path_coexistence" in codes(inventory_migration(context))
    assert inventory_migration(context).exit_code == 1


@pytest.mark.parametrize("name", ["config.env", "ruleset-baseline.json"])
def test_duplicate_target(context: MigrationContext, name: str) -> None:
    """Independent legacy inputs cannot publish the same relative path."""
    put(context.layout.legacy_config.parent / name)
    put(context.layout.legacy_state / name)
    assert "target_collision" in codes(inventory_migration(context))


def test_canonical_root_blocks_legacy_config(
    context: MigrationContext,
) -> None:
    """Even an empty canonical directory cannot be merged during apply."""
    context.layout.canonical_state.mkdir(parents=True)
    put(context.layout.legacy_config)
    assert inventory_migration(context).status is MigrationStatus.BLOCKED


@pytest.mark.parametrize(
    "mode",
    [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK],
)
def test_unsafe_child(
    context: MigrationContext, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """Every unsupported child type blocks without opening the child."""
    path = context.layout.legacy_state / "unsafe"
    put(path)
    original = Path.lstat

    def lstat(target: Path, *args: object, **kwargs: object) -> os.stat_result:
        """Inject portable special-file metadata."""
        result = original(target)
        return (
            os.stat_result((mode, *result[1:])) if target == path else result
        )

    monkeypatch.setattr(Path, "lstat", lstat)
    assert "unsafe_path" in codes(inventory_migration(context))


def test_unreadable_file(
    context: MigrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission failures become value-free blocked findings."""
    put(context.layout.legacy_state / "heartbeat")

    def denied(*args: object, **kwargs: object) -> object:
        """Simulate an operating-system read failure."""
        raise PermissionError("private-command-output")

    monkeypatch.setattr(Path, "open", denied)
    report = inventory_migration(context)
    assert "unreadable_path" in codes(report)
    assert "private-command-output" not in json.dumps(report.as_dict())


def test_environment_conflict_and_syntax(context: MigrationContext) -> None:
    """Both parser and pair conflicts are reported without input values."""
    put(context.layout.legacy_config, "BH_REPO_OWNER='secret-one'\n")
    supplied = replace(
        context,
        layers=(EnvLayer("operator", {"CODEREEVE_REPO_OWNER": "secret-two"}),),
    )
    report = inventory_migration(supplied)
    assert "environment_conflict" in codes(report)
    assert "CODEREEVE_REPO_OWNER (operator)" in json.dumps(report.as_dict())
    assert "BH_REPO_OWNER (config)" in json.dumps(report.as_dict())
    assert "secret-" not in json.dumps(report.as_dict())
    put(context.layout.legacy_config, "BH_REPO_OWNER=$(private-command)\n")
    report = inventory_migration(context)
    assert "invalid_config" in codes(report)
    assert "private-command" not in json.dumps(report.as_dict())


def test_service_requires_explicit_evidence(context: MigrationContext) -> None:
    """Service cutover is external and unknown service state blocks."""
    put(context.layout.legacy_unit)
    report = inventory_migration(context)
    assert report.exit_code == 1
    assert "service_state_unverified" in codes(report)
    report = inventory_migration(
        replace(
            context, evidence=MigrationEvidence(service=EvidenceState.CLEAR)
        )
    )
    assert report.exit_code == 2
    assert not report.actions
    assert "external_service_cutover" in codes(report)


@pytest.mark.parametrize(
    "field,code",
    [
        ("lease", "writer_lease_held"),
        ("journals", "incomplete_journal"),
        ("writers", "writer_active"),
    ],
)
def test_blocking_evidence(
    context: MigrationContext, field: str, code: str
) -> None:
    """Injected known blockers are honored even without legacy files."""
    evidence = replace(MigrationEvidence(), **{field: EvidenceState.BLOCKED})
    report = inventory_migration(replace(context, evidence=evidence))
    assert report.exit_code == 1
    assert code in codes(report)


def test_existing_transaction_requires_verified_recovery(
    context: MigrationContext,
) -> None:
    """An on-disk transaction cannot disappear behind omitted evidence."""
    path = (
        context.layout.canonical_state.parent
        / ".codereeve-migration"
        / "txn"
        / "journal.jsonl"
    )
    put(path, '{"event":"done"}\n')
    put(path.parent / "manifest.json", "{}")
    report = inventory_migration(context)
    assert report.exit_code == 1
    assert "journal_recovery_unverified" in codes(report)


def test_incomplete_transaction_is_detected_without_probe(
    context: MigrationContext,
) -> None:
    """A transaction missing its manifest blocks even with stale clearance."""
    transaction = (
        context.layout.canonical_state.parent / ".codereeve-migration" / "txn"
    )
    put(transaction / "journal.jsonl", "")
    evidence = MigrationEvidence(
        journals=EvidenceState.CLEAR, verified_transactions=(transaction,)
    )
    report = inventory_migration(replace(context, evidence=evidence))
    assert report.exit_code == 1
    assert "incomplete_journal" in codes(report)


def test_verified_transaction_does_not_block_noop(
    context: MigrationContext,
) -> None:
    """Only an explicit parser-verified transaction may pass recovery."""
    transaction = (
        context.layout.canonical_state.parent / ".codereeve-migration" / "txn"
    )
    put(transaction / "journal.jsonl", '{"event":"terminal"}\n')
    put(transaction / "manifest.json", "{}")
    evidence = MigrationEvidence(verified_transactions=(transaction,))
    assert (
        inventory_migration(replace(context, evidence=evidence)).exit_code == 0
    )


def test_linked_ancestor_is_not_followed(
    context: MigrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A linked ancestor above a nonexistent config descendant blocks."""
    ancestor = context.layout.legacy_host.parent.parent
    ancestor.mkdir(parents=True)
    original = Path.lstat

    def lstat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        """Represent a linked ancestor without requiring Windows privilege."""
        result = original(path)
        return (
            os.stat_result((stat.S_IFLNK, *result[1:]))
            if path == ancestor
            else result
        )

    monkeypatch.setattr(Path, "lstat", lstat)
    assert "unsafe_path" in codes(inventory_migration(context))


def test_empty_state_directory_is_a_migration_input(
    context: MigrationContext,
) -> None:
    """An empty legacy root still needs a canonical publication."""
    context.layout.legacy_state.mkdir(parents=True)
    report = inventory_migration(context)
    assert report.exit_code == 2
    assert report.actions[0].source == context.layout.legacy_state
    assert report.actions[0].destination == context.layout.canonical_state


def test_report_paths_order_is_independent_of_caller_order() -> None:
    """Equivalent finding sets have identical immutable and rendered order."""
    a = MigrationFinding("unsafe_path", "state", (Path("a"), Path("z")))
    b = MigrationFinding("unsafe_path", "state", (Path("z"), Path("a")))
    assert MigrationReport(
        MigrationStatus.BLOCKED, findings=(a,)
    ) == MigrationReport(MigrationStatus.BLOCKED, findings=(b,))


@pytest.mark.parametrize("kind", ["current", "ready", "blocked"])
def test_no_mutations_or_external_effects(
    context: MigrationContext, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Read-only checks do not call mutation, process, or network seams."""
    if kind != "current":
        put(context.layout.legacy_config, "BH_REPO_OWNER=owner\n")
    if kind == "blocked":
        put(context.layout.canonical_config)
    original = builtins.open

    def read_only(
        file: object, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        """Reject all writable builtin file opens."""
        assert not any(flag in mode for flag in "wax+")
        return original(file, mode, *args, **kwargs)

    def forbidden(*args: object, **kwargs: object) -> object:
        """Fail on any external effect or mutation."""
        pytest.fail("inventory attempted an effect")

    monkeypatch.setattr(builtins, "open", read_only)
    monkeypatch.setattr(io, "open", read_only)
    for name in (
        "mkdir",
        "rename",
        "replace",
        "unlink",
        "chmod",
        "write_text",
        "write_bytes",
    ):
        monkeypatch.setattr(Path, name, forbidden)
    for name in ("mkdir", "rename", "replace", "unlink", "chmod"):
        monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert inventory_migration(context).status.value == kind


def test_context_snapshots_layers(context: MigrationContext) -> None:
    """Frozen context also protects nested input mappings."""
    values = {"BH_REPO_OWNER": "before"}
    context = replace(context, layers=(EnvLayer("operator", values),))
    values["BH_REPO_OWNER"] = "after"
    assert context.layers[0].values["BH_REPO_OWNER"] == "before"
    with pytest.raises(TypeError):
        context.layers[0].values["BH_REPO_OWNER"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        context.layout = context.layout  # type: ignore[misc]


def test_schema_sorting_and_redaction() -> None:
    """One stable JSON model masks credential-bearing report details."""
    actions = (
        MigrationAction("move", "z", Path("z"), Path("zz")),
        MigrationAction("move", "a", Path("a"), Path("aa")),
    )
    findings = (
        MigrationFinding(
            "diagnostic",
            "x",
            (Path("https://user:password@host/file"),),
            'api_key="opaque secret" ghp_token',
        ),
    )
    report = MigrationReport(MigrationStatus.READY, actions, findings)
    data = report.as_dict()
    assert data["schema_version"] == 1
    assert data["exit_code"] == 2
    assert json.loads(json.dumps(data)) == data
    assert [a["scope"] for a in data["actions"]] == ["a", "z"]
    rendered = json.dumps(data)
    assert "password" not in rendered
    assert "opaque secret" not in rendered
    assert "ghp_token" not in rendered
