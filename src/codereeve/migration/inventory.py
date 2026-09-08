"""Read-only filesystem and configuration migration inventory.

No lease acquisition, process execution, service query, or mutation occurs
here. Readiness describes a local plan only; apply must obtain fresh writer
quiescence evidence and retain its exclusive lease before any mutation.
"""

from __future__ import annotations

import stat
from pathlib import Path

from codereeve.config_env import (
    AliasConflictError,
    ConfigSyntaxError,
    EnvLayer,
    parse_env_text,
    resolve_environment,
)
from codereeve.redact import redact_secrets

from .model import (
    EvidenceState,
    MigrationAction,
    MigrationContext,
    MigrationFinding,
    MigrationReport,
    MigrationStatus,
)


class _Inventory:
    """Accumulate safe metadata observations for one inventory invocation."""

    def __init__(self) -> None:
        """Create invocation-local result buffers."""
        self.findings: list[MigrationFinding] = []
        self.files: dict[Path, bytes] = {}

    def finding(
        self,
        code: str,
        scope: str,
        *paths: Path,
        blocking: bool = True,
        detail: str = "",
    ) -> None:
        """Append a static diagnostic without copying an exception value."""
        self.findings.append(
            MigrationFinding(code, scope, tuple(paths), detail, blocking)
        )

    def inspect(
        self,
        path: Path,
        scope: str,
        *,
        directory: bool = False,
        retain: bool = False,
    ) -> bool:
        """Inspect ancestors, type, readability, and recursive descendants.

        Args:
            path: File or directory to inspect without following links.
            scope: Static diagnostic scope.
            directory: Require a directory and inspect its descendants.
            retain: Retain bytes for literal config parsing only.

        Returns:
            Whether the path exists; unsafe existing paths still count for
            coexistence checks and always append a blocking finding.
        """
        try:
            for parent in reversed(path.parents):
                try:
                    mode = parent.lstat().st_mode
                except FileNotFoundError:
                    return False
                if not stat.S_ISDIR(mode):
                    self.finding("unsafe_path", scope, parent)
                    return False
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            self.finding("unreadable_path", scope, path)
            return False
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(metadata.st_mode):
            self.finding("unsafe_path", scope, path)
            return True
        try:
            if directory:
                for child in sorted(path.iterdir()):
                    child_mode = child.lstat().st_mode
                    self.inspect(
                        child, scope, directory=stat.S_ISDIR(child_mode)
                    )
            else:
                with path.open("rb") as stream:
                    if retain:
                        self.files[path] = stream.read()
                    else:
                        while stream.read(1024 * 1024):
                            pass
        except OSError:
            self.finding("unreadable_path", scope, path)
        return True


def inventory_migration(context: MigrationContext) -> MigrationReport:
    """Build a deterministic migration plan without external effects.

    Args:
        context: Immutable layout, environment, and read-only evidence.

    Returns:
        A schema-v1 report. Unknown writers never become verified merely
        because their marker, heartbeat, or lock file is absent.
    """
    layout = context.layout
    scan = _Inventory()
    actions: list[MigrationAction] = []
    layers = list(context.layers)
    canonical_state = scan.inspect(
        layout.canonical_state, "state", directory=True
    )
    pairs = (
        ("config", layout.legacy_config, layout.canonical_config, False),
        (
            "baseline",
            layout.legacy_config.parent / "ruleset-baseline.json",
            layout.canonical_state / "ruleset-baseline.json",
            False,
        ),
        ("state", layout.legacy_state, layout.canonical_state, True),
        ("host", layout.legacy_host, layout.canonical_host, False),
        ("secrets", layout.legacy_secrets, layout.canonical_secrets, False),
        ("service", layout.legacy_unit, layout.canonical_unit, False),
    )
    service_legacy = False
    for scope, source, destination, directory in pairs:
        config = scope in {"config", "host", "secrets"}
        legacy = scan.inspect(
            source, scope, directory=directory, retain=config
        )
        canonical = (
            canonical_state
            if scope == "state"
            else scan.inspect(destination, scope, retain=config)
        )
        if legacy and (
            canonical or (scope in {"config", "baseline"} and canonical_state)
        ):
            scan.finding("path_coexistence", scope, source, destination)
        if scope == "service":
            service_legacy = legacy
            if legacy:
                scan.finding(
                    "external_service_cutover",
                    scope,
                    source,
                    destination,
                    blocking=False,
                    detail=(
                        "Service cutover requires the external coordinator."
                    ),
                )
            if (
                legacy or canonical
            ) and context.evidence.service is not EvidenceState.CLEAR:
                scan.finding(
                    "service_state_unverified", scope, source, destination
                )
        elif legacy:
            actions.append(
                MigrationAction(f"migrate_{scope}", scope, source, destination)
            )
        if config:
            selected = destination if canonical else source
            if selected in scan.files:
                try:
                    assignments = parse_env_text(
                        scan.files[selected].decode("utf-8"), source=scope
                    )
                    layers.append(
                        EnvLayer(
                            scope,
                            {item.key: item.value for item in assignments},
                        )
                    )
                except (ConfigSyntaxError, UnicodeError):
                    scan.finding("invalid_config", scope, selected)
    for action in actions:
        if action.scope in {"config", "baseline"}:
            competitor = layout.legacy_state / action.destination.name
            if scan.inspect(competitor, "state"):
                scan.finding(
                    "target_collision",
                    action.scope,
                    action.source,
                    competitor,
                    action.destination,
                )
    try:
        resolve_environment(layers)
    except AliasConflictError as exc:
        scan.finding(
            "environment_conflict",
            "environment",
            detail=redact_secrets(
                str(exc),
                extra_values=(
                    value
                    for layer in layers
                    for value in layer.values.values()
                ),
            ),
        )
    _inspect_evidence(context, scan, needed=bool(actions) or service_legacy)
    if any(finding.blocking for finding in scan.findings):
        status = MigrationStatus.BLOCKED
    elif actions or service_legacy:
        status = MigrationStatus.READY
    else:
        status = MigrationStatus.CURRENT
    return MigrationReport(status, tuple(actions), tuple(scan.findings))


def _inspect_evidence(
    context: MigrationContext, scan: _Inventory, *, needed: bool
) -> None:
    """Inventory transaction artifacts without inventing journal semantics."""
    evidence = context.evidence
    project = context.layout.canonical_state.parent
    transaction_root = project / ".codereeve-migration"
    lock = project / ".codereeve-migration.lock"
    lock_exists = scan.inspect(lock, "lease")
    if evidence.lease is EvidenceState.BLOCKED:
        scan.finding("writer_lease_held", "lease", lock)
    elif lock_exists and evidence.lease is EvidenceState.UNKNOWN:
        scan.finding("lease_state_unverified", "lease", lock, blocking=False)
    if evidence.writers is EvidenceState.BLOCKED:
        scan.finding("writer_active", "writers", project)
    elif needed and evidence.writers is EvidenceState.UNKNOWN:
        scan.finding(
            "writer_quiescence_unverified",
            "writers",
            project,
            blocking=False,
            detail=(
                "Apply requires fresh verified shutdown evidence "
                "and a retained exclusive writer lease."
            ),
        )
    if evidence.journals is EvidenceState.BLOCKED:
        scan.finding("incomplete_journal", "recovery", transaction_root)
    before = len(scan.findings)
    exists = scan.inspect(transaction_root, "recovery", directory=True)
    if exists and len(scan.findings) == before:
        try:
            for entry in sorted(transaction_root.iterdir()):
                if not stat.S_ISDIR(entry.lstat().st_mode):
                    scan.finding("unsafe_path", "recovery", entry)
                    continue
                manifest = scan.inspect(entry / "manifest.json", "recovery")
                journal = scan.inspect(entry / "journal.jsonl", "recovery")
                if not manifest or not journal:
                    scan.finding("incomplete_journal", "recovery", entry)
                    continue
                if entry not in evidence.verified_transactions:
                    scan.finding(
                        "journal_recovery_unverified", "recovery", entry
                    )
        except OSError:
            scan.finding("unreadable_path", "recovery", transaction_root)
