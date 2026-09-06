"""Tests for the frozen wheel foundation verifier."""

from __future__ import annotations

import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import baton_harness.verify_foundation as verify_foundation
from baton_harness.resources import RESOURCE_NAMES
from baton_harness.verify_foundation import (
    EXPECTED_ENTRY_POINTS,
    FoundationError,
    _dev_only_distributions,
    _smoke_entry_points,
    _validate_installed_state,
    inspect_wheel,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _RecordingRunner:
    """Record external commands while emulating uv filesystem outputs."""

    def __init__(self, fail_command: tuple[str, ...] | None = None) -> None:
        """Initialize the runner.

        Args:
            fail_command: Optional command prefix that should return failure.
        """
        self.fail_command = fail_command
        self.calls: list[
            tuple[tuple[str, ...], Path, Mapping[str, str] | None, str | None]
        ] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> CompletedProcess[str]:
        """Record a command and create the outputs expected from uv."""
        normalized = tuple(str(part) for part in command)
        self.calls.append((normalized, cwd, env, input_text))
        should_fail = (
            self.fail_command is not None
            and normalized[: len(self.fail_command)] == self.fail_command
        )
        if should_fail:
            return CompletedProcess(normalized, 1, "", "lock stale")
        if normalized[:2] == ("uv", "build"):
            output = Path(normalized[normalized.index("--out-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            _write_wheel(output)
        if normalized[:2] == ("uv", "export"):
            output = Path(normalized[normalized.index("--output-file") + 1])
            packages = "jinja2==3.1.6\n"
            if "--extra" in normalized:
                packages += (
                    "hatchling==1.27.0\nmypy==2.3.1\npytest==9.1.1\n"
                    "ruff==0.15.20\n"
                    "types-pyyaml==6.0.12\n"
                )
            output.write_text(packages, encoding="utf-8")
        return CompletedProcess(normalized, 0, "", "")


class _SmokeRunner:
    """Emulate documented safe exit codes for installed entry points."""

    def __init__(self) -> None:
        """Initialize an empty call log."""
        self.calls: list[
            tuple[tuple[str, ...], Path, Mapping[str, str] | None, str | None]
        ] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> CompletedProcess[str]:
        """Return each original command's side-effect-free smoke status."""
        normalized = tuple(str(part) for part in command)
        self.calls.append((normalized, cwd, env, input_text))
        command_name = Path(normalized[0]).stem
        lifecycle = {"bh-after-create", "bh-before-run", "bh-after-run"}
        return CompletedProcess(
            normalized,
            1 if command_name in lifecycle else 0,
            "",
            "",
        )


def _write_wheel(
    root: Path,
    *,
    resources: tuple[str, ...] = RESOURCE_NAMES,
    entry_points: frozenset[str] | None = None,
    duplicate_resource: str | None = None,
) -> Path:
    """Write a minimal wheel-like ZIP for archive validation.

    Args:
        root: Directory receiving the archive.
        resources: Package resource basenames to include.
        entry_points: Console command names to declare.
        duplicate_resource: Optional resource to add at a second path.

    Returns:
        Path to the generated archive.
    """
    wheel = root / "baton_harness-0.1.0-py3-none-any.whl"
    scripts = EXPECTED_ENTRY_POINTS if entry_points is None else entry_points
    declarations = "\n".join(
        f"{name} = baton_harness.fake:main" for name in sorted(scripts)
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in resources:
            archive.writestr(f"baton_harness/resources/{name}", b"test")
        if duplicate_resource is not None:
            archive.writestr(
                f"shadow/baton_harness/resources/{duplicate_resource}",
                b"duplicate",
            )
        archive.writestr(
            "baton_harness-0.1.0.dist-info/entry_points.txt",
            f"[console_scripts]\n{declarations}\n",
        )
    return wheel


def test_cli_defaults_to_python_floor_and_313(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping either required Python endpoint breaks default validation."""
    calls: list[tuple[Path, tuple[str, ...], bool]] = []

    def record_verification(
        root: Path,
        python_versions: tuple[str, ...],
        *,
        keep_temp: bool = False,
    ) -> None:
        calls.append((root, python_versions, keep_temp))

    monkeypatch.setattr(
        verify_foundation, "verify_repository", record_verification
    )

    assert verify_foundation.main([]) == 0
    assert calls == [(Path.cwd(), ("3.10", "3.13"), False)]


def test_complete_wheel_archive_passes(tmp_path: Path) -> None:
    """A wheel carrying every resource and entry point is accepted."""
    inspect_wheel(_write_wheel(tmp_path))


def test_wheel_without_resource_fails_closed(tmp_path: Path) -> None:
    """Omitting one runtime default from the wheel cannot pass validation."""
    wheel = _write_wheel(tmp_path, resources=RESOURCE_NAMES[1:])

    with pytest.raises(FoundationError, match="wheel resource missing"):
        inspect_wheel(wheel)


def test_wheel_with_duplicate_resource_fails_closed(tmp_path: Path) -> None:
    """Shipping ambiguous duplicate runtime defaults is rejected."""
    wheel = _write_wheel(tmp_path, duplicate_resource="WORKFLOW.md")

    with pytest.raises(FoundationError, match="wheel resource duplicated"):
        inspect_wheel(wheel)


def test_wheel_without_entry_point_fails_closed(tmp_path: Path) -> None:
    """Omitting an installed command wrapper cannot pass validation."""
    wheel = _write_wheel(
        tmp_path,
        entry_points=EXPECTED_ENTRY_POINTS - {"bh-daemon"},
    )

    with pytest.raises(FoundationError, match="wheel entry points missing"):
        inspect_wheel(wheel)


def test_repository_verification_runs_locked_install_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reordering or omitting a frozen-install phase breaks validation."""
    runner = _RecordingRunner()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    verify_foundation.verify_repository(_REPO_ROOT, ("3.10",), runner=runner)

    commands = [call[0] for call in runner.calls]
    assert commands[0] == ("uv", "lock", "--check")
    assert commands[1][:4] == (
        "uv",
        "export",
        "--locked",
        "--no-emit-project",
    )
    assert commands[2][:5] == (
        "uv",
        "export",
        "--locked",
        "--extra",
        "dev",
    )
    assert commands[3][:2] == ("uv", "build")
    assert "--locked" not in commands[3]
    assert "--build-constraints" in commands[3]
    assert "--require-hashes" in commands[3]
    assert "--sdist" in commands[3]
    assert "--wheel" in commands[3]
    assert commands[4][0:2] == ("uv", "venv")
    assert commands[4][-2:] == ("--python", "3.10")
    assert commands[5][0:3] == ("uv", "pip", "sync")
    assert commands[6][0:3] == ("uv", "pip", "install")
    assert "--no-deps" in commands[6]
    assert commands[7][0:3] == ("uv", "pip", "check")
    assert commands[8][1] == "--installed-smoke"
    forbidden = {
        commands[8][index + 1]
        for index, argument in enumerate(commands[8])
        if argument == "--forbid-distribution"
    }
    assert forbidden == {
        "hatchling",
        "mypy",
        "pytest",
        "ruff",
        "types-pyyaml",
    }
    assert runner.calls[8][1].is_relative_to(tmp_path)
    assert runner.calls[8][1] != _REPO_ROOT
    assert runner.calls[8][2] is not None
    assert "PYTHONPATH" not in runner.calls[8][2]


def test_repository_verification_stops_on_stale_lock() -> None:
    """A stale lock fails before mirror, build, or installation work."""
    runner = _RecordingRunner(("uv", "lock", "--check"))

    with pytest.raises(FoundationError, match="uv lock --check.*lock stale"):
        verify_foundation.verify_repository(
            _REPO_ROOT, ("3.10",), runner=runner
        )

    assert len(runner.calls) == 1


def test_dev_only_distributions_exclude_runtime_closure(
    tmp_path: Path,
) -> None:
    """A dev dependency used at runtime is not falsely forbidden."""
    runtime_requirements = tmp_path / "runtime.txt"
    runtime_requirements.write_text(
        "# generated\njinja2==3.1.6\nmarkupsafe==3.0.2\n",
        encoding="utf-8",
    )
    dev_requirements = tmp_path / "dev.txt"
    dev_requirements.write_text(
        "# generated\njinja2==3.1.6\nmarkupsafe==3.0.2\n"
        "pytest==9.1.1\nruff==0.15.20\n",
        encoding="utf-8",
    )

    assert _dev_only_distributions(
        runtime_requirements, dev_requirements
    ) == frozenset({"pytest", "ruff"})


def test_complete_installed_state_passes(tmp_path: Path) -> None:
    """A non-editable in-prefix install with all commands is accepted."""
    package_file = tmp_path / "venv" / "site-packages" / "baton_harness"
    package_file.mkdir(parents=True)
    _validate_installed_state(
        direct_url_json='{"archive_info": {}}',
        package_file=package_file / "__init__.py",
        prefix=tmp_path / "venv",
        entry_points=EXPECTED_ENTRY_POINTS,
        installed_distributions=frozenset({"baton-harness", "jinja2"}),
        forbidden_distributions=frozenset({"pytest", "ruff"}),
    )


def test_editable_installed_state_fails_closed(tmp_path: Path) -> None:
    """An editable wheel replacement cannot pass production validation."""
    package_file = tmp_path / "venv" / "site-packages" / "baton_harness.py"

    with pytest.raises(FoundationError, match="editable installation"):
        _validate_installed_state(
            direct_url_json='{"dir_info": {"editable": true}}',
            package_file=package_file,
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS,
            installed_distributions=frozenset({"baton-harness"}),
            forbidden_distributions=frozenset(),
        )


def test_package_imported_outside_environment_fails_closed(
    tmp_path: Path,
) -> None:
    """A source-checkout import cannot masquerade as the installed wheel."""
    with pytest.raises(FoundationError, match="outside environment"):
        _validate_installed_state(
            direct_url_json=None,
            package_file=tmp_path / "checkout" / "baton_harness.py",
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS,
            installed_distributions=frozenset({"baton-harness"}),
            forbidden_distributions=frozenset(),
        )


def test_missing_installed_entry_point_fails_closed(tmp_path: Path) -> None:
    """A missing generated wrapper cannot pass installed validation."""
    with pytest.raises(
        FoundationError, match="installed entry points missing"
    ):
        _validate_installed_state(
            direct_url_json=None,
            package_file=tmp_path / "venv" / "baton_harness.py",
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS - {"bh-daemon"},
            installed_distributions=frozenset({"baton-harness"}),
            forbidden_distributions=frozenset(),
        )


def test_dev_only_distribution_fails_closed(tmp_path: Path) -> None:
    """Installing a selected dev extra breaks the runtime-only invariant."""
    with pytest.raises(FoundationError, match="dev-only distributions"):
        _validate_installed_state(
            direct_url_json=None,
            package_file=tmp_path / "venv" / "baton_harness.py",
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS,
            installed_distributions=frozenset({"baton-harness", "pytest"}),
            forbidden_distributions=frozenset({"pytest", "ruff"}),
        )


def test_installed_entry_points_use_only_safe_smokes(tmp_path: Path) -> None:
    """Changing a wrapper smoke into an external workflow is rejected."""
    runner = _SmokeRunner()

    _smoke_entry_points(tmp_path / "bin", runner=runner)

    commands = {Path(call[0][0]).stem: call for call in runner.calls}
    assert set(commands) == EXPECTED_ENTRY_POINTS - {"bh-verify-foundation"}
    assert commands["bh-daemon"][0][1:] == ("--help",)
    assert commands["bh-force-pr-not-merge"][3] == "{}"
    for name in ("bh-after-create", "bh-before-run", "bh-after-run"):
        assert commands[name][0][1:] == ()
        assert commands[name][1] != _REPO_ROOT


def test_installed_smoke_mode_skips_repository_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wheel's internal smoke mode cannot recursively rebuild itself."""
    calls: list[frozenset[str]] = []

    def record_installed(forbidden_distributions: frozenset[str]) -> None:
        calls.append(forbidden_distributions)

    monkeypatch.setattr(
        verify_foundation, "verify_installed", record_installed
    )

    assert (
        verify_foundation.main(
            [
                "--installed-smoke",
                "--forbid-distribution",
                "pytest",
                "--forbid-distribution",
                "ruff",
            ]
        )
        == 0
    )
    assert calls == [frozenset({"pytest", "ruff"})]
