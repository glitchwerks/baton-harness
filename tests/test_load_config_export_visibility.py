"""Regression coverage for load-config.sh subprocess visibility."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
LOAD_CONFIG = HARNESS / "bin" / "lib" / "load-config.sh"

# On Windows, the system bash (C:\Windows\System32\bash.exe) launches WSL and
# fails when no WSL distro is configured.  Prefer Git Bash when available.
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""

_NEW_VAR = "BH_TEST_NEW_VAR"
_OVERRIDE_VAR = "BH_TEST_OVERRIDE_VAR"


def _run_config(
    tmp_path: Path, config: str, *, operator_value: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Source host.env and print its values from a grandchild process.

    Args:
        tmp_path: Pytest-provided temporary directory for the test.
        config: Contents to write to the isolated host.env fixture.
        operator_value: Optional pre-existing environment value for the
            operator-override fixture variable.

    Returns:
        The completed wrapper process, including captured output.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"
    host_env_dir = xdg_config_home / "baton-harness"
    host_env_dir.mkdir(parents=True)
    (host_env_dir / "host.env").write_text(
        config, encoding="utf-8", newline="\n"
    )

    wrapper = tmp_path / "source_wrapper.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'source "$1"\n'
        'bash -c \'printf "%s\\n" '
        '"${BH_TEST_NEW_VAR-}" "${BH_TEST_OVERRIDE_VAR-}"\'\n',
        encoding="utf-8",
        newline="\n",
    )

    env = dict(os.environ)
    env.pop(_NEW_VAR, None)
    env.pop(_OVERRIDE_VAR, None)
    env.pop("BH_PROJECT_ROOT", None)
    env["PATH"] = os.pathsep.join(
        part for part in [_BASH_BIN_DIR, env.get("PATH", "")] if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    if operator_value is not None:
        env[_OVERRIDE_VAR] = operator_value

    return subprocess.run(
        [_BASH, str(wrapper), str(LOAD_CONFIG)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_new_plain_config_variable_is_visible_to_grandchild(
    tmp_path: Path,
) -> None:
    """A new plain assignment reaches a process spawned after loading."""
    proc = _run_config(tmp_path, "BH_TEST_NEW_VAR=hello-from-config\n")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "hello-from-config\n\n"


def test_operator_override_is_restored_and_visible_to_grandchild(
    tmp_path: Path,
) -> None:
    """A pre-existing exported value wins and reaches a spawned process."""
    proc = _run_config(
        tmp_path,
        "BH_TEST_OVERRIDE_VAR=value-from-config\n",
        operator_value="operator-value",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "\noperator-value\n"
