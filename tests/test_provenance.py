"""Tests for runtime build provenance validation and loading."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from codereeve.provenance import (
    ProvenanceError,
    load_provenance,
    validate_provenance,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"


def valid_record(**overrides: object) -> dict[str, object]:
    """Return a hand-authored valid provenance record for validation tests."""
    record: dict[str, object] = {
        "schema_version": 1,
        "package_version": "1.2.3",
        "source_revision": REVISION,
        "lock_identity": f"sha256:{'a' * 64}",
        "development": False,
    }
    record.update(overrides)
    return record


def test_validate_provenance_requires_exact_schema() -> None:
    """Reject records with fields outside the supported schema."""
    raw = valid_record(unexpected=True)

    with pytest.raises(ProvenanceError, match="unexpected fields"):
        validate_provenance(raw, installed_version="1.2.3")


def test_validate_provenance_rejects_distribution_mismatch() -> None:
    """Reject a record that does not identify the installed distribution."""
    raw = valid_record(package_version="1.2.3")

    with pytest.raises(ProvenanceError, match="installed version"):
        validate_provenance(raw, installed_version="1.2.4")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "object"),
        (valid_record(schema_version=True), "field types"),
        (valid_record(schema_version=2), "schema version"),
        (valid_record(package_version=123), "field types"),
        (valid_record(source_revision=REVISION.upper()), "source revision"),
        (valid_record(lock_identity=f"sha256:{'A' * 64}"), "lock identity"),
        (valid_record(development=0), "field types"),
    ],
)
def test_validate_provenance_rejects_invalid_values(
    raw: object,
    message: str,
) -> None:
    """Reject unsupported root values, types, versions, and digests."""
    with pytest.raises(ProvenanceError, match=message):
        validate_provenance(raw, installed_version="1.2.3")


def _distribution(*, files: object, version: str = "1.2.3") -> MagicMock:
    """Return a distribution double with an explicit file inventory."""
    distribution = MagicMock()
    distribution.files = files
    distribution.version = version
    return distribution


def _provenance_file(
    text: str = "",
    *,
    path: str = "codereeve/build_provenance.json",
) -> MagicMock:
    """Return one inventory path for a packaged provenance resource."""
    resource = MagicMock()
    resource.configure_mock(**{"__str__.return_value": path})
    resource.read_text.return_value = text
    return resource


def test_load_provenance_rejects_invalid_json() -> None:
    """Report malformed packaged JSON without leaking decoder details."""
    with (
        patch(
            "codereeve.provenance.metadata.distribution",
            return_value=_distribution(files=[_provenance_file("{")]),
        ) as distribution_lookup,
        pytest.raises(
            ProvenanceError,
            match="runtime provenance is unavailable",
        ),
    ):
        load_provenance()
    distribution_lookup.assert_called_once_with("codereeve")


@pytest.mark.parametrize(
    "files",
    [None, [], [_provenance_file(), _provenance_file()]],
    ids=["unknown", "missing", "duplicate"],
)
def test_load_provenance_rejects_invalid_file_inventory(
    files: object,
) -> None:
    """Reject absent, missing, and ambiguous provenance inventory entries."""
    with (
        patch(
            "codereeve.provenance.metadata.distribution",
            return_value=_distribution(files=files),
        ),
        pytest.raises(
            ProvenanceError,
            match="package file inventory",
        ),
    ):
        load_provenance()


def test_load_provenance_rejects_unreadable_inventory_file() -> None:
    """Report an unreadable inventory file with a stable error."""
    resource = _provenance_file()
    resource.read_text.side_effect = OSError

    with (
        patch(
            "codereeve.provenance.metadata.distribution",
            return_value=_distribution(files=[resource]),
        ),
        pytest.raises(
            ProvenanceError,
            match="runtime provenance is unavailable",
        ),
    ):
        load_provenance()


def test_load_provenance_rejects_legacy_package_location() -> None:
    """A Baton-era resource cannot satisfy canonical runtime identity."""
    legacy = _provenance_file(
        json.dumps(valid_record()),
        path="baton_harness/build_provenance.json",
    )

    with (
        patch(
            "codereeve.provenance.metadata.distribution",
            return_value=_distribution(files=[legacy]),
        ),
        pytest.raises(ProvenanceError, match="package file inventory"),
    ):
        load_provenance()


def test_load_provenance_reads_the_real_editable_distribution() -> None:
    """Load the generated provenance retained by this editable installation."""
    record = load_provenance()

    assert record.package_version == "0.2.0.dev0"
    assert record.development is True
