"""Regression coverage for install-daemon-service.sh secrets reuse (#355).

``bin/install-daemon-service.sh`` uses the shared
``_bh_resolve_config_with_reuse_prompt`` helper from
``bin/lib/load-config.sh`` to offer an overwrite-vs-reuse choice when
its secrets file already exists. ``BH_DAEMON_SECRETS_PATH`` overrides
the default ``/etc/bh-daemon/secrets.env`` location throughout the
installer, including the existence check, backup target, install
destination, and rendered systemd ``EnvironmentFile=`` entry. This
suite exercises that existing override against writable temporary
paths.

Why a plain PATH shim over ``sudo``/``install`` cannot substitute for
this: the reuse-vs-overwrite gate has to start with "does the config
file already exist", and ``_bh_resolve_config_with_reuse_prompt`` (an
already-tested shared helper this suite does not edit) answers that
question with a bare ``[[ ! -f "$1" ]]`` -- a shell builtin. Builtins
are resolved without ever touching ``$PATH``, so no fake ``sudo``/
``install``/``test`` executable can intercept it. Only an actual path
override lets a test point that check at a writable ``tmp_path``
location instead of the real, root-owned ``/etc`` tree. A fake ``sudo``
is still used below (see ``_SUDO_STUB``) as defense in depth for other
privileged commands and the unit-file write. It rewrites any
``/etc/``-rooted argument to live under a throwaway ``$FAKE_ROOT``, so
no scenario below can touch the real system ``/etc`` tree.

Assumed prompt ordering (this test author's design choice, since pty
input is fed as one up-front block per ``tests/_bh_pty.py``): the
reuse-vs-overwrite prompt runs inside the "Resolve BWS_ACCESS_TOKEN"
block, before the later "proceed with install? [y/N]" confirmation.
This remains an assumption rather than a public contract; an intentional
reordering would require the suite to be adjusted, per the same category
of known gap already flagged in
``tests/test_install_daemon_service_root_prompt_order.py``.

Non-interactive-existing-file convention (this test author's choice,
per the briefing): no new force-override flag is introduced for this
case. ``_bh_resolve_config_with_reuse_prompt`` already fails closed
unconditionally when the session is non-interactive and the file
exists -- there is no way to ask, so the operator must either run
interactively or delete the file first. Reusing that existing,
already-pinned behavior verbatim (rather than inventing a bespoke force
flag for this one script) keeps the four setup scripts consistent, per
issue #352's own shared-helper intent.

The scenarios verify the current behavior:

  - **overwrite** prompts for a new token, rewrites the secrets file,
    preserves the prior contents in a timestamped backup, and completes
    the installer successfully.
  - **reuse** skips the token prompt, leaves the secrets file untouched,
    and completes the installer successfully.
  - **EOF on the reuse prompt** fails closed with the shared helper's
    read-error message and leaves the existing file untouched.
  - **non-interactive with an existing file** fails closed because no
    overwrite-or-reuse choice can be obtained, leaving the file intact.
  - **no existing file** retains the original fresh-init token-resolution
    behavior without showing the reuse prompt.
  - **relative path override** fails validation before any prompt or file
    write because systemd requires an absolute ``EnvironmentFile=`` path.

Interactive cases are driven via a real pty (see ``tests/_bh_pty.py``)
and, per that helper's own POSIX-only constraint, are skipped (not
failed) on this Windows host; they run for real on the project's CI
runner (``ubuntu-latest`` per ``.github/workflows/ci.yml``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
SCRIPT = HARNESS / "bin" / "install-daemon-service.sh"

# On Windows, the system bash (C:\Windows\System32\bash.exe) launches WSL
# and fails when no WSL distro is configured. Prefer Git Bash when
# available (same resolution as every other bash-driving test in this
# suite).
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""

_TOKEN_PROMPT_TEXT = (
    "BWS_ACCESS_TOKEN (Bitwarden Secrets CLI machine-account token)"
)
_REUSE_PROMPT_TEXT = (
    "type 'overwrite' to replace it, or press Enter to reuse it"
)
_REUSE_EOF_ERROR_TEXT = "could not read overwrite-or-reuse choice"
_NONINTERACTIVE_EXISTING_ERROR_TEXT = (
    "requires an interactive overwrite-or-reuse choice"
)
_LEGACY_NONINTERACTIVE_TOKEN_ERROR_TEXT = (
    "BWS_ACCESS_TOKEN not set and session is non-interactive"
)

# Fake sudo (test-only): never escalates privilege. Rewrites any argument
# rooted at /etc/ to live under $FAKE_ROOT/etc/ instead, then execs the
# (now-rewritten) command directly. Defense in depth (see module
# docstring) for the script's *other* hardcoded /etc/... targets
# (/etc/bh-daemon, /etc/systemd/system/bh-daemon.service) regardless of
# whether BH_DAEMON_SECRETS_PATH is threaded through every call site.
_SUDO_STUB = """#!/usr/bin/env bash
set -euo pipefail
args=()
for a in "$@"; do
    case "$a" in
        /etc/*)
            args+=("${FAKE_ROOT}${a}")
            ;;
        *)
            args+=("$a")
            ;;
    esac
done
exec "${args[@]}"
"""

# Fake systemctl (test-only): this test host has no real bh-daemon unit
# to manage. Always succeed so the script's unconditional
# `sudo systemctl daemon-reload` (activate_service, gated by --no-start
# for everything past it) never fails the run under test.
_SYSTEMCTL_STUB = """#!/usr/bin/env bash
echo "fake-systemctl: $*" >&2
exit 0
"""


def _write_stub(bin_dir: Path, name: str, script: str) -> None:
    """Write one fake executable onto a PATH-stub directory.

    Args:
        bin_dir: Directory to create (if needed) and place the stub in.
        name: Executable name (no extension -- matches the existing
            ``gh`` stub convention elsewhere in this test suite).
        script: Full script body to write.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(script, encoding="utf-8", newline="\n")
    stub.chmod(0o755)


def _build_stub_bin_dir(tmp_path: Path) -> Path:
    """Build a PATH-stub dir with fake ``sudo`` and ``systemctl``.

    Args:
        tmp_path: Pytest-provided temp directory for this test.

    Returns:
        The stub bin directory, ready to be prepended onto ``PATH``.
    """
    bin_dir = tmp_path / "stub_bin"
    _write_stub(bin_dir, "sudo", _SUDO_STUB)
    _write_stub(bin_dir, "systemctl", _SYSTEMCTL_STUB)
    return bin_dir


def _build_harness_dir(tmp_path: Path) -> Path:
    """Build a fake harness checkout satisfying the script's own gates.

    Provides ``config/WORKFLOW.md`` and a ``bh-daemon`` binary under
    both venv layouts the script probes (POSIX ``bin/`` and Windows
    ``Scripts/``), so the fixture works regardless of which platform
    a non-pty test happens to run on.

    Args:
        tmp_path: Pytest-provided temp directory for this test.

    Returns:
        Path to the fake harness directory (pass via ``--harness-dir``).
    """
    harness = tmp_path / "harness"
    (harness / "config").mkdir(parents=True)
    (harness / "config" / "WORKFLOW.md").write_text(
        "workflow: fake\n", encoding="utf-8", newline="\n"
    )
    for layout in ("bin", "Scripts"):
        venv_bin = harness / ".venv" / layout
        venv_bin.mkdir(parents=True)
        daemon_bin = venv_bin / "bh-daemon"
        daemon_bin.write_text(
            "#!/usr/bin/env bash\necho fake-bh-daemon\n",
            encoding="utf-8",
            newline="\n",
        )
        daemon_bin.chmod(0o755)
    return harness


def _base_env(
    tmp_path: Path, fake_root: Path, stub_bin_dir: Path
) -> dict[str, str]:
    """Build an isolated environment for driving the real script.

    Args:
        tmp_path: Pytest-provided temp directory for this test, used to
            build an isolated HOME/XDG_CONFIG_HOME so no real operator
            host.env on this machine can leak into a run.
        fake_root: Directory the fake ``sudo`` rewrites ``/etc/...``
            targets under (``$FAKE_ROOT``).
        stub_bin_dir: Directory holding the fake ``sudo``/``systemctl``
            executables; prepended onto ``PATH``.

    Returns:
        A fresh environment dict with credential-shaped and
        session-mode vars scrubbed, per the two footguns this script's
        own guards are sensitive to: an inherited ``ANTHROPIC_API_KEY``
        aborts the script before any resolution runs at all, and an
        inherited ``BWS_ACCESS_TOKEN`` silently skips every prompt this
        suite means to exercise.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    xdg_config_home = tmp_path / "xdg_config"
    xdg_config_home.mkdir(exist_ok=True)

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "BH_SETUP_NO_PROMPT",
            "BH_PROJECT_ROOT",
            "BH_DEBUG_CONFIG",
            "ANTHROPIC_API_KEY",
            "BWS_ACCESS_TOKEN",
            "BH_DAEMON_SECRETS_PATH",
        )
    }
    env["PATH"] = os.pathsep.join(
        part
        for part in [
            stub_bin_dir.as_posix(),
            _BASH_BIN_DIR,
            env.get("PATH", ""),
        ]
        if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    env["FAKE_ROOT"] = fake_root.as_posix()
    return env


def _argv(harness_dir: Path, project_root: Path) -> list[str]:
    """Build the common argv for driving the real script under test.

    Args:
        harness_dir: Fake harness checkout (``--harness-dir``).
        project_root: Fake sandbox clone root (``--project-root``).

    Returns:
        Argv list, always including ``--no-start`` and an explicit
        ``--user`` (avoids the whitespace-reject guard tripping on
        whatever ``whoami`` happens to resolve to on the test host) and
        ``--print-unit`` deliberately omitted (this suite needs the
        real privileged-write path to run).
    """
    return [
        _BASH,
        str(SCRIPT),
        "--no-start",
        "--harness-dir",
        str(harness_dir),
        "--project-root",
        str(project_root),
        "--user",
        "testuser",
    ]


# ---------------------------------------------------------------------------
# Requirement 3: no existing secrets.env -> callback always runs
# unconditionally, no reuse prompt shown (green-today regression guard,
# matching the precedent in test_init_sandbox_config_reuse_prompt.py).
# ---------------------------------------------------------------------------


def test_absent_secrets_file_reaches_unchanged_token_prompt_noninteractively(
    tmp_path: Path,
) -> None:
    """No pre-existing secrets.env -> the fresh-init path is unaffected.

    Driven non-interactively (BH_SETUP_NO_PROMPT=1, no BWS_ACCESS_TOKEN)
    so this test is portable across platforms with no pty needed: with
    no existing file, the reuse feature must never intervene, so the
    script must still reach its existing, unchanged non-interactive
    BWS_ACCESS_TOKEN failure -- not some new reuse-related failure.

    Regression coverage: with ``BH_DAEMON_SECRETS_PATH`` pointed at a
    location that has no file yet, the reuse-vs-overwrite existence
    check in ``_bh_resolve_config_with_reuse_prompt`` finds nothing to
    reuse and steps aside, leaving the script's existing fresh-init
    token-resolution path to run exactly as before.
    """
    harness_dir = _build_harness_dir(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_root = tmp_path / "fake_root"
    stub_bin_dir = _build_stub_bin_dir(tmp_path)
    secrets_path = tmp_path / "secrets" / "secrets.env"

    env = _base_env(tmp_path, fake_root, stub_bin_dir)
    env["BH_SETUP_NO_PROMPT"] = "1"
    env["BH_DAEMON_SECRETS_PATH"] = secrets_path.as_posix()

    proc = subprocess.run(
        _argv(harness_dir, project_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    assert proc.returncode == 1, (
        "expected the existing non-interactive BWS_ACCESS_TOKEN failure "
        f"to still fire when no secrets.env exists yet; rc="
        f"{proc.returncode}\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert _LEGACY_NONINTERACTIVE_TOKEN_ERROR_TEXT in proc.stderr, (
        "the fresh-init (file-absent) path's failure reason must "
        "remain the existing non-interactive-session token guard, "
        f"unchanged by the new reuse-vs-overwrite logic\nstdout:\n"
        f"{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _REUSE_PROMPT_TEXT not in proc.stderr, (
        "no reuse-vs-overwrite gate should ever fire when there is no "
        f"pre-existing secrets.env to reuse\nstderr:\n{proc.stderr}"
    )
    assert not secrets_path.exists(), (
        "a failed non-interactive run must never create secrets.env\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Requirement 4: existing secrets.env + non-interactive + no override ->
# hard failure, file untouched.
# ---------------------------------------------------------------------------


def test_existing_secrets_file_noninteractive_fails_closed_untouched(
    tmp_path: Path,
) -> None:
    """A pre-existing secrets.env + no way to ask must fail closed.

    Per this suite's documented convention (module docstring): no new
    force-override flag is introduced for this case -- the operator
    must run interactively, or delete the file first, matching the
    already-pinned ``_bh_resolve_config_with_reuse_prompt`` contract
    verbatim.
    """
    harness_dir = _build_harness_dir(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_root = tmp_path / "fake_root"
    stub_bin_dir = _build_stub_bin_dir(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_path = secrets_dir / "secrets.env"
    original_bytes = b"BWS_ACCESS_TOKEN=original-preexisting-token\n"
    secrets_path.write_bytes(original_bytes)

    env = _base_env(tmp_path, fake_root, stub_bin_dir)
    env["BH_SETUP_NO_PROMPT"] = "1"
    env["BH_DAEMON_SECRETS_PATH"] = secrets_path.as_posix()

    proc = subprocess.run(
        _argv(harness_dir, project_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    assert proc.returncode != 0, (
        "a non-interactive session with an existing secrets.env has no "
        "way to ask, and must fail closed rather than silently picking "
        f"a side\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _NONINTERACTIVE_EXISTING_ERROR_TEXT in proc.stderr, (
        "expected the shared reuse-prompt helper's own non-interactive "
        "fail-closed message for an existing config file\nstdout:\n"
        f"{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert secrets_path.read_bytes() == original_bytes, (
        "a non-interactive run must never modify a pre-existing "
        f"secrets.env it cannot obtain a choice for\nstdout:\n"
        f"{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Requirement 6: relative secrets.env override -> hard failure, no file.
# ---------------------------------------------------------------------------


def test_relative_secrets_path_fails_closed_without_writing(
    tmp_path: Path,
) -> None:
    """A relative BH_DAEMON_SECRETS_PATH must fail before any write."""
    harness_dir = _build_harness_dir(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_root = tmp_path / "fake_root"
    stub_bin_dir = _build_stub_bin_dir(tmp_path)

    env = _base_env(tmp_path, fake_root, stub_bin_dir)
    env["BH_SETUP_NO_PROMPT"] = "1"
    env["BH_DAEMON_SECRETS_PATH"] = "relative/secrets.env"

    proc = subprocess.run(
        _argv(harness_dir, project_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        cwd=tmp_path,
        timeout=30,
    )

    assert proc.returncode != 0, (
        "a relative BH_DAEMON_SECRETS_PATH must fail closed\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "BH_DAEMON_SECRETS_PATH must be an absolute path" in proc.stderr, (
        "expected a clear absolute-path validation error\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert not list(tmp_path.rglob("secrets.env")), (
        "a rejected relative path must not create a secrets.env anywhere "
        f"under the working directory\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Requirement 2: existing secrets.env + interactive + Enter (reuse) ->
# no re-prompt for BWS_ACCESS_TOKEN, file untouched.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the interactivity check and cannot "
        "run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_existing_secrets_file_interactive_reuse_skips_token_prompt(
    tmp_path: Path,
) -> None:
    """Choosing "reuse" interactively must never re-prompt for the token.

    Feeds an empty line (bare Enter, per the briefing's "operator
    presses Enter (reuse)") for the reuse choice, then "y" for the
    script's unchanged, later install confirm -- see the module
    docstring's "assumed prompt ordering" note.
    """
    from tests._bh_pty import run_interactive

    harness_dir = _build_harness_dir(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_root = tmp_path / "fake_root"
    stub_bin_dir = _build_stub_bin_dir(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_path = secrets_dir / "secrets.env"
    original_bytes = b"BWS_ACCESS_TOKEN=original-preexisting-token\n"
    secrets_path.write_bytes(original_bytes)

    env = _base_env(tmp_path, fake_root, stub_bin_dir)
    env.pop("BH_SETUP_NO_PROMPT", None)
    env["BH_DAEMON_SECRETS_PATH"] = secrets_path.as_posix()

    returncode, pty_output, stderr = run_interactive(
        _argv(harness_dir, project_root),
        env,
        input_text="\ny\n",
        timeout=30,
    )

    assert returncode == 0, (
        "choosing to reuse must complete the installer successfully; "
        f"rc={returncode}\npty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    assert _TOKEN_PROMPT_TEXT not in stderr, (
        "choosing to reuse must never re-prompt for BWS_ACCESS_TOKEN\n"
        f"rc={returncode}\npty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    assert secrets_path.read_bytes() == original_bytes, (
        "choosing to reuse must leave the existing secrets.env "
        f"untouched\nrc={returncode}\npty_output:\n{pty_output}\n"
        f"stderr:\n{stderr}"
    )
    assert _REUSE_PROMPT_TEXT in stderr, (
        "expected the shared reuse-prompt helper's prompt text on "
        f"stderr\nrc={returncode}\npty_output:\n{pty_output}\n"
        f"stderr:\n{stderr}"
    )


# ---------------------------------------------------------------------------
# Requirement 1: existing secrets.env + interactive + "overwrite" ->
# prompted for new token, file rewritten, backup created.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the interactivity check and cannot "
        "run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_existing_secrets_file_interactive_overwrite_rewrites_with_backup(
    tmp_path: Path,
) -> None:
    """Choosing "overwrite" interactively must re-prompt and rewrite.

    Feeds "overwrite" for the reuse choice, a new token for the
    (silent) BWS_ACCESS_TOKEN prompt, then "y" for the script's
    unchanged, later install confirm -- see the module docstring's
    "assumed prompt ordering" note. The full installer must complete
    successfully after rewriting the secrets file and its backup.
    """
    from tests._bh_pty import run_interactive

    harness_dir = _build_harness_dir(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_root = tmp_path / "fake_root"
    stub_bin_dir = _build_stub_bin_dir(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_path = secrets_dir / "secrets.env"
    original_bytes = b"BWS_ACCESS_TOKEN=original-preexisting-token\n"
    secrets_path.write_bytes(original_bytes)

    env = _base_env(tmp_path, fake_root, stub_bin_dir)
    env.pop("BH_SETUP_NO_PROMPT", None)
    env["BH_DAEMON_SECRETS_PATH"] = secrets_path.as_posix()

    new_token = "new-secret-token-999"
    returncode, pty_output, stderr = run_interactive(
        _argv(harness_dir, project_root),
        env,
        input_text=f"overwrite\n{new_token}\ny\n",
        timeout=30,
    )

    assert returncode == 0, (
        "choosing to overwrite must complete the installer successfully; "
        f"rc={returncode}\npty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    assert _TOKEN_PROMPT_TEXT in stderr, (
        "choosing to overwrite must (re-)prompt for a new "
        f"BWS_ACCESS_TOKEN\nrc={returncode}\npty_output:\n"
        f"{pty_output}\nstderr:\n{stderr}"
    )
    assert secrets_path.read_text(encoding="utf-8") == (
        f"BWS_ACCESS_TOKEN={new_token}\n"
    ), (
        "expected the secrets file to be rewritten with the freshly "
        f"supplied token\nrc={returncode}\npty_output:\n{pty_output}\n"
        f"stderr:\n{stderr}\nsecrets dir contents: "
        f"{sorted(p.name for p in secrets_dir.iterdir())}"
    )
    backups = sorted(secrets_dir.glob("secrets.env.bak.*"))
    assert backups, (
        "expected a timestamped backup of the pre-existing secrets.env "
        f"before overwriting it (matching _backup_if_exists)\n"
        f"rc={returncode}\npty_output:\n{pty_output}\nstderr:\n"
        f"{stderr}\nsecrets dir contents: "
        f"{sorted(p.name for p in secrets_dir.iterdir())}"
    )
    assert backups[0].read_bytes() == original_bytes, (
        "expected the backup to preserve the original content\n"
        f"rc={returncode}\npty_output:\n{pty_output}\nstderr:\n{stderr}"
    )


# ---------------------------------------------------------------------------
# Requirement 5: read EOF/failure on the overwrite-or-reuse prompt ->
# fails closed, file untouched.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Python's pty module is POSIX-only; this test requires a real "
        "tty on fd 0/1 to satisfy the interactivity check and cannot "
        "run on this platform. Executes on CI (ubuntu-latest)."
    ),
)
def test_existing_secrets_file_interactive_read_eof_fails_closed(
    tmp_path: Path,
) -> None:
    """EOF while reading the overwrite-or-reuse choice must fail closed.

    Matches the hardened behavior already pinned for the shared helper
    itself in ``tests/test_load_config_reuse_prompt_helper.py`` --
    ``test_existing_config_file_interactive_read_eof_fails_closed``.
    """
    from tests._bh_pty import run_interactive

    harness_dir = _build_harness_dir(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_root = tmp_path / "fake_root"
    stub_bin_dir = _build_stub_bin_dir(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_path = secrets_dir / "secrets.env"
    original_bytes = b"BWS_ACCESS_TOKEN=original-preexisting-token\n"
    secrets_path.write_bytes(original_bytes)

    env = _base_env(tmp_path, fake_root, stub_bin_dir)
    env.pop("BH_SETUP_NO_PROMPT", None)
    env["BH_DAEMON_SECRETS_PATH"] = secrets_path.as_posix()

    returncode, pty_output, stderr = run_interactive(
        _argv(harness_dir, project_root),
        env,
        input_text="\x04",
        timeout=30,
    )

    assert returncode != 0, (
        "an interactive session that cannot read the overwrite-or-"
        f"reuse choice must fail closed\npty_output:\n{pty_output}\n"
        f"stderr:\n{stderr}"
    )
    assert _REUSE_EOF_ERROR_TEXT in stderr, (
        "expected the shared reuse-prompt helper's own EOF error "
        f"message\npty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
    assert secrets_path.read_bytes() == original_bytes, (
        "an interactive read failure must never modify a pre-existing "
        f"secrets.env\npty_output:\n{pty_output}\nstderr:\n{stderr}"
    )
