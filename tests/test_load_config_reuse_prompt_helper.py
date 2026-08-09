"""Failing tests for the shared reuse-vs-overwrite prompt helper (#352).

Issue #352's acceptance criteria call for the existence-check /
overwrite-vs-reuse prompt logic to live in one shared place (``bin/lib/
load-config.sh`` or a sibling lib file) so all four setup scripts
(``init-sandbox.sh``, ``setup-env.sh``, ``provision-ruleset.sh``,
``install-daemon-service.sh``) share one implementation instead of four
copies. This suite pins the contract for that shared piece directly,
independent of any one caller script, by sourcing ``bin/lib/
load-config.sh`` in an isolated bash and driving a new function that
does not exist yet::

    _bh_resolve_config_with_reuse_prompt <config_file_path> \
        <prompt_and_write_fn_name>

Where ``<prompt_and_write_fn_name>`` is the *name* of a caller-supplied
shell function this helper invokes (with no arguments -- the spec does
not require any argument-passing convention beyond the two named
parameters, so this suite does not test one) to do the actual "prompt
for values and write the file" work when a fresh write is needed.

Contract under test:

  1. File absent -> the helper calls the callback exactly once and
     returns 0 (the fresh-init path, unaffected by session mode).
  2. File present, non-interactive session (``BH_SETUP_NO_PROMPT=1`` OR
     plain non-tty pipes with no flag) -> the helper must NOT call the
     callback, must NOT modify the existing file, and must return
     non-zero (fail closed -- there is no way to ask, so it refuses to
     silently pick a side).
  3. File present, interactive session, operator chooses "reuse" -> the
     helper must NOT call the callback, file bytes unchanged, returns
     0.
  4. File present, interactive session, operator chooses "overwrite" ->
     the helper DOES call the callback exactly once, returns 0.

Answer-format assumption (this test author's choice, since the spec
does not pin the exact accepted input): the helper reads one line via
a ``read -r -p`` style prompt, and treats the trimmed answer
case-insensitively -- the literal word ``overwrite`` selects overwrite;
anything else (empty/default, ``reuse``, ``n``, ...) selects the safe
default of reuse. This suite feeds the literal words ``overwrite`` and
``reuse`` for cases 4 and 3 respectively so the Phase-2 implementation
has an unambiguous, satisfiable target. Assertions on the prompt's own
wording are kept to a loose case-insensitive substring match (something
mentioning "overwrite" or "existing"), per issue #352's own preference
for leaving exact prompt wording to the implementer.

Confirmed red today: ``_bh_resolve_config_with_reuse_prompt`` does not
exist anywhere in ``bin/lib/load-config.sh``, so sourcing the real file
and then calling the function fails with a bash "command not found"
(exit 127) every time, regardless of scenario -- see the return summary
for this agent's task for the exact captured output.

Interactive cases (3 and 4) are driven via a real pty (see
``tests/_bh_pty.py``) and, per that helper's own POSIX-only
constraint, are skipped (not failed) on this Windows host; they run for
real on the project's CI runner (``ubuntu-latest`` per
``.github/workflows/ci.yml``).
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
# fails when no WSL distro is configured.  Prefer Git Bash when available
# (same resolution as every other bash-driving test in this suite).
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""

# Wrapper sources the real load-config.sh (exactly how every caller script
# does it) then drives the function under test, capturing its return code
# without letting `set -e` abort the wrapper before that code is reported.
_WRAPPER_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source "$1"

CONFIG_FILE="$2"
MARKER_FILE="$3"

stub_prompt_and_write() {
    printf 'called\\n' >> "$MARKER_FILE"
    printf '%s\\n' "BH_STUB_WRITTEN=1" > "$CONFIG_FILE"
}

rc=0
_bh_resolve_config_with_reuse_prompt "$CONFIG_FILE" stub_prompt_and_write \\
    || rc=$?
echo "HELPER_EXIT=$rc"
"""

_WRAPPER_SCRIPT_FAILING_CALLBACK = """#!/usr/bin/env bash
set -euo pipefail
source "$1"

CONFIG_FILE="$2"
MARKER_FILE="$3"

stub_prompt_and_write() {
    printf 'called\\n' >> "$MARKER_FILE"
    return 3
}

rc=0
_bh_resolve_config_with_reuse_prompt "$CONFIG_FILE" stub_prompt_and_write \\
    || rc=$?
echo "HELPER_EXIT=$rc"
"""


def _write_wrapper(tmp_path: Path) -> Path:
    """Write the shared reuse-prompt-helper driver wrapper script.

    Args:
        tmp_path: Pytest-provided temp directory for this test.

    Returns:
        Path to the written wrapper script.
    """
    wrapper = tmp_path / "reuse_prompt_wrapper.sh"
    wrapper.write_text(_WRAPPER_SCRIPT, encoding="utf-8", newline="\n")
    return wrapper


def _write_failing_wrapper(tmp_path: Path) -> Path:
    """Write a reuse-prompt driver whose callback returns non-zero.

    Args:
        tmp_path: Pytest-provided temp directory for this test.

    Returns:
        Path to the written wrapper script.
    """
    wrapper = tmp_path / "reuse_prompt_failing_callback_wrapper.sh"
    wrapper.write_text(
        _WRAPPER_SCRIPT_FAILING_CALLBACK, encoding="utf-8", newline="\n"
    )
    return wrapper


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Build an isolated environment for driving the wrapper script.

    Args:
        tmp_path: Pytest-provided temp directory for this test, used to
            build an isolated HOME/XDG_CONFIG_HOME so no real operator
            host.env on this machine can leak into a run.

    Returns:
        A fresh environment dict with HOME/XDG_CONFIG_HOME isolated and
        the relevant control vars removed.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    xdg_config_home = tmp_path / "xdg_config"
    xdg_config_home.mkdir(exist_ok=True)

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in ("BH_SETUP_NO_PROMPT", "BH_PROJECT_ROOT", "BH_DEBUG_CONFIG")
    }
    env["PATH"] = os.pathsep.join(
        part for part in [_BASH_BIN_DIR, env.get("PATH", "")] if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    return env


def _extract_helper_exit(output: str) -> int:
    """Parse the ``HELPER_EXIT=<n>`` marker line out of captured output.

    Args:
        output: Captured stdout (or pty output) from a wrapper run.

    Returns:
        The integer exit code the wrapper reported for the function
        under test.
    """
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("HELPER_EXIT="):
            return int(line.split("=", 1)[1])
    raise AssertionError(
        f"HELPER_EXIT marker not found in captured output:\n{output!r}"
    )


# ---------------------------------------------------------------------------
# Case 1: file absent -> fresh-init path unaffected
# ---------------------------------------------------------------------------


def test_absent_config_file_calls_callback_once_and_succeeds(
    tmp_path: Path,
) -> None:
    """No pre-existing config file -> the callback runs once, helper is 0.

    Exercised non-interactively (plain subprocess pipes, no tty) since
    the fresh-init path is specified to be unaffected by session mode --
    there is nothing to ask about when there is no existing file yet.
    """
    wrapper = _write_wrapper(tmp_path)
    config_file = tmp_path / "host.env"
    marker_file = tmp_path / "marker.txt"

    env = _base_env(tmp_path)
    env["BH_SETUP_NO_PROMPT"] = "1"

    proc = subprocess.run(
        [
            _BASH,
            str(wrapper),
            str(LOAD_CONFIG),
            str(config_file),
            str(marker_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    helper_exit = _extract_helper_exit(proc.stdout)
    assert helper_exit == 0, (
        "expected the helper to succeed on the fresh-init (file-absent) "
        f"path; got exit {helper_exit}\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert marker_file.exists(), (
        "expected the prompt-and-write callback to have been invoked "
        f"when no config file exists yet; stderr:\n{proc.stderr}"
    )
    assert marker_file.read_text(encoding="utf-8") == "called\n", (
        "the prompt-and-write callback must be invoked exactly once on "
        f"the fresh-init path; marker contents:\n"
        f"{marker_file.read_text(encoding='utf-8')!r}"
    )


def test_absent_config_file_propagates_callback_failure(
    tmp_path: Path,
) -> None:
    """A failing fresh-init callback must make the helper fail."""
    wrapper = _write_failing_wrapper(tmp_path)
    config_file = tmp_path / "host.env"
    marker_file = tmp_path / "marker.txt"

    env = _base_env(tmp_path)
    env["BH_SETUP_NO_PROMPT"] = "1"

    proc = subprocess.run(
        [
            _BASH,
            str(wrapper),
            str(LOAD_CONFIG),
            str(config_file),
            str(marker_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    helper_exit = _extract_helper_exit(proc.stdout)
    assert helper_exit != 0, (
        "the helper must propagate a fresh-init callback failure; "
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert marker_file.read_text(encoding="utf-8") == "called\n", (
        "the failing callback must still run exactly once; marker "
        f"contents:\n{marker_file.read_text(encoding='utf-8')!r}"
    )


# ---------------------------------------------------------------------------
# Case 2: file present, non-interactive -> fail closed, no modification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "set_no_prompt_flag",
    [True, False],
    ids=["BH_SETUP_NO_PROMPT=1", "non-tty-pipes-only"],
)
def test_existing_config_file_non_interactive_fails_closed(
    tmp_path: Path, set_no_prompt_flag: bool
) -> None:
    """A pre-existing file + no way to ask must fail closed, untouched.

    Covers both routes to "non-interactive" the briefing calls out:
    the explicit ``BH_SETUP_NO_PROMPT=1`` opt-out, and plain non-tty
    stdin/stdout with no flag set at all (the ordinary case for any
    script run under a subprocess pipe).
    """
    wrapper = _write_wrapper(tmp_path)
    config_file = tmp_path / "host.env"
    original_bytes = b"BH_EXISTING=preexisting\n"
    config_file.write_bytes(original_bytes)
    marker_file = tmp_path / "marker.txt"

    env = _base_env(tmp_path)
    if set_no_prompt_flag:
        env["BH_SETUP_NO_PROMPT"] = "1"

    proc = subprocess.run(
        [
            _BASH,
            str(wrapper),
            str(LOAD_CONFIG),
            str(config_file),
            str(marker_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    helper_exit = _extract_helper_exit(proc.stdout)
    assert helper_exit != 0, (
        "a non-interactive session with an existing config file has no "
        "way to ask, and must fail closed (non-zero) rather than "
        f"silently picking a side; stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert not marker_file.exists(), (
        "the prompt-and-write callback must never be invoked when the "
        "session cannot obtain a choice from the operator; stderr:\n"
        f"{proc.stderr}"
    )
    assert config_file.read_bytes() == original_bytes, (
        "a non-interactive run must never modify a pre-existing config "
        f"file it cannot obtain a choice for; stderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Case 3: file present, interactive, operator chooses "reuse"
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the interactivity check and cannot "
        "run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_existing_config_file_interactive_reuse_choice_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    """Choosing "reuse" interactively must never call the callback.

    Feeds the literal word ``reuse`` as the prompt answer (see the
    module docstring for the answer-format assumption this suite
    pins).
    """
    from tests._bh_pty import run_interactive

    wrapper = _write_wrapper(tmp_path)
    config_file = tmp_path / "host.env"
    original_bytes = b"BH_EXISTING=preexisting\n"
    config_file.write_bytes(original_bytes)
    marker_file = tmp_path / "marker.txt"

    env = _base_env(tmp_path)
    env.pop("BH_SETUP_NO_PROMPT", None)

    returncode, pty_output, stderr = run_interactive(
        [
            _BASH,
            str(wrapper),
            str(LOAD_CONFIG),
            str(config_file),
            str(marker_file),
        ],
        env,
        input_text="reuse\n",
        timeout=30,
    )

    assert returncode == 0, (
        "the wrapper script itself always exits 0 (it captures the "
        f"helper's own rc rather than propagating it); rc={returncode}\n"
        f"pty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    helper_exit = _extract_helper_exit(pty_output)
    assert helper_exit == 0, (
        "expected the helper to succeed once the operator chooses "
        f"reuse; got exit {helper_exit}\npty_output:\n{pty_output}\n"
        f"stderr:\n{stderr}"
    )
    assert not marker_file.exists(), (
        "choosing to reuse must never invoke the prompt-and-write "
        f"callback; stderr:\n{stderr}"
    )
    assert config_file.read_bytes() == original_bytes, (
        f"choosing to reuse must leave the existing file untouched; "
        f"stderr:\n{stderr}"
    )
    stderr_lower = stderr.lower()
    assert "overwrite" in stderr_lower or "existing" in stderr_lower, (
        "expected the interactive prompt to mention the choice being "
        f"made (overwrite vs existing); stderr:\n{stderr!r}"
    )


# ---------------------------------------------------------------------------
# Case 4: file present, interactive, operator chooses "overwrite"
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the interactivity check and cannot "
        "run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_existing_config_file_interactive_overwrite_choice_calls_callback(
    tmp_path: Path,
) -> None:
    """Choosing "overwrite" interactively must call the callback once.

    Feeds the literal word ``overwrite`` as the prompt answer (see the
    module docstring for the answer-format assumption this suite
    pins).
    """
    from tests._bh_pty import run_interactive

    wrapper = _write_wrapper(tmp_path)
    config_file = tmp_path / "host.env"
    original_bytes = b"BH_EXISTING=preexisting\n"
    config_file.write_bytes(original_bytes)
    marker_file = tmp_path / "marker.txt"

    env = _base_env(tmp_path)
    env.pop("BH_SETUP_NO_PROMPT", None)

    returncode, pty_output, stderr = run_interactive(
        [
            _BASH,
            str(wrapper),
            str(LOAD_CONFIG),
            str(config_file),
            str(marker_file),
        ],
        env,
        input_text="overwrite\n",
        timeout=30,
    )

    assert returncode == 0, (
        "the wrapper script itself always exits 0 (it captures the "
        f"helper's own rc rather than propagating it); rc={returncode}\n"
        f"pty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    helper_exit = _extract_helper_exit(pty_output)
    assert helper_exit == 0, (
        "expected the helper to succeed once the operator chooses "
        f"overwrite; got exit {helper_exit}\npty_output:\n{pty_output}\n"
        f"stderr:\n{stderr}"
    )
    assert marker_file.exists(), (
        "choosing to overwrite must invoke the prompt-and-write "
        f"callback; stderr:\n{stderr}"
    )
    assert marker_file.read_text(encoding="utf-8") == "called\n", (
        "the prompt-and-write callback must be invoked exactly once "
        f"when overwrite is chosen; marker contents:\n"
        f"{marker_file.read_text(encoding='utf-8')!r}"
    )
    stderr_lower = stderr.lower()
    assert "overwrite" in stderr_lower or "existing" in stderr_lower, (
        "expected the interactive prompt to mention the choice being "
        f"made (overwrite vs existing); stderr:\n{stderr!r}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the interactivity check and cannot "
        "run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_interactive_overwrite_propagates_callback_failure(
    tmp_path: Path,
) -> None:
    """A failing overwrite callback must make the helper fail."""
    from tests._bh_pty import run_interactive

    wrapper = _write_failing_wrapper(tmp_path)
    config_file = tmp_path / "host.env"
    config_file.write_text("BH_EXISTING=preexisting\n", encoding="utf-8")
    marker_file = tmp_path / "marker.txt"

    env = _base_env(tmp_path)
    env.pop("BH_SETUP_NO_PROMPT", None)

    returncode, pty_output, stderr = run_interactive(
        [
            _BASH,
            str(wrapper),
            str(LOAD_CONFIG),
            str(config_file),
            str(marker_file),
        ],
        env,
        input_text="overwrite\n",
        timeout=30,
    )

    assert returncode == 0, (
        "the wrapper captures the helper status instead of propagating "
        f"it; rc={returncode}\npty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    helper_exit = _extract_helper_exit(pty_output)
    assert helper_exit != 0, (
        "the helper must propagate an interactive overwrite callback "
        f"failure; pty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    assert marker_file.read_text(encoding="utf-8") == "called\n", (
        "the failing callback must still run exactly once; marker "
        f"contents:\n{marker_file.read_text(encoding='utf-8')!r}"
    )
