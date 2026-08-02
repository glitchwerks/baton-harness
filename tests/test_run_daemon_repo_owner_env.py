"""Regression coverage for run-daemon.sh repository environment values."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
RUN_DAEMON = HARNESS / "bin" / "run-daemon.sh"

# On Windows, the system bash (C:\Windows\System32\bash.exe) launches WSL and
# fails when no WSL distro is configured.  Prefer Git Bash when available.
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""
_VENV_BIN_DIR = str(
    HARNESS / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
)


def _write_config_env(tmp_path: Path) -> Path:
    """Create an isolated project config without repository identity keys.

    Args:
        tmp_path: Pytest-provided temporary directory for the test.

    Returns:
        The isolated project root containing the config fixture.
    """
    project_root = tmp_path / "project"
    config_dir = project_root / ".bh"
    config_dir.mkdir(parents=True)
    (config_dir / "config.env").write_text(
        "BH_GITHUB_APP_ID=111\n", encoding="utf-8", newline="\n"
    )
    return project_root


def _write_gh_stub(tmp_path: Path) -> Path:
    """Create a silent gh executable for the label preflight.

    Args:
        tmp_path: Pytest-provided temporary directory for the test.

    Returns:
        The directory containing the executable stub.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    gh_stub.chmod(0o755)
    return bin_dir


def _run_daemon(
    tmp_path: Path,
    *,
    owner: str | None = None,
    name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real daemon launcher with an isolated environment.

    Args:
        tmp_path: Pytest-provided temporary directory for the test.
        owner: Optional operator-provided repository owner.
        name: Optional operator-provided repository name.

    Returns:
        The completed daemon launcher process, including captured output.
    """
    project_root = _write_config_env(tmp_path)
    bin_dir = _write_gh_stub(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"
    xdg_config_home.mkdir()

    env = dict(os.environ)
    env.pop("BH_REPO_OWNER", None)
    env.pop("BH_REPO_NAME", None)
    env.pop("BH_PROJECT_ROOT", None)
    env["PATH"] = os.pathsep.join(
        part
        for part in [
            bin_dir.as_posix(),
            _BASH_BIN_DIR,
            _VENV_BIN_DIR,
            env.get("PATH", ""),
        ]
        if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    env["BH_PROJECT_ROOT"] = project_root.as_posix()
    if owner is not None:
        env["BH_REPO_OWNER"] = owner
    if name is not None:
        env["BH_REPO_NAME"] = name

    return subprocess.run(
        [_BASH, str(RUN_DAEMON)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_operator_env_wins_over_missing_config_env_lines(
    tmp_path: Path,
) -> None:
    """Operator repository values survive absent config.env assignments."""
    proc = _run_daemon(
        tmp_path,
        owner="test-owner",
        name="test-repo",
    )

    assert (
        "checking required labels in test-owner/test-repo..." in proc.stdout
    ), proc.stdout + proc.stderr


def test_missing_owner_and_name_reports_error_not_silent_death(
    tmp_path: Path,
) -> None:
    """Missing repository values reach the launcher's validation error."""
    proc = _run_daemon(tmp_path)

    assert proc.returncode == 1
    assert "BH_REPO_OWNER or BH_REPO_NAME missing from" in proc.stderr
