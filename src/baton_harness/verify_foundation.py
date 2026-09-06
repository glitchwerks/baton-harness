"""Validate the frozen wheel installation foundation."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from typing import Protocol
from zipfile import BadZipFile, ZipFile

from baton_harness.resources import (
    RESOURCE_NAMES,
    PackagedResourceError,
    assert_config_mirrors,
    read_bytes,
)

EXPECTED_ENTRY_POINTS = frozenset(
    {
        "bh-after-create",
        "bh-before-run",
        "bh-after-run",
        "bh-daemon",
        "bh-force-pr-not-merge",
        "bh-verify-foundation",
    }
)
COMMAND_TIMEOUT_SECONDS = 300.0
SMOKE_TIMEOUT_SECONDS = 30.0


class FoundationError(RuntimeError):
    """Raised when the frozen-installation foundation is inconsistent."""


class Runner(Protocol):
    """Callable interface for one captured external command."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a command and return its captured result."""


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one external validation command with captured UTF-8 output.

    Args:
        command: Executable and arguments.
        cwd: Working directory for the child process.
        env: Optional complete child environment.
        input_text: Optional standard-input text.
        timeout_seconds: Maximum command duration.

    Returns:
        Captured process result without automatic exception handling.
    """
    arguments = list(command)
    environment = None if env is None else dict(env)
    if input_text is None:
        return subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout_seconds,
        )
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=timeout_seconds,
    )


def _run_checked(
    runner: Runner,
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> None:
    """Run one command and convert non-zero status into an invariant error.

    Args:
        runner: Injected command runner.
        command: Executable and arguments.
        cwd: Working directory for the child process.
        env: Optional complete child environment.
        input_text: Optional standard-input text.

    Raises:
        FoundationError: If the child exits non-zero.
    """
    rendered = " ".join(str(part) for part in command)
    try:
        result = runner(
            command,
            cwd=cwd,
            env=env,
            input_text=input_text,
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise FoundationError(
            f"command could not complete ({rendered}): {exc}"
        ) from exc
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout).strip() or "no output"
    raise FoundationError(f"command failed ({rendered}): {detail}")


@contextmanager
def _temporary_workspace(keep: bool) -> Iterator[Path]:
    """Create a disposable or retained validation workspace.

    Args:
        keep: Whether to retain the workspace for diagnostics.

    Yields:
        Temporary workspace path.
    """
    if keep:
        retained = Path(tempfile.mkdtemp(prefix="bh-foundation-"))
        print(f"bh-verify-foundation: retaining temporary files at {retained}")
        yield retained
        return
    with tempfile.TemporaryDirectory(prefix="bh-foundation-") as raw:
        yield Path(raw)


def _venv_executable(venv: Path, name: str) -> Path:
    """Return a console executable path for the current operating system.

    Args:
        venv: Virtual-environment root.
        name: Console-script basename or ``python``.

    Returns:
        Platform-specific executable path.
    """
    if os.name == "nt":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def _normalize_distribution_name(name: str) -> str:
    """Normalize one distribution name for comparisons.

    Args:
        name: Distribution name from metadata or configuration.

    Returns:
        Lowercase PEP 503-style normalized name.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(path: Path) -> frozenset[str]:
    """Extract normalized distribution names from a uv export.

    Args:
        path: Exported requirements file.

    Returns:
        Resolved distribution names from requirement records.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise FoundationError(
            f"could not read locked requirements {path}: {exc}"
        ) from exc
    names: set[str] = set()
    for line in lines:
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line.strip())
        if match is not None:
            names.add(_normalize_distribution_name(match.group(1)))
    return frozenset(names)


def _dev_only_distributions(
    runtime_requirements: Path,
    dev_requirements: Path,
) -> frozenset[str]:
    """Return locked distributions selected only by the development extra.

    Args:
        runtime_requirements: Base-project uv export.
        dev_requirements: Base project plus ``dev`` extra uv export.

    Returns:
        Normalized distributions absent from the runtime closure.
    """
    runtime_names = _requirement_names(runtime_requirements)
    return _requirement_names(dev_requirements) - runtime_names


def _validate_installed_state(
    *,
    direct_url_json: str | None,
    package_file: Path,
    prefix: Path,
    entry_points: frozenset[str],
    installed_distributions: frozenset[str],
    forbidden_distributions: frozenset[str],
) -> None:
    """Validate collected metadata for one installed wheel environment.

    Args:
        direct_url_json: Installed ``direct_url.json`` text, when present.
        package_file: Imported package file path.
        prefix: Active virtual-environment prefix.
        entry_points: Installed console-script names.
        installed_distributions: Installed distribution names.
        forbidden_distributions: Dev-only names forbidden in production.

    Raises:
        FoundationError: If the install is editable, imported from outside
            its environment, missing a command, or includes a dev-only
            distribution.
    """
    if direct_url_json:
        try:
            direct_url = json.loads(direct_url_json)
        except json.JSONDecodeError as exc:
            raise FoundationError("invalid installed direct_url.json") from exc
        if (direct_url.get("dir_info") or {}).get("editable") is True:
            raise FoundationError("editable installation detected")

    if not package_file.resolve().is_relative_to(prefix.resolve()):
        raise FoundationError(
            f"package imported from outside environment: {package_file}"
        )

    missing = EXPECTED_ENTRY_POINTS - entry_points
    if missing:
        raise FoundationError(
            f"installed entry points missing: {', '.join(sorted(missing))}"
        )

    installed = {
        _normalize_distribution_name(name) for name in installed_distributions
    }
    forbidden = {
        _normalize_distribution_name(name) for name in forbidden_distributions
    }
    present = sorted(installed & forbidden)
    if present:
        raise FoundationError(
            f"dev-only distributions installed: {', '.join(present)}"
        )


def _smoke_entry_points(
    executable_dir: Path,
    *,
    runner: Runner = run_command,
) -> None:
    """Execute each original installed wrapper through a safe early path.

    Args:
        executable_dir: Directory containing installed console wrappers.
        runner: External command boundary, injectable for tests.

    Raises:
        FoundationError: If a wrapper returns an unexpected status.
    """
    smoke_cases = (
        ("bh-daemon", ("--help",), None, 0),
        ("bh-after-create", (), None, 1),
        ("bh-before-run", (), None, 1),
        ("bh-after-run", (), None, 1),
        ("bh-force-pr-not-merge", (), "{}", 0),
    )
    with tempfile.TemporaryDirectory(prefix="bh-entrypoint-smoke-") as raw:
        cwd = Path(raw)
        child_env = dict(os.environ)
        child_env.pop("PYTHONPATH", None)
        child_env.pop("PYTHONHOME", None)
        for name, arguments, input_text, expected_status in smoke_cases:
            executable = executable_dir / (
                f"{name}.exe" if os.name == "nt" else name
            )
            try:
                result = runner(
                    (str(executable), *arguments),
                    cwd=cwd,
                    env=child_env,
                    input_text=input_text,
                    timeout_seconds=SMOKE_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
                raise FoundationError(
                    f"entry-point smoke could not complete ({name}): {exc}"
                ) from exc
            if result.returncode != expected_status:
                detail = (
                    result.stderr or result.stdout
                ).strip() or "no output"
                raise FoundationError(
                    f"entry-point smoke failed ({name}): expected exit "
                    f"{expected_status}, got {result.returncode}: {detail}"
                )


def _read_installed_resources(
    reader: Callable[[str], bytes] = read_bytes,
) -> None:
    """Read every installed default and normalize resource failures.

    Args:
        reader: Package-resource byte reader, injectable for tests.

    Raises:
        FoundationError: If a resource is absent or unreadable.
    """
    try:
        for name in RESOURCE_NAMES:
            reader(name)
    except (PackagedResourceError, OSError, UnicodeError) as exc:
        raise FoundationError(f"installed resource unreadable: {exc}") from exc


def verify_installed(forbidden_distributions: frozenset[str]) -> None:
    """Validate the active installed wheel and its generated commands.

    Args:
        forbidden_distributions: Dev-only package names that must be absent.

    Raises:
        FoundationError: If any installed-state invariant fails.
    """
    import baton_harness

    try:
        distribution = metadata.distribution("baton-harness")
        package_file = Path(baton_harness.__file__ or "")
        entry_points = frozenset(
            entry.name
            for entry in distribution.entry_points
            if entry.group == "console_scripts"
        )
        installed_names = frozenset(
            name
            for candidate in metadata.distributions()
            if (name := candidate.metadata["Name"]) is not None
        )
        direct_url_json = distribution.read_text("direct_url.json")
    except (
        metadata.PackageNotFoundError,
        OSError,
        UnicodeError,
        configparser.Error,
    ) as exc:
        raise FoundationError(
            f"could not read installed metadata: {exc}"
        ) from exc
    _validate_installed_state(
        direct_url_json=direct_url_json,
        package_file=package_file,
        prefix=Path(sys.prefix),
        entry_points=entry_points,
        installed_distributions=installed_names,
        forbidden_distributions=forbidden_distributions,
    )
    _read_installed_resources()

    executable_dir = Path(sys.executable).resolve().parent
    _smoke_entry_points(executable_dir)


def _wheel_entry_points(archive: ZipFile) -> frozenset[str]:
    """Read console-script names from one wheel archive.

    Args:
        archive: Open wheel ZIP archive.

    Returns:
        Declared console-script names, or an empty set when metadata is
        absent.
    """
    metadata_files = [
        name
        for name in archive.namelist()
        if name.endswith(".dist-info/entry_points.txt")
    ]
    if len(metadata_files) != 1:
        return frozenset()
    parser = configparser.ConfigParser()
    parser.read_string(archive.read(metadata_files[0]).decode("utf-8"))
    if not parser.has_section("console_scripts"):
        return frozenset()
    return frozenset(parser["console_scripts"])


def inspect_wheel(wheel: Path) -> None:
    """Require one wheel to contain all resources and console scripts.

    Args:
        wheel: Wheel archive to inspect.

    Raises:
        FoundationError: If a resource or entry point is absent or
            duplicated.
    """
    try:
        with ZipFile(wheel) as archive:
            archive_names = archive.namelist()
            for resource_name in RESOURCE_NAMES:
                expected = f"baton_harness/resources/{resource_name}"
                count = archive_names.count(expected)
                if count == 0:
                    raise FoundationError(
                        f"wheel resource missing: {resource_name}"
                    )
                if count > 1:
                    raise FoundationError(
                        f"wheel resource duplicated: {resource_name}"
                    )

            missing = EXPECTED_ENTRY_POINTS - _wheel_entry_points(archive)
            if missing:
                raise FoundationError(
                    f"wheel entry points missing: {', '.join(sorted(missing))}"
                )
    except FoundationError:
        raise
    except (OSError, BadZipFile, UnicodeError, configparser.Error) as exc:
        raise FoundationError(
            f"could not inspect wheel {wheel}: {exc}"
        ) from exc


def verify_repository(
    root: Path,
    python_versions: Sequence[str],
    *,
    keep_temp: bool = False,
    runner: Runner = run_command,
) -> None:
    """Validate repository, wheel, and locked installed environments.

    Args:
        root: Harness repository root.
        python_versions: Python versions to exercise.
        keep_temp: Whether diagnostic artifacts should be retained.
        runner: External command boundary, injectable for tests.

    Raises:
        FoundationError: If any foundation invariant fails.
    """
    root = root.resolve()
    if not (root / "pyproject.toml").is_file():
        raise FoundationError(f"project root missing pyproject.toml: {root}")

    _run_checked(runner, ("uv", "lock", "--check"), cwd=root)
    try:
        assert_config_mirrors(root)
    except PackagedResourceError as exc:
        raise FoundationError(str(exc)) from exc

    with _temporary_workspace(keep_temp) as workspace:
        distributions = workspace / "dist"
        requirements = workspace / "requirements.txt"
        dev_requirements = workspace / "requirements-dev.txt"
        _run_checked(
            runner,
            (
                "uv",
                "export",
                "--locked",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements),
            ),
            cwd=root,
        )
        _run_checked(
            runner,
            (
                "uv",
                "export",
                "--locked",
                "--extra",
                "dev",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                str(dev_requirements),
            ),
            cwd=root,
        )
        forbidden_distributions = _dev_only_distributions(
            requirements, dev_requirements
        )
        _run_checked(
            runner,
            (
                "uv",
                "build",
                "--build-constraints",
                str(dev_requirements),
                "--require-hashes",
                "--sdist",
                "--wheel",
                "--out-dir",
                str(distributions),
            ),
            cwd=root,
        )
        wheels = sorted(distributions.glob("*.whl"))
        if len(wheels) != 1:
            raise FoundationError(
                f"expected exactly one built wheel, found {len(wheels)}"
            )
        wheel = wheels[0]
        inspect_wheel(wheel)

        smoke_cwd = workspace / "outside-checkout"
        smoke_cwd.mkdir()
        child_env = dict(os.environ)
        child_env.pop("PYTHONPATH", None)
        child_env.pop("PYTHONHOME", None)
        for version in python_versions:
            venv = workspace / f"python-{version}"
            python = _venv_executable(venv, "python")
            verifier = _venv_executable(venv, "bh-verify-foundation")
            _run_checked(
                runner,
                ("uv", "venv", str(venv), "--python", version),
                cwd=smoke_cwd,
                env=child_env,
            )
            _run_checked(
                runner,
                (
                    "uv",
                    "pip",
                    "sync",
                    "--python",
                    str(python),
                    str(requirements),
                ),
                cwd=smoke_cwd,
                env=child_env,
            )
            _run_checked(
                runner,
                (
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    str(wheel),
                ),
                cwd=smoke_cwd,
                env=child_env,
            )
            _run_checked(
                runner,
                ("uv", "pip", "check", "--python", str(python)),
                cwd=smoke_cwd,
                env=child_env,
            )
            smoke_command = [str(verifier), "--installed-smoke"]
            for name in sorted(forbidden_distributions):
                smoke_command.extend(("--forbid-distribution", name))
            _run_checked(
                runner,
                smoke_command,
                cwd=smoke_cwd,
                env=child_env,
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Run frozen-foundation verification.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero on success and one when an invariant fails.
    """
    parser = argparse.ArgumentParser(
        prog="bh-verify-foundation",
        description="Validate the locked, packaged wheel foundation.",
    )
    parser.add_argument("--python", action="append", dest="python_versions")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument(
        "--installed-smoke", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--forbid-distribution",
        action="append",
        default=[],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    python_versions = tuple(args.python_versions or ("3.10", "3.13"))
    try:
        if args.installed_smoke:
            verify_installed(frozenset(args.forbid_distribution))
        else:
            verify_repository(
                Path.cwd(), python_versions, keep_temp=args.keep_temp
            )
    except FoundationError as exc:
        print(f"bh-verify-foundation: {exc}", file=sys.stderr)
        return 1
    print("bh-verify-foundation: all invariants passed")
    return 0
