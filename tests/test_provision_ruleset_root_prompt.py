"""Regression tests for provision-ruleset.sh's BH_PROJECT_ROOT prompt.

When the shared config loader cannot resolve ``BH_PROJECT_ROOT``, an
interactive session prompts once for an absolute project-root path.
Non-interactive sessions and runs with ``BH_SETUP_NO_PROMPT=1`` continue
to fail closed through the script's missing-environment diagnostic.

The interactive coverage verifies both that the prompted value reaches
the diagnostic and that ``.bh/config.env`` is loaded from the newly known
root before required variables are checked. The latter case supplies only
``BH_REPO_OWNER`` in that file, proving the prompted-root config contributes
resolved values without allowing the script to reach live GitHub calls.

The interactive cases use a real pty from ``tests/_bh_pty.py``. Python's
``pty`` module is POSIX-only, so they are skipped on Windows and execute
for real on the project's ``ubuntu-latest`` CI runner.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
SCRIPT = HARNESS / "bin" / "provision-ruleset.sh"

_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""

_REQUIRED_VARS = (
    "BH_REPO_OWNER",
    "BH_REPO_NAME",
    "BH_GITHUB_APP_ID",
    "BH_GITHUB_APP_INSTALLATION_ID",
)


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """Build an env with no real host.env / required vars reachable.

    Args:
        tmp_path: Pytest-provided temp directory for this test, used to
            build an isolated HOME/XDG_CONFIG_HOME so no real operator
            host.env on this machine can supply BH_PROJECT_ROOT or the
            required vars and mask the fatal path under test.

    Returns:
        A fresh environment dict with HOME/XDG_CONFIG_HOME isolated and
        BH_PROJECT_ROOT / the required vars / BH_ADMIN_ROLE_ID /
        BH_DEBUG_CONFIG removed.
    """
    home = tmp_path / "isolated_home"
    home.mkdir()
    xdg_config_home = tmp_path / "isolated_xdg_config"

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            *_REQUIRED_VARS,
            "BH_PROJECT_ROOT",
            "BH_ADMIN_ROLE_ID",
            "BH_DEBUG_CONFIG",
            "BH_SETUP_NO_PROMPT",
        )
    }
    env["PATH"] = os.pathsep.join(
        part for part in [_BASH_BIN_DIR, env.get("PATH", "")] if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    return env


# ---------------------------------------------------------------------------
# Regression guard: non-interactive (BH_SETUP_NO_PROMPT=1) must fail as today
# ---------------------------------------------------------------------------


def test_non_interactive_unresolved_root_regression(tmp_path: Path) -> None:
    """BH_SETUP_NO_PROMPT=1 + unresolved root must keep failing as today.

    A plain subprocess pipe already has no tty, so this is really
    confirming the explicit-opt-out flag doesn't change anything -- the
    genuinely important regression guard for the non-interactive case.
    """
    import subprocess

    env = _isolated_env(tmp_path)
    env["BH_SETUP_NO_PROMPT"] = "1"

    proc = subprocess.run(
        [_BASH, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    assert proc.returncode == 2, (
        f"expected exit 2 for missing required env vars with "
        f"BH_SETUP_NO_PROMPT=1; got rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "provision-ruleset: missing env vars:" in proc.stderr, proc.stderr
    assert "detail: BH_PROJECT_ROOT=(unset)" in proc.stderr, (
        "a non-interactive session (even with BH_SETUP_NO_PROMPT=1 "
        "explicit) must never attempt to prompt for BH_PROJECT_ROOT -- "
        f"it must report (unset) exactly as today; stderr was:\n"
        f"{proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# RED: interactive session must prompt for and use BH_PROJECT_ROOT
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the script's interactivity check and "
        "cannot run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_interactive_session_prompts_for_and_uses_project_root(
    tmp_path: Path,
) -> None:
    """An interactive session must read BH_PROJECT_ROOT from the prompt.

    Feeds a fed-but-still-incomplete path (it has no ``.bh/config.env``
    of its own, so the run still ultimately hits the same generic
    missing-vars diagnostic) as the prompt answer. The diagnostic's
    ``detail: BH_PROJECT_ROOT=`` line must then report that fed path --
    proof the value was actually read from the prompt and plumbed into
    the rest of the script's resolution chain, not merely that the run
    failed for the pre-existing reason.
    """
    from tests._bh_pty import run_interactive

    env = _isolated_env(tmp_path)
    fed_root = tmp_path / "operator-entered-root"
    fed_root.mkdir()
    # Deliberately no .bh/config.env under fed_root -- the required
    # vars remain unresolved even after BH_PROJECT_ROOT is supplied, so
    # the run still reaches the same familiar fatal diagnostic, just
    # with a populated BH_PROJECT_ROOT this time.

    returncode, _pty_output, stderr = run_interactive(
        [_BASH, str(SCRIPT)],
        env,
        input_text=f"{fed_root.as_posix()}\n",
        timeout=30,
    )

    assert returncode == 2, (
        f"expected the same exit 2 fatal diagnostic once BH_PROJECT_ROOT "
        f"is supplied via the prompt (the required vars are still "
        f"unresolved at that path); got rc={returncode}\nstderr:\n{stderr}"
    )
    expected_root = fed_root.as_posix()
    assert f"detail: BH_PROJECT_ROOT={expected_root}" in stderr, (
        "expected the interactively-prompted BH_PROJECT_ROOT value to "
        "be read and used -- today the script never prompts for it at "
        "all, so this detail line always reports (unset) regardless of "
        f"what is fed on stdin; stderr was:\n{stderr!r}"
    )
    assert "detail: BH_PROJECT_ROOT=(unset)" not in stderr, (
        "the prompted value must replace the (unset) default; stderr "
        f"was:\n{stderr!r}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the script's interactivity check and "
        "cannot run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_interactive_session_loads_config_env_from_prompted_root(
    tmp_path: Path,
) -> None:
    """The prompted root's config.env must load before env validation."""
    from tests._bh_pty import run_interactive

    env = _isolated_env(tmp_path)
    fed_root = tmp_path / "operator-entered-root"
    config_dir = fed_root / ".bh"
    config_dir.mkdir(parents=True)
    (config_dir / "config.env").write_text(
        "BH_REPO_OWNER=some-test-owner\n",
        encoding="utf-8",
        newline="\n",
    )

    returncode, _pty_output, stderr = run_interactive(
        [_BASH, str(SCRIPT)],
        env,
        input_text=f"{fed_root.as_posix()}\n",
        timeout=30,
    )

    assert returncode == 2, (
        "the remaining undefined required vars must stop the script "
        f"before any GitHub calls; rc={returncode}\nstderr:\n{stderr}"
    )
    idx = stderr.find("provision-ruleset: missing env vars:")
    assert idx != -1, f"missing env vars line not found; stderr:\n{stderr!r}"
    missing_line = stderr[idx:].splitlines()[0]
    assert "BH_REPO_NAME" in missing_line, missing_line
    assert "BH_GITHUB_APP_ID" in missing_line, missing_line
    assert "BH_GITHUB_APP_INSTALLATION_ID" in missing_line, missing_line
    assert "BH_REPO_OWNER" not in missing_line, missing_line

    expected_config = f"{fed_root.as_posix()}/.bh/config.env"
    assert f"detail: .bh/config.env={expected_config} (exists)" in stderr, (
        "the diagnostic must identify config.env under the prompted "
        f"root; stderr was:\n{stderr!r}"
    )
