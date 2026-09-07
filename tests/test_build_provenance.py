"""Tests for build-time provenance identity resolution."""

import importlib.util
import json
import os
import subprocess
import tarfile
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


def test_sdist_excludes_ignored_workspace_state(tmp_path: Path) -> None:
    """An sdist cannot capture ignored runtime, ledger, or scratch files."""
    root = Path(__file__).resolve().parents[1]
    marker_name = "sdist-boundary-sentinel.txt"
    markers = [
        root / directory / marker_name
        for directory in (".baton-harness", ".superpowers", ".tmp")
    ]
    created_directories: list[Path] = []
    for marker in markers:
        if not marker.parent.exists():
            marker.parent.mkdir()
            created_directories.append(marker.parent)
        marker.write_text("must not ship\n", encoding="utf-8")

    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    environment = {
        **os.environ,
        "CODEREEVE_BUILD_VERSION": "0.2.0",
        "CODEREEVE_BUILD_SOURCE_REVISION": revision,
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
    }
    output = tmp_path / "dist"
    try:
        subprocess.run(
            [
                "uv",
                "build",
                "--sdist",
                "--no-build-isolation",
                "--out-dir",
                str(output),
            ],
            cwd=root,
            check=True,
            env=environment,
        )
        (sdist,) = output.glob("codereeve-0.2.0.tar.gz")
        with tarfile.open(sdist) as archive:
            members = archive.getnames()

        for marker in markers:
            relative_marker = marker.relative_to(root).as_posix()
            assert not any(
                member.endswith(f"/{relative_marker}") for member in members
            ), f"sdist captured ignored workspace file {marker}"
    finally:
        for marker in markers:
            marker.unlink(missing_ok=True)
        for directory in created_directories:
            directory.rmdir()


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
            "CODEREEVE_BUILD_VERSION": "1.2.3",
            "CODEREEVE_BUILD_SOURCE_REVISION": REVISION.upper(),
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
            "CODEREEVE_BUILD_VERSION": "v1.2.3",
            "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
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
        {"CODEREEVE_BUILD_DEVELOPMENT": "1"},
        read_head=lambda _root: REVISION,
    )
    assert identity.package_version == "0.2.0.dev0"
    assert identity.source_revision == REVISION
    assert identity.development is True


@pytest.mark.parametrize(
    ("environment", "read_head", "create_lock", "create_git"),
    [
        ({}, REVISION, True, False),
        ({"CODEREEVE_BUILD_VERSION": "1.2.3"}, REVISION, True, False),
        ({"CODEREEVE_BUILD_SOURCE_REVISION": REVISION}, REVISION, True, False),
        (
            {
                "CODEREEVE_BUILD_VERSION": "not a version",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
            },
            REVISION,
            True,
            False,
        ),
        (
            {
                "CODEREEVE_BUILD_VERSION": "1.2.3",
                "CODEREEVE_BUILD_SOURCE_REVISION": "abc",
            },
            REVISION,
            True,
            False,
        ),
        (
            {
                "CODEREEVE_BUILD_VERSION": "1.2.3",
                "CODEREEVE_BUILD_SOURCE_REVISION": "g" * 40,
            },
            REVISION,
            True,
            False,
        ),
        ({"CODEREEVE_BUILD_DEVELOPMENT": "true"}, REVISION, True, False),
        (
            {
                "CODEREEVE_BUILD_DEVELOPMENT": "1",
                "CODEREEVE_BUILD_VERSION": "1.2.3",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
            },
            REVISION,
            True,
            False,
        ),
        (
            {
                "CODEREEVE_BUILD_VERSION": "1.2.3",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
            },
            "f" * 40,
            True,
            True,
        ),
        (
            {
                "CODEREEVE_BUILD_VERSION": "1.2.3",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
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
    record_path = tmp_path / "src" / "codereeve" / "build_provenance.json"
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
                "CODEREEVE_BUILD_VERSION": "1.2.3",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
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
    record_path = tmp_path / "src" / "codereeve" / "build_provenance.json"
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
                "CODEREEVE_BUILD_VERSION": "1.2.3",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
            },
            read_head=lambda _root: REVISION,
        )


@pytest.mark.parametrize(
    ("canonical", "legacy", "canonical_value", "legacy_value", "other"),
    [
        (
            "CODEREEVE_BUILD_VERSION",
            "BH_BUILD_VERSION",
            "0.2.0",
            "9.9.9",
            {"CODEREEVE_BUILD_SOURCE_REVISION": REVISION},
        ),
        (
            "CODEREEVE_BUILD_SOURCE_REVISION",
            "BH_BUILD_SOURCE_REVISION",
            REVISION,
            "f" * 40,
            {"CODEREEVE_BUILD_VERSION": "0.2.0"},
        ),
        (
            "CODEREEVE_BUILD_DEVELOPMENT",
            "BH_BUILD_DEVELOPMENT",
            "1",
            "0",
            {},
        ),
    ],
)
def test_conflicting_canonical_and_legacy_build_values_fail_closed(
    tmp_path: Path,
    canonical: str,
    legacy: str,
    canonical_value: str,
    legacy_value: str,
    other: dict[str, str],
) -> None:
    """Every conflicting alias pair fails without exposing either value."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    environment = {
        canonical: canonical_value,
        legacy: legacy_value,
        **other,
    }

    with pytest.raises(BuildProvenanceError) as caught:
        resolve_build_provenance(
            tmp_path,
            environment,
            read_head=lambda _root: REVISION,
        )

    message = str(caught.value)
    assert canonical in message
    assert legacy in message
    assert canonical_value not in message
    assert legacy_value not in message


@pytest.mark.parametrize(
    ("environment", "development"),
    [
        (
            {
                "CODEREEVE_BUILD_VERSION": "0.2.0",
                "BH_BUILD_VERSION": "0.2.0",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
                "BH_BUILD_SOURCE_REVISION": REVISION,
            },
            False,
        ),
        (
            {
                "CODEREEVE_BUILD_DEVELOPMENT": "1",
                "BH_BUILD_DEVELOPMENT": "1",
            },
            True,
        ),
    ],
)
def test_equal_canonical_and_legacy_build_values_succeed(
    tmp_path: Path,
    environment: dict[str, str],
    development: bool,
) -> None:
    """Equal canonical and compatibility aliases resolve unambiguously."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)

    identity = resolve_build_provenance(
        tmp_path,
        environment,
        read_head=lambda _root: REVISION,
    )

    assert identity.development is development
    assert identity.source_revision == REVISION


def test_legacy_build_variables_remain_compatible(tmp_path: Path) -> None:
    """Temporary legacy aliases preserve existing build automation."""
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    identity = resolve_build_provenance(
        tmp_path,
        {
            "BH_BUILD_VERSION": "0.2.0",
            "BH_BUILD_SOURCE_REVISION": REVISION,
        },
        read_head=lambda _root: REVISION,
    )
    assert identity.package_version == "0.2.0"
