"""Issue #321 — bin/lib/load-config.sh opt-in debug tracing.

``load-config.sh`` has no shebang and is designed to be *sourced*, not
executed — see the file's own header comment. This suite drives it with
a tiny bash wrapper that sources the real file under ``set -euo
pipefail`` (mirroring how ``bin/provision-ruleset.sh`` sources it in
production) and inspects the resulting stderr.

New env var ``BH_DEBUG_CONFIG`` gates ALL new output:

- Unset, or any value other than exactly ``"1"`` (including the
  explicit ``"0"`` case) -> zero new stderr lines. This is the default,
  backward-compatible path every existing caller of ``load-config.sh``
  exercises today, so it must never regress.
- Exactly ``"1"`` -> four possible ``config-debug:`` lines, one pair
  per config-chain step (host.env, then .bh/config.env), selecting the
  found/not-found variant per step based on whether the resolved file
  actually exists (and, for step 2, whether ``BH_PROJECT_ROOT`` is even
  known yet).

Each test isolates ``HOME``/``XDG_CONFIG_HOME`` to a fresh tmp_path
subtree so a real operator host.env on this machine can never leak into
a test's "not found" expectation, and isolates ``BH_PROJECT_ROOT`` the
same way (explicit set/unset per case) for the same reason.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Invocation helper
# ---------------------------------------------------------------------------


def _run(
    tmp_path: Path,
    *,
    debug: str | None,
    home: Path,
    xdg_config_home: Path,
    project_root: Path | None,
) -> tuple[int, str, str]:
    """Source load-config.sh in an isolated bash and capture its output.

    Writes a small wrapper script that sources ``load-config.sh`` under
    ``set -euo pipefail`` — the same strict mode
    ``bin/provision-ruleset.sh`` runs under in production — so this
    suite exercises the file the way a real caller does, not a looser
    ad-hoc shell.

    Args:
        tmp_path: Pytest-provided temp directory for this test.
        debug: Value for BH_DEBUG_CONFIG, or None to leave it unset.
        home: Directory to use as HOME for the subprocess.
        xdg_config_home: Directory to use as XDG_CONFIG_HOME for the
            subprocess (always set explicitly, even when the intended
            host.env under it is absent, so step 1's resolved path is
            deterministic across test runs/machines).
        project_root: Value for BH_PROJECT_ROOT, or None to leave it
            unset.

    Returns:
        (returncode, stdout, stderr) of the wrapper process.
    """
    wrapper = tmp_path / "source_wrapper.sh"
    wrapper.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nsource "$1"\n',
        encoding="utf-8",
        newline="\n",
    )

    env = dict(os.environ)
    env.pop("BH_DEBUG_CONFIG", None)
    env.pop("BH_PROJECT_ROOT", None)
    env["PATH"] = os.pathsep.join(
        part for part in [_BASH_BIN_DIR, env.get("PATH", "")] if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    if project_root is not None:
        env["BH_PROJECT_ROOT"] = project_root.as_posix()
    if debug is not None:
        env["BH_DEBUG_CONFIG"] = debug

    proc = subprocess.run(
        [_BASH, str(wrapper), str(LOAD_CONFIG)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


def _host_env_path(xdg_config_home: Path) -> str:
    """Build the expected resolved host.env path as load-config.sh would.

    Args:
        xdg_config_home: The XDG_CONFIG_HOME value used for the run.

    Returns:
        The posix-style resolved host.env path string.
    """
    return f"{xdg_config_home.as_posix()}/baton-harness/host.env"


def _config_env_path(project_root: Path) -> str:
    """Build the expected resolved .bh/config.env path as the script would.

    Args:
        project_root: The BH_PROJECT_ROOT value used for the run.

    Returns:
        The posix-style resolved .bh/config.env path string.
    """
    return f"{project_root.as_posix()}/.bh/config.env"


# ---------------------------------------------------------------------------
# Case 1 / 5: BH_DEBUG_CONFIG unset or explicitly "0" -> zero new output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host_env_present", [False, True])
@pytest.mark.parametrize("project_root_set", [False, True])
def test_debug_unset_emits_no_stderr(
    tmp_path: Path, host_env_present: bool, project_root_set: bool
) -> None:
    """BH_DEBUG_CONFIG unset -> zero stderr, across all four state combos.

    Covers both host.env presence variants and both BH_PROJECT_ROOT
    presence variants (the full state space step 1 and step 2 branch
    on) to prove the gate is unconditional, not just "happens to be
    quiet in the common case".
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"
    if host_env_present:
        host_env_dir = xdg_config_home / "baton-harness"
        host_env_dir.mkdir(parents=True)
        (host_env_dir / "host.env").write_text(
            "# fixture host.env\n", encoding="utf-8", newline="\n"
        )

    project_root = None
    if project_root_set:
        project_root = tmp_path / "project"
        project_root.mkdir()

    rc, stdout, stderr = _run(
        tmp_path,
        debug=None,
        home=home,
        xdg_config_home=xdg_config_home,
        project_root=project_root,
    )

    assert rc == 0, (
        f"sourcing load-config.sh must not fail; stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    assert stderr == "", (
        "BH_DEBUG_CONFIG unset must produce zero new stderr lines "
        f"(backward compatibility); stderr was:\n{stderr!r}"
    )


def test_debug_explicit_zero_emits_no_stderr(tmp_path: Path) -> None:
    """Case 5: BH_DEBUG_CONFIG=0 is treated identically to unset."""
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"

    rc, stdout, stderr = _run(
        tmp_path,
        debug="0",
        home=home,
        xdg_config_home=xdg_config_home,
        project_root=None,
    )

    assert rc == 0, (
        f"sourcing load-config.sh must not fail; stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    assert stderr == "", (
        "BH_DEBUG_CONFIG=0 must be treated the same as unset — only the "
        f"exact value '1' may enable debug output; stderr was:\n{stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 2: BH_DEBUG_CONFIG=1, host.env absent, BH_PROJECT_ROOT unset
# ---------------------------------------------------------------------------


def test_debug_enabled_host_env_absent_and_project_root_unset(
    tmp_path: Path,
) -> None:
    """Both not-found/unset debug lines fire when nothing is configured."""
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"

    rc, stdout, stderr = _run(
        tmp_path,
        debug="1",
        home=home,
        xdg_config_home=xdg_config_home,
        project_root=None,
    )

    assert rc == 0, (
        f"sourcing load-config.sh must not fail; stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    expected_host_env = _host_env_path(xdg_config_home)
    assert (
        "baton-harness: config-debug: host.env not found — skipping "
        f"{expected_host_env}" in stderr
    ), f"expected host.env not-found debug line; stderr was:\n{stderr!r}"
    assert (
        "baton-harness: config-debug: BH_PROJECT_ROOT unset — skipping "
        ".bh/config.env lookup" in stderr
    ), (
        "expected the BH_PROJECT_ROOT-unset debug line; stderr was:\n"
        f"{stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 3: BH_DEBUG_CONFIG=1, both host.env and .bh/config.env found
# ---------------------------------------------------------------------------


def test_debug_enabled_both_found_with_correct_resolved_paths(
    tmp_path: Path,
) -> None:
    """Both found-and-sourcing debug lines fire with correct paths."""
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"
    host_env_dir = xdg_config_home / "baton-harness"
    host_env_dir.mkdir(parents=True)
    (host_env_dir / "host.env").write_text(
        "# fixture host.env\n", encoding="utf-8", newline="\n"
    )

    project_root = tmp_path / "project"
    bh_dir = project_root / ".bh"
    bh_dir.mkdir(parents=True)
    (bh_dir / "config.env").write_text(
        "# fixture config.env\n", encoding="utf-8", newline="\n"
    )

    rc, stdout, stderr = _run(
        tmp_path,
        debug="1",
        home=home,
        xdg_config_home=xdg_config_home,
        project_root=project_root,
    )

    assert rc == 0, (
        f"sourcing load-config.sh must not fail; stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    expected_host_env = _host_env_path(xdg_config_home)
    expected_config_env = _config_env_path(project_root)
    assert (
        "baton-harness: config-debug: host.env found — sourcing "
        f"{expected_host_env}" in stderr
    ), f"expected host.env found debug line; stderr was:\n{stderr!r}"
    assert (
        "baton-harness: config-debug: .bh/config.env found — sourcing "
        f"{expected_config_env}" in stderr
    ), (
        "expected .bh/config.env found debug line with the correct "
        f"resolved path; stderr was:\n{stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 4: BH_DEBUG_CONFIG=1, BH_PROJECT_ROOT set but .bh/config.env absent
# ---------------------------------------------------------------------------


def test_debug_enabled_project_root_set_but_config_env_absent(
    tmp_path: Path,
) -> None:
    """The not-found debug line fires for .bh/config.env specifically."""
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"

    project_root = tmp_path / "project"
    project_root.mkdir()
    # Deliberately no .bh/config.env under project_root.

    rc, stdout, stderr = _run(
        tmp_path,
        debug="1",
        home=home,
        xdg_config_home=xdg_config_home,
        project_root=project_root,
    )

    assert rc == 0, (
        f"sourcing load-config.sh must not fail; stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    expected_config_env = _config_env_path(project_root)
    assert (
        "baton-harness: config-debug: .bh/config.env not found — "
        f"skipping {expected_config_env}" in stderr
    ), (
        "expected .bh/config.env not-found debug line with the correct "
        f"resolved path; stderr was:\n{stderr!r}"
    )
