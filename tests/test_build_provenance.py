"""Tests for build-time provenance identity resolution."""

import importlib.util
import json
from hashlib import sha256
from pathlib import Path

import pytest

from hatch_build import (
    BuildProvenance,
    BuildProvenanceError,
    resolve_build_provenance,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"
LOCK_CONTENT = b"locked\n"


def test_hook_module_loads_without_a_registered_module() -> None:
    """The Hatch custom-script loader can execute the hook module directly."""
    spec = importlib.util.spec_from_file_location(
        "unregistered_hook", "hatch_build.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_standard_identity_requires_and_preserves_assertions(
    tmp_path: Path,
) -> None:
    """Standard builds preserve each validated asserted identity field."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    identity = resolve_build_provenance(
        tmp_path,
        {
            "BH_BUILD_VERSION": "1.2.3",
            "BH_BUILD_SOURCE_REVISION": REVISION.upper(),
        },
        read_head=lambda _root: REVISION,
    )
    assert identity == BuildProvenance(
        schema_version=1,
        package_version="1.2.3",
        source_revision=REVISION,
        lock_identity=f"sha256:{sha256(LOCK_CONTENT).hexdigest()}",
        development=False,
    )


def test_standard_identity_normalizes_a_valid_pep_440_version(
    tmp_path: Path,
) -> None:
    """Standard builds store the canonical PEP 440 version spelling."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)

    identity = resolve_build_provenance(
        tmp_path,
        {
            "BH_BUILD_VERSION": "v1.2.3",
            "BH_BUILD_SOURCE_REVISION": REVISION,
        },
        read_head=lambda _root: REVISION,
    )

    assert identity.package_version == "1.2.3"


def test_development_identity_uses_stable_version_and_exact_head(
    tmp_path: Path,
) -> None:
    """Development builds use the stable version and injected HEAD."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    identity = resolve_build_provenance(
        tmp_path,
        {"BH_BUILD_DEVELOPMENT": "1"},
        read_head=lambda _root: REVISION,
    )
    assert identity.package_version == "0.1.0.dev0"
    assert identity.source_revision == REVISION
    assert identity.development is True


@pytest.mark.parametrize(
    ("environment", "read_head", "create_lock", "create_git"),
    [
        ({}, REVISION, True, False),
        ({"BH_BUILD_VERSION": "1.2.3"}, REVISION, True, False),
        ({"BH_BUILD_SOURCE_REVISION": REVISION}, REVISION, True, False),
        (
            {
                "BH_BUILD_VERSION": "not a version",
                "BH_BUILD_SOURCE_REVISION": REVISION,
            },
            REVISION,
            True,
            False,
        ),
        (
            {"BH_BUILD_VERSION": "1.2.3", "BH_BUILD_SOURCE_REVISION": "abc"},
            REVISION,
            True,
            False,
        ),
        (
            {
                "BH_BUILD_VERSION": "1.2.3",
                "BH_BUILD_SOURCE_REVISION": "g" * 40,
            },
            REVISION,
            True,
            False,
        ),
        ({"BH_BUILD_DEVELOPMENT": "true"}, REVISION, True, False),
        (
            {
                "BH_BUILD_DEVELOPMENT": "1",
                "BH_BUILD_VERSION": "1.2.3",
                "BH_BUILD_SOURCE_REVISION": REVISION,
            },
            REVISION,
            True,
            False,
        ),
        (
            {
                "BH_BUILD_VERSION": "1.2.3",
                "BH_BUILD_SOURCE_REVISION": REVISION,
            },
            "f" * 40,
            True,
            True,
        ),
        (
            {
                "BH_BUILD_VERSION": "1.2.3",
                "BH_BUILD_SOURCE_REVISION": REVISION,
            },
            REVISION,
            False,
            False,
        ),
    ],
)
def test_invalid_build_identity_inputs_fail_closed(
    tmp_path: Path,
    environment: dict[str, str],
    read_head: str,
    create_lock: bool,
    create_git: bool,
) -> None:
    """Invalid build assertions and unavailable inputs are terminal."""
    if create_lock:
        (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    if create_git:
        (tmp_path / ".git").mkdir()

    with pytest.raises(BuildProvenanceError):
        resolve_build_provenance(
            tmp_path,
            environment,
            read_head=lambda _root: read_head,
        )


def test_carried_record_must_exactly_match_resolved_identity(
    tmp_path: Path,
) -> None:
    """An sdist record cannot differ from the asserted build identity."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    record_path = tmp_path / "src" / "baton_harness" / "build_provenance.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_version": "1.2.4",
                "source_revision": REVISION,
                "lock_identity": f"sha256:{sha256(LOCK_CONTENT).hexdigest()}",
                "development": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BuildProvenanceError):
        resolve_build_provenance(
            tmp_path,
            {
                "BH_BUILD_VERSION": "1.2.3",
                "BH_BUILD_SOURCE_REVISION": REVISION,
            },
            read_head=lambda _root: REVISION,
        )


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [("schema_version", True), ("development", 0)],
)
def test_carried_record_rejects_values_with_wrong_json_types(
    tmp_path: Path,
    field: str,
    malformed_value: bool | int,
) -> None:
    """Reject carried values that compare equal but have wrong types."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    record_path = tmp_path / "src" / "baton_harness" / "build_provenance.json"
    record_path.parent.mkdir(parents=True)
    record = BuildProvenance(
        schema_version=1,
        package_version="1.2.3",
        source_revision=REVISION,
        lock_identity=f"sha256:{sha256(LOCK_CONTENT).hexdigest()}",
        development=False,
    ).as_dict()
    record[field] = malformed_value
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(BuildProvenanceError):
        resolve_build_provenance(
            tmp_path,
            {
                "BH_BUILD_VERSION": "1.2.3",
                "BH_BUILD_SOURCE_REVISION": REVISION,
            },
            read_head=lambda _root: REVISION,
        )
