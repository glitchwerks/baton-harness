"""Behavioral tests for packaged runtime resources and config mirrors."""

from __future__ import annotations

from pathlib import Path

import pytest

from baton_harness.resources import (
    RESOURCE_NAMES,
    PackagedResourceError,
    as_path,
    assert_config_mirrors,
    read_bytes,
    resource,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_NAMES = (
    "WORKFLOW.md",
    "ruleset.main.json",
    "ruleset.feature.json",
    "ruleset.compare-keys.json",
    "ruleset.compare-keys.app.json",
)


def test_packaged_resource_manifest_is_complete() -> None:
    """Removing an approved runtime resource breaks the package contract."""
    assert RESOURCE_NAMES == _EXPECTED_NAMES
    assert all(resource(name).is_file() for name in _EXPECTED_NAMES)


def test_packaged_resources_match_config_mirrors() -> None:
    """Editing only a compatibility mirror is detected as drift."""
    assert_config_mirrors(_REPO_ROOT)
    assert all(
        read_bytes(name) == (_REPO_ROOT / "config" / name).read_bytes()
        for name in _EXPECTED_NAMES
    )


def test_missing_config_mirror_fails_closed(tmp_path: Path) -> None:
    """Omitting a required compatibility mirror cannot pass validation."""
    with pytest.raises(PackagedResourceError, match="missing config mirror"):
        assert_config_mirrors(tmp_path)


def test_different_config_mirror_fails_closed(tmp_path: Path) -> None:
    """Different canonical and compatibility bytes cannot pass validation."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in _EXPECTED_NAMES:
        (config_dir / name).write_bytes(read_bytes(name))
    (config_dir / "WORKFLOW.md").write_text(
        "drifted\n", encoding="utf-8"
    )

    with pytest.raises(PackagedResourceError, match="config mirror drift"):
        assert_config_mirrors(tmp_path)


def test_unknown_resource_name_fails_closed() -> None:
    """A caller cannot escape the fixed internal resource manifest."""
    with pytest.raises(
        PackagedResourceError, match="unknown packaged resource"
    ):
        resource("../pyproject.toml")


def test_as_path_keeps_workflow_available() -> None:
    """Path-only loaders receive the complete packaged workflow bytes."""
    with as_path("WORKFLOW.md") as path:
        assert path.is_file()
        assert path.read_bytes() == read_bytes("WORKFLOW.md")
