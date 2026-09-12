"""Installed-wheel release gate for separate upgrade environments."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile

import pytest

from tests.release_gate.fixture import load_fixture, materialize_fixture

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "codereeve-upgrade-v1.json"


def _load_driver() -> ModuleType:
    """Load the release driver for focused contract tests."""
    path = ROOT / "scripts" / "verify_installed_upgrade.py"
    spec = importlib.util.spec_from_file_location(
        "installed_upgrade_driver", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_wheel(
    path: Path,
    *,
    distribution: str,
    version: str,
    revision: str,
    lock_identity: str,
) -> None:
    """Write the minimum wheel records consumed by binding preflight."""
    normalized = distribution.replace("-", "_")
    provenance = {
        "schema_version": 1,
        "package_version": version,
        "source_revision": revision,
        "lock_identity": lock_identity,
        "development": False,
    }
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"{normalized}/build_provenance.json",
            json.dumps(provenance),
        )
        archive.writestr(
            f"{normalized}-{version}.dist-info/METADATA",
            f"Name: {distribution}\nVersion: {version}\n",
        )


def _binding_args(tmp_path: Path) -> object:
    """Create trusted expectations and matching synthetic artifact inputs."""
    old_revision = "1" * 40
    candidate_revision = "2" * 40
    old_requirements = tmp_path / "old.txt"
    candidate_requirements = tmp_path / "candidate.txt"
    old_requirements.write_bytes(b"old locked runtime\n")
    candidate_requirements.write_bytes(b"candidate locked runtime\n")
    old_lock = "sha256:" + "3" * 64
    candidate_lock = "sha256:" + "4" * 64
    old_wheel = tmp_path / "old.whl"
    candidate_wheel = tmp_path / "candidate.whl"
    _write_wheel(
        old_wheel,
        distribution="baton-harness",
        version="0.1.0+upgradefixture",
        revision=old_revision,
        lock_identity=old_lock,
    )
    _write_wheel(
        candidate_wheel,
        distribution="codereeve",
        version="0.2.0",
        revision=candidate_revision,
        lock_identity=candidate_lock,
    )
    return type(
        "Args",
        (),
        {
            "old_wheel": old_wheel,
            "candidate_wheel": candidate_wheel,
            "old_requirements": old_requirements,
            "candidate_requirements": candidate_requirements,
            "expected_old_revision": old_revision,
            "expected_candidate_revision": candidate_revision,
            "expected_old_lock_identity": old_lock,
            "expected_candidate_lock_identity": candidate_lock,
            "expected_old_runtime_sha256": hashlib.sha256(
                old_requirements.read_bytes()
            ).hexdigest(),
            "expected_candidate_runtime_sha256": hashlib.sha256(
                candidate_requirements.read_bytes()
            ).hexdigest(),
            "workspace": tmp_path / "workspace",
        },
    )()


def test_failed_command_diagnostic_omits_secret_canary(tmp_path: Path) -> None:
    """A child failure cannot copy raw output into the public exception."""
    driver = _load_driver()
    outcomes: list[dict[str, object]] = []
    canary = "SECRET_CANARY_MUST_NOT_ESCAPE"
    with pytest.raises(RuntimeError) as failure:
        driver._run(
            [
                sys.executable,
                "-c",
                f"import sys; print({canary!r}, file=sys.stderr); sys.exit(7)",
            ],
            cwd=tmp_path,
            env=dict(os.environ),
            expected=0,
            name="controlled.failure",
            outcomes=outcomes,
        )
    assert canary not in str(failure.value)
    assert str(failure.value) == "controlled.failure returned 7, expected 0"


def test_stale_candidate_revision_fails_before_environment_creation(
    tmp_path: Path,
) -> None:
    """A stale wheel cannot reach installation or smoke execution."""
    driver = _load_driver()
    args = _binding_args(tmp_path)
    args.expected_candidate_revision = "5" * 40
    with pytest.raises(
        RuntimeError, match="candidate artifact source revision"
    ):
        driver.verify(args)
    assert not args.workspace.exists()


def test_mismatched_runtime_export_fails_before_environment_creation(
    tmp_path: Path,
) -> None:
    """An unrelated requirements export cannot reach installation."""
    driver = _load_driver()
    args = _binding_args(tmp_path)
    args.expected_candidate_runtime_sha256 = "6" * 64
    with pytest.raises(RuntimeError, match="candidate runtime export digest"):
        driver.verify(args)
    assert not args.workspace.exists()


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
    expected_values = {
        name: os.environ.get(name)
        for name in (
            "CODEREEVE_RELEASE_EXPECTED_OLD_REVISION",
            "CODEREEVE_RELEASE_EXPECTED_CANDIDATE_REVISION",
            "CODEREEVE_RELEASE_EXPECTED_OLD_LOCK_IDENTITY",
            "CODEREEVE_RELEASE_EXPECTED_CANDIDATE_LOCK_IDENTITY",
            "CODEREEVE_RELEASE_EXPECTED_OLD_RUNTIME_SHA256",
            "CODEREEVE_RELEASE_EXPECTED_CANDIDATE_RUNTIME_SHA256",
        )
    }
    missing = [name for name, value in expected_values.items() if not value]
    if missing:
        pytest.skip("explicit release identity expectations are required")
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
        "--expected-old-revision",
        expected_values["CODEREEVE_RELEASE_EXPECTED_OLD_REVISION"],
        "--expected-candidate-revision",
        expected_values["CODEREEVE_RELEASE_EXPECTED_CANDIDATE_REVISION"],
        "--expected-old-lock-identity",
        expected_values["CODEREEVE_RELEASE_EXPECTED_OLD_LOCK_IDENTITY"],
        "--expected-candidate-lock-identity",
        expected_values["CODEREEVE_RELEASE_EXPECTED_CANDIDATE_LOCK_IDENTITY"],
        "--expected-old-runtime-sha256",
        expected_values["CODEREEVE_RELEASE_EXPECTED_OLD_RUNTIME_SHA256"],
        "--expected-candidate-runtime-sha256",
        expected_values["CODEREEVE_RELEASE_EXPECTED_CANDIDATE_RUNTIME_SHA256"],
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
    assert evidence["bindings"] == {
        "candidate_lock_identity": expected_values[
            "CODEREEVE_RELEASE_EXPECTED_CANDIDATE_LOCK_IDENTITY"
        ],
        "candidate_runtime_sha256": expected_values[
            "CODEREEVE_RELEASE_EXPECTED_CANDIDATE_RUNTIME_SHA256"
        ],
        "candidate_source_revision": expected_values[
            "CODEREEVE_RELEASE_EXPECTED_CANDIDATE_REVISION"
        ],
        "old_lock_identity": expected_values[
            "CODEREEVE_RELEASE_EXPECTED_OLD_LOCK_IDENTITY"
        ],
        "old_runtime_sha256": expected_values[
            "CODEREEVE_RELEASE_EXPECTED_OLD_RUNTIME_SHA256"
        ],
        "old_source_revision": expected_values[
            "CODEREEVE_RELEASE_EXPECTED_OLD_REVISION"
        ],
    }
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
