"""Issue #321 — bin/provision-ruleset.sh missing-env-vars diagnostic detail.

The existing missing-env-vars fatal path (``bin/provision-ruleset.sh``,
just before ``exit 2``) prints a single summary line naming the missing
variables. This suite covers the new *always-on* (not gated by
``BH_DEBUG_CONFIG``) detail lines that must follow it, giving an
operator enough context to see WHY the vars are missing without
re-running with debug tracing enabled:

  1. ``  detail: BH_PROJECT_ROOT=`` — the current value, or the literal
     ``(unset)``.
  2. ``  detail: .bh/config.env=`` — one of the resolved path + "
     (exists)"/" (does not exist)", or the literal
     "(not checked: BH_PROJECT_ROOT unset)" when BH_PROJECT_ROOT itself
     is unset.

Each test isolates HOME/XDG_CONFIG_HOME to a fresh tmp_path subtree so a
real operator host.env on this machine can never supply the four
required vars and mask the fatal path under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
SCRIPT = HARNESS / "bin" / "provision-ruleset.sh"

# On Windows, the system bash (C:\Windows\System32\bash.exe) launches WSL and
# fails when no WSL distro is configured.  Prefer Git Bash when available.
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


# ---------------------------------------------------------------------------
# Invocation helper
# ---------------------------------------------------------------------------


def _invoke(
    tmp_path: Path, *, project_root: Path | None
) -> tuple[int, str, str]:
    """Run provision-ruleset.sh with all four required env vars unset.

    Args:
        tmp_path: Pytest-provided temp directory for this test, used to
            build an isolated HOME/XDG_CONFIG_HOME so no real host.env
            on this machine can supply the required vars (or
            BH_PROJECT_ROOT) and mask the fatal path under test.
        project_root: Value for BH_PROJECT_ROOT, or None to leave it
            unset.

    Returns:
        (returncode, stdout, stderr) of the script process.
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
        )
    }
    env["PATH"] = os.pathsep.join(
        part for part in [_BASH_BIN_DIR, env.get("PATH", "")] if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    if project_root is not None:
        env["BH_PROJECT_ROOT"] = project_root.as_posix()

    proc = subprocess.run(
        [_BASH, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Case 1: BH_PROJECT_ROOT unset
# ---------------------------------------------------------------------------


def test_missing_env_vars_detail_project_root_unset(tmp_path: Path) -> None:
    """BH_PROJECT_ROOT unset -> both detail lines report unset/not-checked."""
    rc, stdout, stderr = _invoke(tmp_path, project_root=None)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert "provision-ruleset: missing env vars:" in stderr, stderr
    assert "detail: BH_PROJECT_ROOT=(unset)" in stderr, (
        f"expected the BH_PROJECT_ROOT=(unset) detail line; stderr was:\n"
        f"{stderr!r}"
    )
    assert (
        "detail: .bh/config.env=(not checked: BH_PROJECT_ROOT unset)" in stderr
    ), (
        "expected the .bh/config.env not-checked detail line; stderr "
        f"was:\n{stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 2: BH_PROJECT_ROOT set, .bh/config.env absent
# ---------------------------------------------------------------------------


def test_missing_env_vars_detail_config_env_absent(tmp_path: Path) -> None:
    """BH_PROJECT_ROOT set, .bh/config.env absent -> "does not exist"."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    # Deliberately no .bh/config.env under project_root.

    rc, stdout, stderr = _invoke(tmp_path, project_root=project_root)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    expected_root = project_root.as_posix()
    expected_config_env = f"{expected_root}/.bh/config.env"
    assert f"detail: BH_PROJECT_ROOT={expected_root}" in stderr, (
        f"expected the resolved BH_PROJECT_ROOT detail line; stderr "
        f"was:\n{stderr!r}"
    )
    assert (
        f"detail: .bh/config.env={expected_config_env} (does not exist)"
        in stderr
    ), (
        "expected the .bh/config.env (does not exist) detail line with "
        f"the correct resolved path; stderr was:\n{stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 3: BH_PROJECT_ROOT set, .bh/config.env present
# ---------------------------------------------------------------------------


def test_missing_env_vars_detail_config_env_present(tmp_path: Path) -> None:
    """BH_PROJECT_ROOT set, .bh/config.env present -> "(exists)".

    The fixture file deliberately does NOT supply the missing required
    vars — the point of this case is only that the file's *existence*
    is correctly detected and reported, not that its content changes
    the outcome (the fatal path still fires; only the detail wording
    differs from case 2).
    """
    project_root = tmp_path / "project"
    bh_dir = project_root / ".bh"
    bh_dir.mkdir(parents=True)
    (bh_dir / "config.env").write_text(
        "# fixture config.env — deliberately does not set the missing "
        "required vars\n",
        encoding="utf-8",
        newline="\n",
    )

    rc, stdout, stderr = _invoke(tmp_path, project_root=project_root)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    expected_config_env = f"{project_root.as_posix()}/.bh/config.env"
    assert (
        f"detail: .bh/config.env={expected_config_env} (exists)" in stderr
    ), (
        "expected the .bh/config.env (exists) detail line with the "
        f"correct resolved path; stderr was:\n{stderr!r}"
    )


def test_missing_env_vars_per_var_detail_project_root_unset(
    tmp_path: Path,
) -> None:
    """Each missing var reports that config lookup was not possible."""
    rc, stdout, stderr = _invoke(tmp_path, project_root=None)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    for var_name in _REQUIRED_VARS:
        expected = f"  detail: {var_name}: not checked (BH_PROJECT_ROOT unset)"
        assert expected in stderr, (
            f"expected per-variable not-checked detail {expected!r}; "
            f"stderr was:\n{stderr!r}"
        )


def test_missing_env_vars_per_var_detail_config_env_absent(
    tmp_path: Path,
) -> None:
    """Each missing var reports that .bh/config.env does not exist."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    # Deliberately no .bh/config.env under project_root.

    rc, stdout, stderr = _invoke(tmp_path, project_root=project_root)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    for var_name in _REQUIRED_VARS:
        expected = f"  detail: {var_name}: .bh/config.env does not exist"
        assert expected in stderr, (
            f"expected per-variable missing-file detail {expected!r}; "
            f"stderr was:\n{stderr!r}"
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod unreadability is unreliable on Windows/NTFS",
)
def test_missing_env_vars_per_var_detail_config_env_unreadable(
    tmp_path: Path,
) -> None:
    """Each missing var reports that .bh/config.env is unreadable."""
    project_root = tmp_path / "project"
    bh_dir = project_root / ".bh"
    bh_dir.mkdir(parents=True)
    config_env = bh_dir / "config.env"
    config_env.write_text("SOME_UNRELATED_VAR=foo\n", encoding="utf-8")
    os.chmod(config_env, 0o000)

    rc, stdout, stderr = _invoke(tmp_path, project_root=project_root)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    for var_name in _REQUIRED_VARS:
        expected = (
            f"  detail: {var_name}: .bh/config.env exists but is not readable"
        )
        assert expected in stderr, (
            f"expected per-variable unreadable-file detail {expected!r}; "
            f"stderr was:\n{stderr!r}"
        )
        not_defined = f"  detail: {var_name}: not defined in .bh/config.env"
        assert not_defined not in stderr, (
            f"unexpected undefined-key detail {not_defined!r}; "
            f"stderr was:\n{stderr!r}"
        )


def test_missing_env_vars_per_var_detail_not_defined_in_config_env(
    tmp_path: Path,
) -> None:
    """Each absent assignment is distinguished from an empty value."""
    project_root = tmp_path / "project"
    bh_dir = project_root / ".bh"
    bh_dir.mkdir(parents=True)
    (bh_dir / "config.env").write_text(
        "SOME_UNRELATED_VAR=foo\n",
        encoding="utf-8",
        newline="\n",
    )

    rc, stdout, stderr = _invoke(tmp_path, project_root=project_root)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    for var_name in _REQUIRED_VARS:
        expected = f"  detail: {var_name}: not defined in .bh/config.env"
        assert expected in stderr, (
            f"expected per-variable undefined-key detail {expected!r}; "
            f"stderr was:\n{stderr!r}"
        )


def test_missing_env_vars_per_var_detail_present_but_empty(
    tmp_path: Path,
) -> None:
    """An assigned empty value is distinguished from absent assignments."""
    project_root = tmp_path / "project"
    bh_dir = project_root / ".bh"
    bh_dir.mkdir(parents=True)
    (bh_dir / "config.env").write_text(
        "BH_GITHUB_APP_ID=\n",
        encoding="utf-8",
        newline="\n",
    )

    rc, stdout, stderr = _invoke(tmp_path, project_root=project_root)

    assert rc == 2, (
        f"expected exit 2 for missing required env vars; got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    empty_expected = (
        "  detail: BH_GITHUB_APP_ID: present in .bh/config.env "
        "but resolved empty"
    )
    assert empty_expected in stderr, (
        f"expected present-but-empty detail {empty_expected!r}; "
        f"stderr was:\n{stderr!r}"
    )
    for var_name in (
        "BH_REPO_OWNER",
        "BH_REPO_NAME",
        "BH_GITHUB_APP_INSTALLATION_ID",
    ):
        expected = f"  detail: {var_name}: not defined in .bh/config.env"
        assert expected in stderr, (
            f"expected per-variable undefined-key detail {expected!r}; "
            f"stderr was:\n{stderr!r}"
        )
