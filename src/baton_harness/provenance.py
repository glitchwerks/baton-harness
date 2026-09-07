"""Load and validate the immutable provenance embedded in distributions."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from importlib import metadata

_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "package_version",
        "source_revision",
        "lock_identity",
        "development",
    }
)
_SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_LOCK_IDENTITY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProvenanceError(ValueError):
    """Raised when runtime artifact provenance cannot be validated."""


@dataclass(frozen=True)
class Provenance:
    """The complete, versioned provenance embedded in a distribution."""

    schema_version: int
    package_version: str
    source_revision: str
    lock_identity: str
    development: bool

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-compatible representation of this record."""
        return asdict(self)


def validate_provenance(
    raw: object,
    installed_version: str,
) -> Provenance:
    """Validate an untrusted provenance object against the runtime contract.

    Args:
        raw: JSON-decoded provenance content.
        installed_version: Version reported by installed distribution metadata.

    Returns:
        The immutable, validated provenance record.

    Raises:
        ProvenanceError: If the record's schema, values, or version mismatch.
    """
    if not isinstance(raw, dict):
        raise ProvenanceError("runtime provenance must be a JSON object")

    fields = set(raw)
    unexpected = fields - _RECORD_KEYS
    missing = _RECORD_KEYS - fields
    if unexpected:
        raise ProvenanceError("runtime provenance has unexpected fields")
    if missing:
        raise ProvenanceError("runtime provenance has missing fields")

    schema_version = raw["schema_version"]
    package_version = raw["package_version"]
    source_revision = raw["source_revision"]
    lock_identity = raw["lock_identity"]
    development = raw["development"]
    if (
        type(schema_version) is not int
        or type(package_version) is not str
        or type(source_revision) is not str
        or type(lock_identity) is not str
        or type(development) is not bool
    ):
        raise ProvenanceError("runtime provenance has invalid field types")
    if schema_version != 1:
        raise ProvenanceError(
            "runtime provenance has unsupported schema version"
        )
    if not _SOURCE_REVISION_PATTERN.fullmatch(source_revision):
        raise ProvenanceError("runtime provenance has invalid source revision")
    if not _LOCK_IDENTITY_PATTERN.fullmatch(lock_identity):
        raise ProvenanceError("runtime provenance has invalid lock identity")
    if package_version != installed_version:
        raise ProvenanceError(
            "runtime provenance does not match the installed version"
        )

    return Provenance(
        schema_version=schema_version,
        package_version=package_version,
        source_revision=source_revision,
        lock_identity=lock_identity,
        development=development,
    )


def load_provenance() -> Provenance:
    """Read and validate packaged provenance for the installed distribution.

    Returns:
        The immutable, validated provenance record.

    Raises:
        ProvenanceError: If the packaged record or distribution is unavailable.
    """
    try:
        distribution = metadata.distribution("baton-harness")
    except metadata.PackageNotFoundError as exc:
        raise ProvenanceError("runtime provenance is unavailable") from exc

    files = distribution.files
    if files is None:
        raise ProvenanceError(
            "runtime provenance package file inventory is unavailable"
        )
    matches = [
        file
        for file in files
        if str(file) == "baton_harness/build_provenance.json"
    ]
    if len(matches) != 1:
        raise ProvenanceError(
            "runtime provenance package file inventory is invalid"
        )

    try:
        text = matches[0].read_text(encoding="utf-8")
        raw = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("runtime provenance is unavailable") from exc
    return validate_provenance(raw, installed_version=distribution.version)
