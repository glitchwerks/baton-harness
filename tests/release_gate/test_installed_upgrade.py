"""Installed-wheel release gate for separate upgrade environments."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.release_gate.fixture import load_fixture, materialize_fixture

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "codereeve-upgrade-v1.json"


def _required_path(name: str) -> Path:
    """Return an explicitly configured release-gate input path.

    Args:
        name: Environment variable containing the path.

    Returns:
        Existing input path.

    Raises:
        AssertionError: If the configured path does not exist.
    """
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for the installed release gate")
    path = Path(value).resolve()
    assert path.is_file(), f"{name} does not identify a file"
    return path


def test_explicit_wheels_preserve_legacy_rollback_environment(
    tmp_path: Path,
) -> None:
    """A rejected overlapping install leaves the legacy environment intact."""
    old_wheel = _required_path("CODEREEVE_RELEASE_OLD_WHEEL")
    candidate_wheel = _required_path("CODEREEVE_RELEASE_CANDIDATE_WHEEL")
    old_requirements = _required_path("CODEREEVE_RELEASE_OLD_REQUIREMENTS")
    candidate_requirements = _required_path(
        "CODEREEVE_RELEASE_CANDIDATE_REQUIREMENTS"
    )
    python_version = os.environ.get("CODEREEVE_RELEASE_PYTHON")
    if not python_version:
        pytest.skip("CODEREEVE_RELEASE_PYTHON is required")

    fixture_root = tmp_path / "fixture"
    materialize_fixture(fixture_root, load_fixture(FIXTURE))
    fixture_before = {
        path.relative_to(fixture_root): path.read_bytes()
        for path in fixture_root.rglob("*")
        if path.is_file()
    }
    evidence_path = tmp_path / "installed-evidence.json"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "verify_installed_upgrade.py"),
        "--old-wheel",
        str(old_wheel),
        "--candidate-wheel",
        str(candidate_wheel),
        "--old-requirements",
        str(old_requirements),
        "--candidate-requirements",
        str(candidate_requirements),
        "--fixture",
        str(FIXTURE),
        "--fixture-root",
        str(fixture_root),
        "--python",
        python_version,
        "--workspace",
        str(tmp_path / "installed-workspace"),
        "--evidence",
        str(evidence_path),
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    fixture_after = {
        relative: (fixture_root / relative).read_bytes()
        for relative in fixture_before
    }
    assert evidence["schema_version"] == 1
    assert evidence["python_requested"] == python_version
    assert evidence["fixture"]["schema_version"] == 1
    assert evidence["legacy_environment"]["unchanged_after_rollback"] is True
    assert (
        evidence["legacy_environment"]["import_owned_by_environment"] is True
    )
    assert evidence["candidate_artifact"]["version"] == "0.2.0"
    assert evidence["candidate_artifact"]["development"] is False
    assert evidence["overlap_negative"]["rejected"] is True
    assert evidence["migration_rollback"] == {
        "failure_injected": True,
        "restoration_status": "complete",
    }
    assert fixture_after == fixture_before
    assert (
        "incompatible distributions installed"
        in evidence["overlap_negative"]["diagnostic"]
    )
    assert all(
        outcome["expected_returncode"] == outcome["returncode"]
        for outcome in evidence["command_outcomes"]
    )
