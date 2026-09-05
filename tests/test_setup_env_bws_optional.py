"""Black-box coverage for optional BWS handling in ``setup-env.sh``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
SETUP_ENV = HARNESS / "bin" / "setup-env.sh"
LOAD_CONFIG = HARNESS / "bin" / "lib" / "load-config.sh"

_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""

_STUBS = {
    "uv": """#!/usr/bin/env bash
printf 'uv:%s\n' "$*" >> "$SETUP_TEST_LOG"
""",
    "gh": """#!/usr/bin/env bash
printf 'gh:%s\n' "$*" >> "$SETUP_TEST_LOG"
printf 'gh version 2.62.0-test\n'
""",
    "claude": """#!/usr/bin/env bash
printf 'claude:%s\n' "$*" >> "$SETUP_TEST_LOG"
printf 'claude 1.0-test\n'
""",
    "prek": """#!/usr/bin/env bash
printf 'prek:%s\n' "$*" >> "$SETUP_TEST_LOG"
""",
    "curl": """#!/usr/bin/env bash
printf 'curl:%s\n' "$*" >> "$SETUP_TEST_LOG"
exit 97
""",
}

_BWS_STUB = """#!/usr/bin/env bash
printf 'bws:%s\n' "$*" >> "$SETUP_TEST_LOG"
printf 'bws 2.1.0-test\n'
"""


def _write_executable(path: Path, content: str) -> None:
    """Write one executable shell-test stub.

    Args:
        path: Stub path to create.
        content: Complete shell script content.
    """
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _path_without_bws() -> list[str]:
    """Return host PATH entries that do not expose a BWS executable.

    Returns:
        PATH entries safe for a missing-BWS test process.
    """
    safe_entries: list[str] = []
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = raw_entry.strip('"')
        if not entry:
            continue
        directory = Path(entry)
        if any((directory / name).exists() for name in ("bws", "bws.exe")):
            continue
        safe_entries.append(entry)
    return safe_entries


def _make_harness_fixture(tmp_path: Path) -> Path:
    """Copy the real setup surfaces into an isolated harness layout.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the copied ``setup-env.sh``.
    """
    harness = tmp_path / "harness"
    setup = harness / "bin" / "setup-env.sh"
    load_config = harness / "bin" / "lib" / "load-config.sh"
    load_config.parent.mkdir(parents=True)
    shutil.copy2(SETUP_ENV, setup)
    shutil.copy2(LOAD_CONFIG, load_config)
    daemon = harness / ".venv" / "Scripts" / "bh-daemon"
    daemon.parent.mkdir(parents=True)
    daemon.write_text("test console entry point\n", encoding="utf-8")
    return setup


def _base_env(tmp_path: Path, fake_bin: Path) -> dict[str, str]:
    """Build an isolated setup-script environment.

    Args:
        tmp_path: Pytest temporary directory.
        fake_bin: Directory containing controlled CLI stubs.

    Returns:
        Environment with no ambient BWS executable or secret configuration.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg-config"
    xdg_config_home.mkdir()
    log_path = tmp_path / "commands.log"

    env = dict(os.environ)
    for key in (
        "BH_SETUP_NO_PROMPT",
        "BH_PROJECT_ROOT",
        "BWS_ACCESS_TOKEN",
        "BH_GITHUB_APP_KEY_PROVIDER",
        "BWS_GH_TOKEN_SECRET_ID",
        "BWS_HEARTBEAT_PING_URL_SECRET_ID",
    ):
        env.pop(key, None)
    env["PATH"] = os.pathsep.join(
        entry
        for entry in (
            fake_bin.as_posix(),
            _BASH_BIN_DIR,
            *_path_without_bws(),
        )
        if entry
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    env["SETUP_TEST_LOG"] = log_path.as_posix()
    return env


def _run_setup(
    tmp_path: Path,
    *,
    include_bws: bool = False,
    interactive_input: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the copied real setup script against controlled CLI stubs.

    Args:
        tmp_path: Pytest temporary directory.
        include_bws: Whether the controlled PATH contains BWS.
        interactive_input: Answers to supply through a POSIX PTY.

    Returns:
        Completed process and command-log path.
    """
    setup = _make_harness_fixture(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, content in _STUBS.items():
        _write_executable(fake_bin / name, content)
    if include_bws:
        _write_executable(fake_bin / "bws", _BWS_STUB)

    env = _base_env(tmp_path, fake_bin)
    log_path = Path(env["SETUP_TEST_LOG"])
    if interactive_input is not None:
        from tests._bh_pty import run_interactive

        returncode, stdout, stderr = run_interactive(
            [_BASH, str(setup)],
            env,
            input_text=interactive_input,
            timeout=30,
        )
        return (
            subprocess.CompletedProcess([], returncode, stdout, stderr),
            log_path,
        )

    env["BH_SETUP_NO_PROMPT"] = "1"
    proc = subprocess.run(
        [_BASH, str(setup)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    return proc, log_path


def test_missing_bws_no_prompt_continues_without_download(
    tmp_path: Path,
) -> None:
    """Missing optional BWS must not stop the later package-install phase."""
    proc, log_path = _run_setup(tmp_path)

    assert proc.returncode == 0, proc.stderr
    command_log = log_path.read_text(encoding="utf-8")
    assert "uv:pip install" in command_log
    assert "curl:" not in command_log
    assert "bws" in proc.stderr.lower()
    assert "optional" in proc.stderr.lower()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX pty required for setup-env interactive branch",
)
def test_declining_interactive_bws_install_continues(
    tmp_path: Path,
) -> None:
    """Declining optional BWS installation must still complete setup."""
    proc, log_path = _run_setup(tmp_path, interactive_input="n\n\n")

    assert proc.returncode == 0, proc.stderr
    command_log = log_path.read_text(encoding="utf-8")
    assert "uv:pip install" in command_log
    assert "curl:" not in command_log
    assert "bws install declined" in proc.stderr.lower()
    assert "optional" in proc.stderr.lower()


def test_present_bws_retains_version_check(tmp_path: Path) -> None:
    """A present BWS executable must still be version-checked and reported."""
    proc, log_path = _run_setup(tmp_path, include_bws=True)
    command_log = log_path.read_text(encoding="utf-8")

    assert proc.returncode == 0, proc.stderr
    assert "bws:--version" in command_log
    assert "bws already on PATH (bws 2.1.0-test)" in proc.stdout


def test_missing_token_notice_is_conditional(tmp_path: Path) -> None:
    """The absent-token notice must name only configurations that use BWS."""
    proc, _log_path = _run_setup(tmp_path)
    guidance = proc.stderr.lower()

    assert proc.returncode == 0, proc.stderr
    assert "bws_access_token not set" in guidance
    assert "app-key provider" in guidance
    assert "pat" in guidance
    assert "heartbeat" in guidance
    assert "only when" in guidance


def test_present_token_value_is_never_printed(tmp_path: Path) -> None:
    """The setup notice may report token presence but never its value."""
    setup = _make_harness_fixture(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, content in _STUBS.items():
        _write_executable(fake_bin / name, content)
    env = _base_env(tmp_path, fake_bin)
    sentinel = "task-7a-secret-token-sentinel"
    env["BH_SETUP_NO_PROMPT"] = "1"
    env["BWS_ACCESS_TOKEN"] = sentinel

    proc = subprocess.run(
        [_BASH, str(setup)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "BWS_ACCESS_TOKEN already set" in proc.stdout
    assert "only when" in proc.stdout.lower()
    assert "app-key provider" in proc.stdout.lower()
    assert "pat" in proc.stdout.lower()
    assert "heartbeat" in proc.stdout.lower()
    assert sentinel not in proc.stdout + proc.stderr
