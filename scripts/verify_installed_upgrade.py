"""Verify explicit release wheels in isolated installed environments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

LEGACY_SMOKES = (
    ("bh-daemon", ("--help",), 0, None),
    ("bh-daemon", ("--version",), 0, None),
    ("bh-daemon", ("--provenance",), 0, None),
    ("bh-after-create", (), 1, None),
    ("bh-before-run", (), 1, None),
    ("bh-after-run", (), 1, None),
    ("bh-force-pr-not-merge", (), 0, "{}"),
    ("bh-verify-foundation", ("--help",), 0, None),
)


def _executable(environment: Path, name: str) -> Path:
    """Return a virtual environment executable path.

    Args:
        environment: Virtual environment root.
        name: Executable basename.

    Returns:
        Platform-specific executable path.
    """
    if os.name == "nt":
        return environment / "Scripts" / f"{name}.exe"
    return environment / "bin" / name


def _environment(home: Path) -> dict[str, str]:
    """Build a minimal child environment with fresh configuration homes.

    Args:
        home: Disposable home directory.

    Returns:
        A complete environment without credential or project authority.
    """
    home.mkdir(parents=True, exist_ok=True)
    result = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_CACHE_DIR": str(home / "uv-cache"),
        "UV_NO_PROGRESS": "1",
    }
    for key in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR"):
        if value := os.environ.get(key):
            result[key] = value
    for key in (
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "GH_CONFIG_DIR",
        "CLAUDE_CONFIG_DIR",
    ):
        result[key] = str(home)
    return result


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    expected: int,
    name: str,
    outcomes: list[dict[str, object]],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and append its secret-free outcome.

    Args:
        command: Command arguments.
        cwd: Working directory outside source checkouts.
        env: Complete scrubbed child environment.
        expected: Required return code.
        name: Stable evidence label without paths or arguments.
        outcomes: Evidence records to append.
        input_text: Optional standard input.

    Returns:
        Captured subprocess result.

    Raises:
        RuntimeError: If the command returns an unexpected status.
    """
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=600,
    )
    outcomes.append(
        {
            "name": name,
            "returncode": result.returncode,
            "expected_returncode": expected,
        }
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"{name} returned {result.returncode}, expected {expected}"
        )
    return result


def _wheel_identity(path: Path) -> dict[str, object]:
    """Read immutable identity from a wheel without importing it.

    Args:
        path: Explicit wheel path.

    Returns:
        Artifact digest and embedded provenance fields.
    """
    with ZipFile(path) as archive:
        records = [
            name
            for name in archive.namelist()
            if name.endswith("/build_provenance.json") and name.count("/") == 1
        ]
        if len(records) != 1:
            raise ValueError(
                "wheel must contain one package provenance record"
            )
        provenance = json.loads(archive.read(records[0]).decode("utf-8"))
        metadata_names = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        ]
        metadata = Parser().parsestr(
            archive.read(metadata_names[0]).decode("utf-8")
        )
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "version": metadata["Version"],
        "source_revision": provenance["source_revision"],
        "lock_identity": provenance["lock_identity"],
        "development": provenance["development"],
    }


def _validate_bindings(
    args: argparse.Namespace,
) -> tuple[dict[str, object], dict[str, object], dict[str, str]]:
    """Bind supplied artifacts and exports to trusted expected identities.

    Args:
        args: Parsed artifact paths and independently supplied expectations.

    Returns:
        Old identity, candidate identity, and sanitized binding evidence.

    Raises:
        RuntimeError: If an artifact or runtime export is stale or unrelated.
    """
    old = _wheel_identity(args.old_wheel)
    candidate = _wheel_identity(args.candidate_wheel)
    checks = (
        ("old artifact version", old["version"], "0.1.0+upgradefixture"),
        ("candidate artifact version", candidate["version"], "0.2.0"),
        (
            "old artifact source revision",
            old["source_revision"],
            args.expected_old_revision,
        ),
        (
            "candidate artifact source revision",
            candidate["source_revision"],
            args.expected_candidate_revision,
        ),
        (
            "old artifact lock identity",
            old["lock_identity"],
            args.expected_old_lock_identity,
        ),
        (
            "candidate artifact lock identity",
            candidate["lock_identity"],
            args.expected_candidate_lock_identity,
        ),
        (
            "old runtime export digest",
            hashlib.sha256(args.old_requirements.read_bytes()).hexdigest(),
            args.expected_old_runtime_sha256,
        ),
        (
            "candidate runtime export digest",
            hashlib.sha256(
                args.candidate_requirements.read_bytes()
            ).hexdigest(),
            args.expected_candidate_runtime_sha256,
        ),
    )
    for label, actual, wanted in checks:
        if actual != wanted:
            raise RuntimeError(f"{label} does not match expected identity")
    if old["development"] is not False:
        raise RuntimeError("old artifact is not a standard build")
    if candidate["development"] is not False:
        raise RuntimeError("candidate artifact is not a standard build")
    bindings = {
        "old_source_revision": args.expected_old_revision,
        "candidate_source_revision": args.expected_candidate_revision,
        "old_lock_identity": args.expected_old_lock_identity,
        "candidate_lock_identity": args.expected_candidate_lock_identity,
        "old_runtime_sha256": args.expected_old_runtime_sha256,
        "candidate_runtime_sha256": args.expected_candidate_runtime_sha256,
    }
    return old, candidate, bindings


def _snapshot(environment: Path, distribution: str) -> dict[str, str]:
    """Hash installed distribution files and generated console scripts.

    Args:
        environment: Installed environment root.
        distribution: Distribution metadata name.

    Returns:
        Relative path to SHA-256 mapping.
    """
    python = _executable(environment, "python")
    script = (
        "import hashlib,json,importlib.metadata as m,pathlib,sys,sysconfig;"
        f"d=m.distribution({distribution!r});"
        "root=pathlib.Path(d.locate_file(''));"
        "paths=[root.joinpath(p) for p in (d.files or ())];"
        "scripts=pathlib.Path(sysconfig.get_path('scripts'));"
        "paths += [p for p in scripts.iterdir() "
        "if p.name.startswith(('bh-', 'codereeve'))];"
        "print(json.dumps({str(p.relative_to(pathlib.Path(sys.prefix))):"
        "hashlib.sha256(p.read_bytes()).hexdigest() for p in paths "
        "if p.is_file()},sort_keys=True))"
    )
    result = subprocess.run(
        [str(python), "-I", "-c", script],
        cwd=environment.parent,
        env=_environment(environment.parent / "snapshot-home"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("installed snapshot failed")
    return dict(json.loads(result.stdout))


def _install(
    environment: Path,
    python_version: str,
    requirements: Path,
    wheels: list[Path],
    outside: Path,
    env: dict[str, str],
    outcomes: list[dict[str, object]],
    label: str,
) -> None:
    """Create one runtime-only environment and install explicit wheels."""
    _run(
        ["uv", "venv", str(environment), "--python", python_version],
        cwd=outside,
        env=env,
        expected=0,
        name=f"{label}.create",
        outcomes=outcomes,
    )
    python = _executable(environment, "python")
    _run(
        ["uv", "pip", "sync", "--python", str(python), str(requirements)],
        cwd=outside,
        env=env,
        expected=0,
        name=f"{label}.runtime-sync",
        outcomes=outcomes,
    )
    for number, wheel in enumerate(wheels, 1):
        _run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(wheel),
            ],
            cwd=outside,
            env=env,
            expected=0,
            name=f"{label}.wheel-{number}",
            outcomes=outcomes,
        )


def _migration_rollback(
    python: Path,
    fixture_helper: Path,
    fixture_root: Path,
    outside: Path,
    env: dict[str, str],
    outcomes: list[dict[str, object]],
) -> dict[str, object]:
    """Apply and restore the durable fixture through installed production code.

    Args:
        python: Candidate environment interpreter.
        fixture_helper: Portable fixture helper module path.
        fixture_root: Already materialized fixture root.
        outside: Working directory outside source checkouts.
        env: Scrubbed child environment.
        outcomes: Evidence records to append.

    Returns:
        Stable rollback facts emitted by the installed subprocess.
    """
    program = """
import importlib.util
import json
import pathlib
import sys
from codereeve.migration.model import MigrationContext
from codereeve.migration.transaction import apply_migration, restore_migration
from codereeve.paths import PathLayout

helper_path = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("release_fixture", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
layout = PathLayout.for_environment(
    root / "project",
    {"XDG_CONFIG_HOME": str(root / "home" / ".config")},
    home=root / "home",
    etc_root=root / "etc",
)
operations = helper.PortableFixtureOperations()
result = apply_migration(MigrationContext(layout), operations=operations)
failure_injected = False
try:
    failure_injected = True
    raise RuntimeError("synthetic post-apply failure")
except RuntimeError:
    restoration = restore_migration(
        result.manifest_path,
        operations=operations,
    )
print(json.dumps({
    "failure_injected": failure_injected,
    "restoration_status": restoration.status.value,
}))
"""
    result = _run(
        [
            str(python),
            "-I",
            "-c",
            program,
            str(fixture_helper),
            str(fixture_root),
        ],
        cwd=outside,
        env=env,
        expected=0,
        name="candidate.migration-failure-rollback",
        outcomes=outcomes,
    )
    return dict(json.loads(result.stdout))


def verify(args: argparse.Namespace) -> dict[str, object]:
    """Execute the installed upgrade proof and return sanitized evidence."""
    old_identity, candidate_identity, bindings = _validate_bindings(args)
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    outside = workspace / "outside-checkout"
    outside.mkdir()
    env = _environment(workspace / "home")
    outcomes: list[dict[str, object]] = []
    old_env = workspace / "old-environment"
    candidate_env = workspace / "candidate-environment"
    overlap_env = workspace / "overlap-environment"
    _install(
        old_env,
        args.python,
        args.old_requirements,
        [args.old_wheel],
        outside,
        env,
        outcomes,
        "legacy",
    )
    before = _snapshot(old_env, "baton-harness")
    old_python = _executable(old_env, "python")
    old_cli = _executable(old_env, "bh-daemon")
    _run(
        [str(old_cli), "--version"],
        cwd=outside,
        env=env,
        expected=0,
        name="legacy.before-rollback.version",
        outcomes=outcomes,
    )
    _install(
        candidate_env,
        args.python,
        args.candidate_requirements,
        [args.candidate_wheel],
        outside,
        env,
        outcomes,
        "candidate",
    )
    candidate_cli = _executable(candidate_env, "codereeve")
    for name, command in (
        ("help", [str(candidate_cli), "--help"]),
        ("version", [str(candidate_cli), "--version"]),
        ("provenance", [str(candidate_cli), "provenance"]),
        ("strict-installed", [str(candidate_cli), "verify", "--installed"]),
    ):
        _run(
            command,
            cwd=outside,
            env=env,
            expected=0,
            name=f"candidate.{name}",
            outcomes=outcomes,
        )
    rollback = _migration_rollback(
        _executable(candidate_env, "python"),
        args.fixture.parent.parent / "release_gate" / "fixture.py",
        args.fixture_root,
        outside,
        env,
        outcomes,
    )
    if not rollback.get("failure_injected"):
        raise RuntimeError(
            "migration rollback did not inject the expected failure"
        )
    if rollback.get("restoration_status") != "complete":
        raise RuntimeError(
            "migration rollback did not restore the durable fixture"
        )
    for command, command_args, expected, input_text in LEGACY_SMOKES:
        result = _run(
            [str(_executable(candidate_env, command)), *command_args],
            cwd=outside,
            env=env,
            expected=expected,
            name=f"candidate.compatibility.{command}",
            outcomes=outcomes,
            input_text=input_text,
        )
        if "removed in 0.4.0" not in result.stderr:
            raise RuntimeError(f"{command} omitted its 0.4.0 removal notice")
    _install(
        overlap_env,
        args.python,
        args.candidate_requirements,
        [args.old_wheel, args.candidate_wheel],
        outside,
        env,
        outcomes,
        "overlap",
    )
    overlap_result = _run(
        [str(_executable(overlap_env, "codereeve")), "verify", "--installed"],
        cwd=outside,
        env=env,
        expected=1,
        name="overlap.ownership-rejection",
        outcomes=outcomes,
    )
    diagnostic = "incompatible distributions installed"
    if diagnostic not in overlap_result.stderr:
        raise RuntimeError(
            "overlap gate did not report incompatible distributions"
        )
    _run(
        [str(old_cli), "--version"],
        cwd=outside,
        env=env,
        expected=0,
        name="legacy.after-rollback.version",
        outcomes=outcomes,
    )
    import_result = _run(
        [
            str(old_python),
            "-I",
            "-c",
            "import baton_harness,pathlib,sys; "
            "print(pathlib.Path(baton_harness.__file__).is_relative_to("
            "pathlib.Path(sys.prefix)))",
        ],
        cwd=outside,
        env=env,
        expected=0,
        name="legacy.after-rollback.import",
        outcomes=outcomes,
    )
    after = _snapshot(old_env, "baton-harness")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    interpreter = _run(
        [str(_executable(candidate_env, "python")), "--version"],
        cwd=outside,
        env=env,
        expected=0,
        name="candidate.interpreter-version",
        outcomes=outcomes,
    )
    return {
        "schema_version": 1,
        "python_requested": args.python,
        "interpreter_version": (
            interpreter.stdout or interpreter.stderr
        ).strip(),
        "fixture": {
            "schema_version": fixture["schema_version"],
            "sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        },
        "legacy_artifact": old_identity,
        "candidate_artifact": candidate_identity,
        "bindings": bindings,
        "legacy_environment": {
            "unchanged_after_rollback": before == after,
            "import_owned_by_environment": import_result.stdout.strip()
            == "True",
            "snapshot_file_count": len(before),
        },
        "overlap_negative": {
            "rejected": overlap_result.returncode == 1,
            "diagnostic": diagnostic,
        },
        "migration_rollback": rollback,
        "command_outcomes": outcomes,
    }


def _parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser()
    for option in (
        "old-wheel",
        "candidate-wheel",
        "old-requirements",
        "candidate-requirements",
        "fixture",
        "fixture-root",
        "workspace",
        "evidence",
    ):
        parser.add_argument(f"--{option}", type=Path, required=True)
    for option in (
        "expected-old-revision",
        "expected-candidate-revision",
        "expected-old-lock-identity",
        "expected-candidate-lock-identity",
        "expected-old-runtime-sha256",
        "expected-candidate-runtime-sha256",
    ):
        parser.add_argument(f"--{option}", required=True)
    parser.add_argument("--python", required=True)
    return parser


def main() -> int:
    """Run the installed proof and write sanitized JSON evidence."""
    args = _parser().parse_args()
    try:
        evidence = verify(args)
        args.evidence.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        print("installed release gate failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
