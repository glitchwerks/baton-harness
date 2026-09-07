"""Tests for the frozen wheel foundation verifier."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import sysconfig
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

import codereeve.verify_foundation as verify_foundation
from codereeve.resources import RESOURCE_NAMES
from codereeve.verify_foundation import (
    EXPECTED_ENTRY_POINTS,
    FoundationError,
    _dev_only_distributions,
    _read_installed_resources,
    _smoke_entry_points,
    _validate_installed_state,
    inspect_wheel,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_IDENTITY = {
    "schema_version": 1,
    "package_version": "0.0.0+foundation",
    "source_revision": "a" * 40,
    "lock_identity": "sha256:"
    + hashlib.sha256((_REPO_ROOT / "uv.lock").read_bytes()).hexdigest(),
    "development": False,
}


def _write_sdist(root: Path, extra: str | None = None) -> Path:
    """Create an actual source archive for extraction and identity checks."""
    path = root / "source.tar.gz"
    contents = {
        "source/pyproject.toml": b"[project]\n",
        "source/hatch_build.py": b"# hook\n",
        "source/uv.lock": (_REPO_ROOT / "uv.lock").read_bytes(),
        "source/src/codereeve/build_provenance.json": json.dumps(
            _IDENTITY
        ).encode(),
    }
    if extra:
        contents[extra] = b"malicious"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_archive_provenance_identity(tmp_path: Path, kind: str) -> None:
    """Both distribution formats must preserve the asserted identity."""
    path = (
        _write_wheel(tmp_path) if kind == "wheel" else _write_sdist(tmp_path)
    )
    verify_foundation.inspect_provenance_archive(path, _IDENTITY)


@pytest.mark.parametrize(
    "field", ["source_revision", "lock_identity", "package_version"]
)
def test_archive_rejects_identity_mismatch(tmp_path: Path, field: str) -> None:
    """A valid but different record cannot satisfy the expected build."""
    expected = dict(_IDENTITY, **{field: "wrong"})
    with pytest.raises(FoundationError):
        verify_foundation.inspect_provenance_archive(
            _write_wheel(tmp_path), expected
        )


@pytest.mark.parametrize("record", [None, b"{", b"[]", b"{}"])
def test_archive_rejects_invalid_record(
    tmp_path: Path, record: bytes | None
) -> None:
    """Missing or malformed records fail closed."""
    path = tmp_path / "bad.whl"
    with zipfile.ZipFile(path, "w") as archive:
        if record is not None:
            archive.writestr("codereeve/build_provenance.json", record)
        archive.writestr(
            "codereeve.dist-info/METADATA", "Version: 0.0.0+foundation\n"
        )
    with pytest.raises(FoundationError):
        verify_foundation.inspect_provenance_archive(path, _IDENTITY)


@pytest.mark.parametrize(
    "extra",
    [
        "source/.git/config",
        "../escape",
        "/absolute",
        "source/../../escape",
        "C:/escape",
        "source/C:/escape",
        "source/C:escape",
        "source/file:stream",
        "source/UV.LOCK",
        "source/uv.lock.",
        "source/uv.lock ",
        "source/.GiT/config",
        "source/.git./config",
        "source/CON.txt",
        "source/./alias",
        "source//alias",
    ],
)
def test_sdist_rejects_unsafe_tree(tmp_path: Path, extra: str) -> None:
    """Git state and escaping paths cannot enter the rebuild tree."""
    with pytest.raises(FoundationError):
        verify_foundation.inspect_provenance_archive(
            _write_sdist(tmp_path, extra), _IDENTITY
        )


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE]
)
def test_source_rejects_links_and_special_files(
    tmp_path: Path, kind: bytes
) -> None:
    """Links and special files are rejected before source extraction."""
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("source/link")
        member.type = kind
        member.linkname = "../../escape"
        archive.addfile(member)
    with pytest.raises(FoundationError, match="unsafe source archive"):
        verify_foundation.inspect_provenance_archive(path, _IDENTITY)


def test_wheel_metadata_version_must_match_record(tmp_path: Path) -> None:
    """A wheel's record cannot disagree with its Core Metadata version."""
    path = tmp_path / "mismatch.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "codereeve/build_provenance.json", json.dumps(_IDENTITY)
        )
        archive.writestr(
            "codereeve.dist-info/METADATA", "Version: 9.9.9\n"
        )
    with pytest.raises(FoundationError, match="installed version"):
        verify_foundation.inspect_provenance_archive(path, _IDENTITY)


def test_source_lock_bytes_must_match_record(tmp_path: Path) -> None:
    """A record with the asserted digest cannot hide changed lock bytes."""
    path = tmp_path / "mismatch.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in {
            "source/src/codereeve/build_provenance.json": json.dumps(
                _IDENTITY
            ).encode(),
            "source/uv.lock": b"different lock",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    with pytest.raises(FoundationError, match="lock digest mismatch"):
        verify_foundation.inspect_provenance_archive(path, _IDENTITY)


class _RecordingRunner:
    """Record external commands while emulating uv filesystem outputs."""

    def __init__(self, fail_command: tuple[str, ...] | None = None) -> None:
        """Initialize the runner.

        Args:
            fail_command: Optional command prefix that should return failure.
        """
        self.fail_command = fail_command
        self.calls: list[
            tuple[
                tuple[str, ...],
                Path,
                Mapping[str, str] | None,
                str | None,
                float,
            ]
        ] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = 300,
    ) -> CompletedProcess[str]:
        """Record a command and create the outputs expected from uv."""
        normalized = tuple(str(part) for part in command)
        self.calls.append((normalized, cwd, env, input_text, timeout_seconds))
        should_fail = (
            self.fail_command is not None
            and normalized[: len(self.fail_command)] == self.fail_command
        )
        if should_fail:
            return CompletedProcess(normalized, 1, "", "lock stale")
        if normalized == ("git", "rev-parse", "HEAD"):
            return CompletedProcess(normalized, 0, "a" * 40, "")
        if normalized[:2] == ("uv", "build"):
            if "--sdist" not in normalized:
                assert cwd.is_dir()
                assert (cwd / "pyproject.toml").is_file()
                assert not (cwd / ".git").exists()
            output = Path(normalized[normalized.index("--out-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            _write_wheel(output)
            if "--sdist" in normalized:
                _write_sdist(output)
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
            tuple[
                tuple[str, ...],
                Path,
                Mapping[str, str] | None,
                str | None,
                float,
            ]
        ] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = 300,
    ) -> CompletedProcess[str]:
        """Return each original command's side-effect-free smoke status."""
        normalized = tuple(str(part) for part in command)
        self.calls.append((normalized, cwd, env, input_text, timeout_seconds))
        command_name = Path(normalized[0]).stem
        lifecycle = {"bh-after-create", "bh-before-run", "bh-after-run"}
        return CompletedProcess(
            normalized,
            1 if command_name in lifecycle else 0,
            json.dumps(_IDENTITY)
            if "--provenance" in normalized
            else json.dumps(
                {
                    "schema_version": 1,
                    "provenance": {
                        key: value
                        for key, value in _IDENTITY.items()
                        if key != "schema_version"
                    },
                    "selected_phases": ["installation"],
                    "checks": [
                        {
                            "id": "PKG_PROVENANCE",
                            "phase": "installation",
                            "status": "pass",
                            "severity": "critical",
                            "title": "Provenance",
                            "detail": "Valid",
                            "remediation": "",
                        }
                    ],
                    "summary": {
                        "pass": 1,
                        "fail": 0,
                        "warn": 0,
                        "skip": 0,
                        "critical_failures": 0,
                    },
                }
            )
            if "--doctor" in normalized
            else "bh-daemon 0.0.0+foundation"
            if "--version" in normalized
            else "",
            "",
        )


def _write_wheel(
    root: Path,
    *,
    resources: tuple[str, ...] = RESOURCE_NAMES,
    entry_points: frozenset[str] | None = None,
    duplicate_resource: str | None = None,
    shadow_resource: str | None = None,
    entry_points_text: bytes | None = None,
) -> Path:
    """Write a minimal wheel-like ZIP for archive validation.

    Args:
        root: Directory receiving the archive.
        resources: Package resource basenames to include.
        entry_points: Console command names to declare.
        duplicate_resource: Optional resource to add twice at its exact path.
        shadow_resource: Optional resource to add only below a shadow path.
        entry_points_text: Optional raw entry-point metadata replacement.

    Returns:
        Path to the generated archive.
    """
    wheel = root / "codereeve-0.1.0-py3-none-any.whl"
    scripts = EXPECTED_ENTRY_POINTS if entry_points is None else entry_points
    declarations = "\n".join(
        f"{name} = codereeve.fake:main" for name in sorted(scripts)
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "codereeve/build_provenance.json", json.dumps(_IDENTITY)
        )
        archive.writestr(
            "codereeve-0.1.0.dist-info/METADATA",
            "Version: 0.0.0+foundation\n",
        )
        for name in resources:
            archive.writestr(f"codereeve/resources/{name}", b"test")
        if duplicate_resource is not None:
            archive.writestr(
                f"codereeve/resources/{duplicate_resource}",
                b"duplicate",
            )
        if shadow_resource is not None:
            archive.writestr(
                f"shadow/codereeve/resources/{shadow_resource}",
                b"shadow",
            )
        archive.writestr(
            "codereeve-0.1.0.dist-info/entry_points.txt",
            entry_points_text
            if entry_points_text is not None
            else f"[console_scripts]\n{declarations}\n",
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
    with pytest.warns(UserWarning, match="Duplicate name"):
        wheel = _write_wheel(tmp_path, duplicate_resource="WORKFLOW.md")

    with pytest.raises(FoundationError, match="wheel resource duplicated"):
        inspect_wheel(wheel)


def test_shadow_resource_path_cannot_satisfy_wheel_contract(
    tmp_path: Path,
) -> None:
    """Only the canonical archive member satisfies a packaged default."""
    wheel = _write_wheel(
        tmp_path,
        resources=RESOURCE_NAMES[1:],
        shadow_resource="WORKFLOW.md",
    )

    with pytest.raises(FoundationError, match="wheel resource missing"):
        inspect_wheel(wheel)


def test_invalid_wheel_archive_is_normalized(tmp_path: Path) -> None:
    """A corrupt wheel reports a stable foundation error."""
    wheel = tmp_path / "codereeve-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"not a zip archive")

    with pytest.raises(FoundationError, match="could not inspect wheel"):
        inspect_wheel(wheel)


def test_malformed_entry_point_metadata_is_normalized(tmp_path: Path) -> None:
    """Invalid wheel metadata reports a stable foundation error."""
    wheel = _write_wheel(
        tmp_path,
        entry_points_text=b"[console_scripts\nbroken",
    )

    with pytest.raises(FoundationError, match="could not inspect wheel"):
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
    build_calls = [
        call for call in runner.calls if call[0][:2] == ("uv", "build")
    ]
    assert len(build_calls) == 2
    for call in build_calls:
        assert call[2] is not None
        assert call[2]["BH_BUILD_VERSION"] == "0.0.0+foundation"
        assert call[2]["BH_BUILD_SOURCE_REVISION"] == "a" * 40
        assert "BH_BUILD_DEVELOPMENT" not in call[2]
    assert build_calls[1][1] != _REPO_ROOT
    commands = [
        command for command in commands if command[:2] != ("git", "rev-parse")
    ]
    commands.pop(4)  # The separately asserted source-archive wheel rebuild.
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
    assert runner.calls[-1][1].is_relative_to(tmp_path)
    assert runner.calls[-1][1] != _REPO_ROOT
    assert runner.calls[-1][2] is not None
    assert "PYTHONPATH" not in runner.calls[-1][2]


def test_repository_verification_stops_on_stale_lock() -> None:
    """A stale lock fails before mirror, build, or installation work."""
    runner = _RecordingRunner(("uv", "lock", "--check"))

    with pytest.raises(FoundationError, match="uv lock --check.*lock stale"):
        verify_foundation.verify_repository(
            _REPO_ROOT, ("3.10",), runner=runner
        )

    assert len(runner.calls) == 1


def test_source_extraction_checks_resolved_containment_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination resolving outside the extraction tree is never written."""
    original_resolve = Path.resolve
    escaped = tmp_path / "escaped.toml"

    def resolve(path: Path, strict: bool = False) -> Path:
        if path.parts[-3:] == ("source", "source", "pyproject.toml"):
            return escaped
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(FoundationError, match="outside extraction"):
        verify_foundation.verify_repository(
            _REPO_ROOT, ("3.13",), runner=_RecordingRunner()
        )
    assert not escaped.exists()


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("uv"), TimeoutExpired(("uv", "lock"), 300)],
)
def test_repository_command_failures_are_normalized(
    failure: BaseException,
) -> None:
    """Missing or stalled tools report stable foundation errors."""

    def failing_runner(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = 300,
    ) -> CompletedProcess[str]:
        del command, cwd, env, input_text, timeout_seconds
        raise failure

    with pytest.raises(FoundationError, match="command could not complete"):
        verify_foundation.verify_repository(
            _REPO_ROOT, ("3.10",), runner=failing_runner
        )


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
    package_file = tmp_path / "venv" / "site-packages" / "codereeve"
    package_file.mkdir(parents=True)
    _validate_installed_state(
        direct_url_json='{"archive_info": {}}',
        package_file=package_file / "__init__.py",
        prefix=tmp_path / "venv",
        entry_points=EXPECTED_ENTRY_POINTS,
        installed_distributions=frozenset({"codereeve", "jinja2"}),
        forbidden_distributions=frozenset({"pytest", "ruff"}),
    )


def test_editable_installed_state_fails_closed(tmp_path: Path) -> None:
    """An editable wheel replacement cannot pass production validation."""
    package_file = tmp_path / "venv" / "site-packages" / "codereeve.py"

    with pytest.raises(FoundationError, match="editable installation"):
        _validate_installed_state(
            direct_url_json='{"dir_info": {"editable": true}}',
            package_file=package_file,
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS,
            installed_distributions=frozenset({"codereeve"}),
            forbidden_distributions=frozenset(),
        )


@pytest.mark.parametrize(
    "direct_url_json",
    ("[]", '{"dir_info": []}'),
)
def test_invalid_direct_url_shape_fails_closed(
    tmp_path: Path,
    direct_url_json: str,
) -> None:
    """Structurally corrupt direct-URL metadata cannot raise AttributeError."""
    package_file = tmp_path / "venv" / "site-packages" / "codereeve.py"

    with pytest.raises(
        FoundationError, match="invalid installed direct_url.json"
    ):
        _validate_installed_state(
            direct_url_json=direct_url_json,
            package_file=package_file,
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS,
            installed_distributions=frozenset({"codereeve"}),
            forbidden_distributions=frozenset(),
        )


def test_package_imported_outside_environment_fails_closed(
    tmp_path: Path,
) -> None:
    """A source-checkout import cannot masquerade as the installed wheel."""
    with pytest.raises(FoundationError, match="outside environment"):
        _validate_installed_state(
            direct_url_json=None,
            package_file=tmp_path / "checkout" / "codereeve.py",
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS,
            installed_distributions=frozenset({"codereeve"}),
            forbidden_distributions=frozenset(),
        )


def test_missing_installed_entry_point_fails_closed(tmp_path: Path) -> None:
    """A missing generated wrapper cannot pass installed validation."""
    with pytest.raises(
        FoundationError, match="installed entry points missing"
    ):
        _validate_installed_state(
            direct_url_json=None,
            package_file=tmp_path / "venv" / "codereeve.py",
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS - {"bh-daemon"},
            installed_distributions=frozenset({"codereeve"}),
            forbidden_distributions=frozenset(),
        )


def test_dev_only_distribution_fails_closed(tmp_path: Path) -> None:
    """Installing a selected dev extra breaks the runtime-only invariant."""
    with pytest.raises(FoundationError, match="dev-only distributions"):
        _validate_installed_state(
            direct_url_json=None,
            package_file=tmp_path / "venv" / "codereeve.py",
            prefix=tmp_path / "venv",
            entry_points=EXPECTED_ENTRY_POINTS,
            installed_distributions=frozenset({"codereeve", "pytest"}),
            forbidden_distributions=frozenset({"pytest", "ruff"}),
        )


def test_missing_installed_distribution_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent wheel metadata reports a stable foundation error."""

    def missing_distribution(name: str) -> metadata.Distribution:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", missing_distribution)

    with pytest.raises(FoundationError, match="installed metadata"):
        verify_foundation.verify_installed(frozenset())


def test_installed_smoke_uses_environment_scripts_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external resolved interpreter cannot redirect wrapper smokes."""
    prefix = tmp_path / "environment"
    scripts = prefix / "bin"
    resolved_interpreter = tmp_path / "uv-python" / "bin" / "python"
    selected_directories: list[Path] = []

    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(resolved_interpreter))
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(scripts))
    monkeypatch.setattr(
        verify_foundation,
        "_validate_installed_state",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        verify_foundation,
        "_read_installed_resources",
        lambda: None,
    )
    monkeypatch.setattr(
        verify_foundation,
        "_smoke_entry_points",
        selected_directories.append,
    )

    verify_foundation.verify_installed(frozenset())

    assert selected_directories == [scripts]
    assert selected_directories[0].is_relative_to(Path(sys.prefix))


def test_installed_resource_read_failure_is_normalized() -> None:
    """Unreadable packaged defaults report a stable foundation error."""

    def unreadable_resource(name: str) -> bytes:
        raise OSError(f"cannot read {name}")

    with pytest.raises(FoundationError, match="installed resource"):
        _read_installed_resources(unreadable_resource)


@pytest.mark.parametrize("outer", [False, True])
def test_installation_smokes_have_no_ambient_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outer: bool
) -> None:
    """Both installed process hops get fresh homes and no caller secrets."""
    secret_keys = (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "BWS_ACCESS_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "BH_PROJECT_ROOT",
        "BH_REPO_OWNER",
        "BH_GITHUB_APP_PRIVATE_KEY_FILE",
        "BWS_PEM_SECRET_ID",
        "PYTHONPATH",
        "PYTHONHOME",
        "UNRECOGNIZED_AUTH_TOKEN",
    )
    home_keys = (
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "APPDATA",
        "LOCALAPPDATA",
        "GH_CONFIG_DIR",
        "CLAUDE_CONFIG_DIR",
    )
    for key in secret_keys:
        monkeypatch.setenv(key, "dummy-ambient-authority")
    for key in home_keys:
        monkeypatch.setenv(key, str(tmp_path / "real-home"))
    monkeypatch.setenv("PATH", "execution-path")
    monkeypatch.setenv("SYSTEMROOT", "execution-system-root")
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    normal = _RecordingRunner() if outer else _SmokeRunner()
    smoke_calls: list[tuple[str, ...]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = 300,
    ) -> CompletedProcess[str]:
        """Inspect the real launch environment while temporary homes exist."""
        if "--installed-smoke" in command or "--doctor" in command:
            smoke_calls.append(tuple(command))
            assert env is not None
            assert not set(secret_keys) & set(env)
            assert env["PATH"] == "execution-path"
            assert env["SYSTEMROOT"] == "execution-system-root"
            for key in home_keys:
                home = Path(env[key])
                assert home.is_relative_to(tmp_path)
                assert home != tmp_path / "real-home"
                assert home.is_dir()
                assert not list(home.iterdir())
        return normal(
            command,
            cwd=cwd,
            env=env,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )

    if outer:
        verify_foundation.verify_repository(
            _REPO_ROOT, ("3.10",), runner=runner
        )
    else:
        _smoke_entry_points(tmp_path / "bin", runner=runner)
    assert len(smoke_calls) == 1
    assert os.environ["GH_TOKEN"] == "dummy-ambient-authority"


def test_installed_entry_points_use_only_safe_smokes(tmp_path: Path) -> None:
    """Changing a wrapper smoke into an external workflow is rejected."""
    runner = _SmokeRunner()

    _smoke_entry_points(tmp_path / "bin", runner=runner)

    commands = {Path(call[0][0]).stem: call for call in runner.calls}
    assert set(commands) == EXPECTED_ENTRY_POINTS - {"bh-verify-foundation"}
    daemon_args = [
        call[0][1:]
        for call in runner.calls
        if Path(call[0][0]).stem == "bh-daemon"
    ]
    assert ("--help",) in daemon_args
    assert ("--version",) in daemon_args
    assert ("--provenance",) in daemon_args
    assert (
        "--doctor",
        "--phase",
        "installation",
        "--format",
        "json",
        "--strict",
    ) in daemon_args
    assert commands["bh-force-pr-not-merge"][3] == "{}"
    for name in ("bh-after-create", "bh-before-run", "bh-after-run"):
        assert commands[name][0][1:] == ()
        assert commands[name][1] != _REPO_ROOT
    assert all(call[4] == 30 for call in runner.calls)


@pytest.mark.parametrize("flag", ["--provenance", "--doctor"])
@pytest.mark.parametrize(
    "output", ["invalid", "[]", "{}", "wrong-phase", "failed", "empty-checks"]
)
def test_installed_json_smoke_rejects_bad_output(
    tmp_path: Path, flag: str, output: str
) -> None:
    """Successful exit alone does not establish a valid installed report."""
    normal = _SmokeRunner()

    def runner(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = 300,
    ) -> CompletedProcess[str]:
        result = normal(
            command,
            cwd=cwd,
            env=env,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )
        if flag in command:
            if output in {"wrong-phase", "failed", "empty-checks"}:
                if flag != "--doctor":
                    result.stdout = "{}"
                else:
                    report = json.loads(result.stdout)
                    if output == "wrong-phase":
                        report["selected_phases"] = ["live"]
                    elif output == "failed":
                        report["summary"]["critical_failures"] = 1
                    else:
                        report["checks"] = []
                    result.stdout = json.dumps(report)
            else:
                result.stdout = output
        return result

    with pytest.raises(FoundationError):
        _smoke_entry_points(tmp_path, runner=runner)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("checks", [{}]),
        ("checks", [None]),
        ("summary", {"critical_failures": 0}),
        *[
            (f"check.{name}", None)
            for name in (
                "id",
                "phase",
                "status",
                "severity",
                "title",
                "detail",
                "remediation",
            )
        ],
        ("check.id", ""),
        ("check.phase", "live"),
        ("check.status", "unknown"),
        ("check.severity", "unknown"),
        ("check.extra", "unexpected"),
        *[
            (f"check.{name}", "<missing>")
            for name in (
                "id",
                "phase",
                "status",
                "severity",
                "title",
                "detail",
                "remediation",
            )
        ],
        *[
            (f"summary.{name}", value)
            for name in ("pass", "fail", "warn", "skip", "critical_failures")
            for value in (None, True, -1, 0.0, "0", 2)
        ],
        ("summary.extra", 0),
    ],
)
def test_installed_doctor_rejects_malformed_schema(
    tmp_path: Path, field: str, value: object
) -> None:
    """Malformed checks and dishonest summary counters cannot pass smoke."""
    normal = _SmokeRunner()

    def runner(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = 300,
    ) -> CompletedProcess[str]:
        """Replace one report field at the external process boundary."""
        result = normal(
            command,
            cwd=cwd,
            env=env,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )
        if "--doctor" in command:
            report = json.loads(result.stdout)
            if field.startswith("check."):
                name = field.removeprefix("check.")
                if value == "<missing>":
                    del report["checks"][0][name]
                else:
                    report["checks"][0][name] = value
            elif field.startswith("summary."):
                report["summary"][field.removeprefix("summary.")] = value
            else:
                report[field] = value
            result.stdout = json.dumps(report)
        return result

    with pytest.raises(FoundationError, match="invalid installed doctor"):
        _smoke_entry_points(tmp_path, runner=runner)


def test_installed_entry_point_timeout_is_normalized(tmp_path: Path) -> None:
    """A stalled generated wrapper reports a stable foundation error."""

    def stalled_runner(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout_seconds: float = 300,
    ) -> CompletedProcess[str]:
        del cwd, env, input_text
        raise TimeoutExpired(command, timeout_seconds)

    with pytest.raises(
        FoundationError, match="entry-point smoke could not complete"
    ):
        _smoke_entry_points(tmp_path / "bin", runner=stalled_runner)


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
