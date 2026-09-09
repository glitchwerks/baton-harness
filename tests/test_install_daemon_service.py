"""Black-box tests for the narrow service-installer Bash launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/install-daemon-service.sh"
GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")


def _bash_executable(
    platform: str = sys.platform,
    *,
    search: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Resolve Bash before tests narrow PATH for injected executables."""
    if platform == "win32":
        return GIT_BASH
    executable = search("bash")
    if executable is None:
        pytest.skip("Bash is unavailable on this test host")
    return Path(executable)


BASH = _bash_executable()


def test_bash_resolution_is_platform_correct() -> None:
    """Windows selects Git Bash while POSIX preserves native discovery."""
    assert (
        _bash_executable(
            "win32", search=lambda _name: pytest.fail("must not search")
        )
        == GIT_BASH
    )
    assert _bash_executable(
        "linux", search=lambda name: f"/usr/bin/{name}"
    ) == Path("/usr/bin/bash")


def _candidate(tmp_path: Path, *, status: int = 0) -> tuple[Path, Path]:
    """Create an executable candidate Python shim that records exact argv."""
    environment = tmp_path / "candidate"
    binary = environment / "bin/python"
    binary.parent.mkdir(parents=True)
    log = tmp_path / "candidate-argv.txt"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" > "${CANDIDATE_ARGV_LOG}"\n'
        f"exit {status}\n",
        encoding="utf-8",
        newline="\n",
    )
    binary.chmod(0o755)
    return environment, log


def _run(
    tmp_path: Path,
    arguments: list[str],
    *,
    status: int = 0,
    trace: bool = False,
    extra_environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    """Run the real launcher with an isolated candidate and command PATH."""
    candidate, log = _candidate(tmp_path, status=status)
    systemctl_log = tmp_path / "systemctl.log"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    systemctl = stub_dir / "systemctl"
    systemctl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "${SYSTEMCTL_LOG}"\n',
        encoding="utf-8",
        newline="\n",
    )
    systemctl.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": os.pathsep.join(
            (str(stub_dir), str(BASH.parent), os.environ.get("PATH", ""))
        ),
        "CANDIDATE_ARGV_LOG": str(log),
        "SYSTEMCTL_LOG": str(systemctl_log),
    }
    if extra_environment:
        environment.update(extra_environment)
    command = [str(BASH)]
    if trace:
        command.append("-x")
    command.extend(
        (
            str(SCRIPT),
            "--environment",
            candidate.as_posix(),
            "--harness-dir",
            tmp_path.as_posix(),
            *arguments,
        )
    )
    process = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    recorded = (
        log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    )
    return process, recorded, systemctl_log


@pytest.mark.parametrize("mode", ["--print-unit", "--no-start"])
def test_launcher_delegates_without_service_effects(
    tmp_path: Path, mode: str
) -> None:
    """Render and install-only modes reach candidate Python, not systemctl."""
    process, arguments, systemctl_log = _run(tmp_path, [mode])

    assert process.returncode == 0, process.stderr
    assert arguments[:3] == ["-I", "-m", "codereeve.service_cutover.cli"]
    assert mode in arguments
    assert not systemctl_log.exists()


def test_missing_candidate_fails_before_any_service_effect(
    tmp_path: Path,
) -> None:
    """A missing separate environment fails without an old-venv fallback."""
    missing = tmp_path / "missing"
    process = subprocess.run(
        [
            str(BASH),
            str(SCRIPT),
            "--environment",
            missing.as_posix(),
            "--harness-dir",
            tmp_path.as_posix(),
        ],
        env={**os.environ, "PATH": str(BASH.parent)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert process.returncode != 0
    assert "candidate Python is unavailable" in process.stderr


def test_bootstrap_alias_conflict_fails_before_candidate_discovery(
    tmp_path: Path,
) -> None:
    """Different root aliases fail before checking an environment."""
    process = subprocess.run(
        [str(BASH), str(SCRIPT)],
        env={
            **os.environ,
            "PATH": str(BASH.parent),
            "CODEREEVE_ROOT": "/one",
            "BATON_HARNESS_DIR": "/two",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert process.returncode != 0
    assert "CODEREEVE_ROOT and BATON_HARNESS_DIR" in process.stderr
    assert "/one" not in process.stderr
    assert "/two" not in process.stderr


@pytest.mark.parametrize("arguments", [[], ["--recover", "/private/journal"]])
def test_candidate_nonzero_status_is_propagated(
    tmp_path: Path, arguments: list[str]
) -> None:
    """Install and recovery failures retain the candidate process status."""
    process, _recorded, _systemctl = _run(tmp_path, arguments, status=7)

    assert process.returncode == 7


def test_shell_trace_never_contains_secret_value(tmp_path: Path) -> None:
    """The launcher disables inherited xtrace before touching configuration."""
    sentinel = "trace-secret-sentinel"
    process, _recorded, _systemctl = _run(
        tmp_path,
        [],
        trace=True,
        extra_environment={"BWS_ACCESS_TOKEN": sentinel},
    )

    assert process.returncode == 0
    assert sentinel not in process.stdout + process.stderr
