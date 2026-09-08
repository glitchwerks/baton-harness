"""Immutable migration plans and secret-safe schema-v1 reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from codereeve.config_env import EnvLayer
from codereeve.paths import PathLayout
from codereeve.redact import redact_secrets


class MigrationStatus(str, Enum):
    """Public local-inventory status; readiness does not grant a lease."""

    CURRENT = "current"
    READY = "ready"
    BLOCKED = "blocked"


class EvidenceState(str, Enum):
    """Read-only evidence states; omitted evidence is never clearance."""

    UNKNOWN = "unknown"
    CLEAR = "clear"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class MigrationEvidence:
    """Coordinator observations, never a substitute for apply-time locks.

    Attributes:
        lease: Whether a cooperative writer lease was observed held.
        writers: Whether all relevant writers were verified stopped.
        service: Whether the service coordinator verified shutdown.
        journals: Whether recovery found incomplete or corrupt transactions.
        verified_transactions: Transaction directories verified terminal by
            the journal parser. Inventory never guesses event semantics.
    """

    lease: EvidenceState = EvidenceState.UNKNOWN
    writers: EvidenceState = EvidenceState.UNKNOWN
    service: EvidenceState = EvidenceState.UNKNOWN
    journals: EvidenceState = EvidenceState.UNKNOWN
    verified_transactions: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        """Snapshot caller-owned sequences."""
        object.__setattr__(
            self, "verified_transactions", tuple(self.verified_transactions)
        )


@dataclass(frozen=True)
class MigrationContext:
    """Immutable explicit inputs for a side-effect-free inventory.

    Attributes:
        layout: Canonical and legacy locations, including injected roots.
        layers: Environment sources in descending precedence, ahead of
            managed config, host config, and system secrets respectively.
        evidence: External read-only observations; defaults to unknown.
    """

    layout: PathLayout
    layers: tuple[EnvLayer, ...] = field(default=(), repr=False)
    evidence: MigrationEvidence = field(default_factory=MigrationEvidence)

    def __post_init__(self) -> None:
        """Prevent changes through mutable environment source mappings."""
        object.__setattr__(
            self,
            "layers",
            tuple(
                EnvLayer(layer.source, MappingProxyType(dict(layer.values)))
                for layer in self.layers
            ),
        )


@dataclass(frozen=True)
class MigrationAction:
    """One input tree/file and its canonical destination.

    Attributes:
        code: Stable operation identifier.
        scope: Stable resource scope.
        source: Legacy input path; state actions include the whole tree.
        destination: Canonical target, never a merge authorization.
    """

    code: str
    scope: str
    source: Path
    destination: Path

    def as_dict(self) -> dict[str, object]:
        """Return redacted JSON primitives for the public renderer."""
        return {
            key: redact_secrets(str(value))
            for key, value in (
                ("code", self.code),
                ("scope", self.scope),
                ("source", self.source),
                ("destination", self.destination),
            )
        }


@dataclass(frozen=True)
class MigrationFinding:
    """A value-free diagnostic, or an explicit unmet apply prerequisite.

    Attributes:
        code: Stable diagnostic identifier.
        scope: Stable resource scope.
        paths: Relevant paths, redacted at the rendering boundary.
        detail: Safe explanatory text; raw exception output is forbidden.
        blocking: Whether this finding blocks local inventory readiness.
    """

    code: str
    scope: str
    paths: tuple[Path, ...] = ()
    detail: str = ""
    blocking: bool = True

    def __post_init__(self) -> None:
        """Freeze caller-owned path collections."""
        object.__setattr__(self, "paths", tuple(sorted(self.paths)))

    def as_dict(self) -> dict[str, object]:
        """Return a secret-safe diagnostic with stable path ordering."""
        return {
            "code": redact_secrets(self.code),
            "scope": redact_secrets(self.scope),
            "paths": [
                redact_secrets(str(path)) for path in sorted(self.paths)
            ],
            "detail": redact_secrets(self.detail),
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class MigrationReport:
    """One deterministic inventory result shared by text and JSON output.

    Attributes:
        status: Public current/ready/blocked local inventory outcome.
        actions: Planned data migration operations, never service changes.
        findings: Blockers and explicit external prerequisites.
    """

    status: MigrationStatus
    actions: tuple[MigrationAction, ...] = ()
    findings: tuple[MigrationFinding, ...] = ()

    def __post_init__(self) -> None:
        """Freeze and order public records independently of input order."""
        object.__setattr__(
            self,
            "actions",
            tuple(
                sorted(
                    self.actions,
                    key=lambda a: (
                        a.scope,
                        str(a.source),
                        str(a.destination),
                        a.code,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "findings",
            tuple(
                sorted(
                    self.findings,
                    key=lambda f: (
                        f.scope,
                        tuple(map(str, f.paths)),
                        f.code,
                        f.detail,
                        f.blocking,
                    ),
                )
            ),
        )

    @property
    def exit_code(self) -> int:
        """Return the stable shell status for this inventory outcome."""
        return {
            MigrationStatus.CURRENT: 0,
            MigrationStatus.READY: 2,
            MigrationStatus.BLOCKED: 1,
        }[self.status]

    def as_dict(self) -> dict[str, object]:
        """Render schema version one using only JSON primitives."""
        return {
            "schema_version": 1,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "actions": [action.as_dict() for action in self.actions],
            "findings": [finding.as_dict() for finding in self.findings],
        }
