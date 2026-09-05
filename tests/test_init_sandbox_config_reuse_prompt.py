"""Failing tests for bin/init-sandbox.sh config.env overwrite-vs-reuse (#352).

Current bug: ``bin/init-sandbox.sh`` never checks whether
``${BH_PROJECT_ROOT}/.bh/config.env`` already exists before writing it.
Confirmed via a black-box run of the real script (pre-seeding
``.bh/config.env`` with known content, no tty attached): execution
reaches the exact same ``baton-harness: writing sandbox config to
<path>/.bh/config.env ...`` stdout line whether or not the file was
already present, and only then fails on the (pre-existing, unrelated)
non-interactive-session guard. There is no existence check anywhere in
the sequence.

Target behavior (issue #352, item 1): before writing, the script must
check for an existing ``.bh/config.env`` and, when interactive, ask
overwrite-vs-reuse; the fresh-init (file-absent) path must be
unaffected. This suite pins the part of that contract observable
without a real tty:

  - ``test_existing_config_env_write_marker_suppressed_before_overwrite``
    (RED): with a pre-existing ``.bh/config.env``, the script must not
    reach the unconditional "writing sandbox config to" step -- an
    existence check must intervene first, either by silently reusing
    (no need to reach the write branch) or by asking a question that
    then fails closed for lack of a tty (a *new*, earlier failure than
    today's). Confirmed red: today's script reaches that write-marker
    regardless.
  - ``test_existing_config_env_bytes_unchanged_when_run_non_interactively``
    (regression companion, green today): whatever the exact failure
    path, the pre-existing file's bytes must never be touched by a run
    that cannot complete the write.
  - ``test_absent_config_env_regression_reaches_write_step`` (regression
    guard, green today): the ordinary fresh-init path (no pre-existing
    file) must still reach the same write step and fail only for the
    unrelated (pre-existing) non-interactive-session reason -- pinned
    so a future refactor of the new existence check cannot accidentally
    also break the fresh-init path.

These tests drive the real ``bin/init-sandbox.sh`` via subprocess with a
fake ``gh`` on PATH and isolated HOME/XDG_CONFIG_HOME, following the
established convention in ``tests/test_init_sandbox_branch_guard.py``
(git fixture helpers, gh stub script, and invocation shape duplicated
here rather than imported, matching that file's own self-contained
style and every other test module in this suite).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
INIT_SANDBOX = HARNESS / "bin" / "init-sandbox.sh"

# On Windows, the system bash (C:\Windows\System32\bash.exe) launches WSL
# and fails when no WSL distro is configured. Prefer Git Bash when
# available (same resolution as test_init_sandbox_branch_guard.py).
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""
_VENV_BIN_DIR = str(
    HARNESS / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
)

_GH_STUB_SCRIPT = """#!/usr/bin/env bash
# Minimal gh stub -- see test_init_sandbox_branch_guard.py for the full
# rationale. Handles exactly the calls init-sandbox.sh issues before
# the .bh/config.env write step under test.
set -euo pipefail

if [[ "$1" == "auth" && "$2" == "status" ]]; then
    exit 0
fi
if [[ "$1" == "auth" && "$2" == "setup-git" ]]; then
    exit 0
fi
if [[ "$1" == "api" && "$2" == repos/*/labels/* ]]; then
    echo "HTTP/1.1 200 OK"
    exit 0
fi
if [[ "$1" == "repo" && "$2" == "view" ]]; then
    echo "${FAKE_GH_DEFAULT_BRANCH:-main}"
    exit 0
fi

echo "fake-gh: unexpected invocation: $*" >&2
exit 1
"""


def _git_env() -> dict[str, str]:
    """Build a git-invocation environment with a stable commit identity.

    Returns:
        A copy of the current environment plus deterministic
        ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` values and
        ``GIT_CONFIG_NOSYSTEM=1``, so these throwaway fixture repos
        never depend on (or are perturbed by) the host machine's git
        identity or system-wide git config.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test-author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "test-author@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command against ``cwd`` and require it to succeed.

    Args:
        cwd: The repository directory to run the command against.
        *args: Arguments passed to ``git`` after the executable name.

    Returns:
        The completed process.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Build a bare 'origin' repo with one commit on main, plus a clone.

    Args:
        tmp_path: Pytest-provided temp directory for the test.

    Returns:
        A tuple of ``(origin_bare_path, project_root_clone_path)``.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    project_root = tmp_path / "project"
    subprocess.run(
        ["git", "clone", str(origin), str(project_root)],
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    (project_root / "base.txt").write_text(
        "v0\n", encoding="utf-8", newline="\n"
    )
    _git(project_root, "add", "base.txt")
    _git(project_root, "commit", "-m", "initial commit")
    _git(project_root, "branch", "-M", "main")
    _git(project_root, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, project_root


def _write_gh_stub(bin_dir: Path) -> None:
    """Write the fake ``gh`` executable described by ``_GH_STUB_SCRIPT``.

    Args:
        bin_dir: Directory to create and place the ``gh`` stub in.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(_GH_STUB_SCRIPT, encoding="utf-8", newline="\n")
    gh_stub.chmod(0o755)


def _run_init_sandbox(
    tmp_path: Path, project_root: Path, *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the real ``init-sandbox.sh`` non-interactively.

    ``stdin`` is explicitly ``DEVNULL`` (never inherited) so the run can
    never block waiting on a real tty regardless of how pytest itself
    was invoked.

    Args:
        tmp_path: Pytest-provided temp directory for the test.
        project_root: The local clone to point ``BH_PROJECT_ROOT`` at.
        input_text: Optional answers for a real POSIX pseudo-terminal.

    Returns:
        The completed ``init-sandbox.sh`` process, including captured
        output.
    """
    gh_bin_dir = tmp_path / "gh_bin"
    _write_gh_stub(gh_bin_dir)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    xdg_config_home = tmp_path / "xdg_config"
    xdg_config_home.mkdir(exist_ok=True)

    env = _git_env()
    env.pop("BH_REPO_OWNER", None)
    env.pop("BH_REPO_NAME", None)
    env.pop("BH_PROJECT_ROOT", None)
    env.pop("BH_SCENARIO", None)
    env["PATH"] = os.pathsep.join(
        part
        for part in [
            gh_bin_dir.as_posix(),
            _BASH_BIN_DIR,
            _VENV_BIN_DIR,
            env.get("PATH", ""),
        ]
        if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    env["BH_PROJECT_ROOT"] = project_root.as_posix()
    env["BH_REPO_OWNER"] = "fake-owner"
    env["BH_REPO_NAME"] = "fake-repo"
    env["BH_SCENARIO"] = "recovery"
    env["FAKE_GH_DEFAULT_BRANCH"] = "main"
    env.pop("BH_SETUP_NO_PROMPT", None)

    if input_text is not None:
        from tests._bh_pty import run_interactive

        rc, stdout, stderr = run_interactive(
            [_BASH, str(INIT_SANDBOX)], env, input_text=input_text, timeout=60
        )
        return subprocess.CompletedProcess([], rc, stdout, stderr)

    return subprocess.run(
        [_BASH, str(INIT_SANDBOX)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# RED: existence check must run before the unconditional write
# ---------------------------------------------------------------------------


def test_existing_config_env_write_marker_suppressed_before_overwrite(
    tmp_path: Path,
) -> None:
    """A pre-existing config.env must not reach the unconditional write step.

    Black-box confirmed today: with no existence check at all, the
    script reaches "baton-harness: writing sandbox config to ..."
    regardless of whether ``.bh/config.env`` already has content, and
    only fails afterward because this subprocess has no tty. Once an
    existence check is added ahead of the write (issue #352 item 1),
    that exact marker must no longer appear on this path -- either
    because the run silently reuses the existing file (never reaching
    the write branch), or because it fails earlier at a *new*
    interactive-choice gate. Either way the unconditional overwrite
    marker must be gone.
    """
    _origin, project_root = _make_origin_and_clone(tmp_path)
    bh_dir = project_root / ".bh"
    bh_dir.mkdir()
    (bh_dir / "config.env").write_text(
        "BH_GITHUB_APP_ID=preexisting123\n", encoding="utf-8", newline="\n"
    )

    proc = _run_init_sandbox(tmp_path, project_root)

    # Confirm the run actually reached this far (proves the assertion
    # below is meaningful and not an artifact of an unrelated early
    # crash) -- the labels/ci-workflow steps that precede the
    # config.env step must have completed normally.
    assert "ci.yml committed and pushed to sandbox" in proc.stdout, (
        "expected the run to reach the config.env step normally; got\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "writing sandbox config to" not in proc.stdout, (
        "the script must check for an existing .bh/config.env BEFORE "
        "reaching the unconditional overwrite step -- issue #352 item "
        "1 requires this check to run ahead of the write regardless of "
        "the eventual choice (reuse or overwrite)\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_existing_config_env_bytes_unchanged_when_run_non_interactively(
    tmp_path: Path,
) -> None:
    """A pre-existing config.env's bytes must survive a non-interactive run.

    Regression companion to the RED test above: passes today (the
    current unconditional-write attempt itself fails closed for lack of
    a tty before ever touching the file), and must keep passing once
    the new existence check lands.
    """
    _origin, project_root = _make_origin_and_clone(tmp_path)
    bh_dir = project_root / ".bh"
    bh_dir.mkdir()
    original_bytes = b"BH_GITHUB_APP_ID=preexisting123\n"
    (bh_dir / "config.env").write_bytes(original_bytes)

    proc = _run_init_sandbox(tmp_path, project_root)

    assert (bh_dir / "config.env").read_bytes() == original_bytes, (
        "a non-interactive run must never modify a pre-existing "
        "config.env it cannot obtain a choice for\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Regression guard: fresh-init (file-absent) path must be unaffected
# ---------------------------------------------------------------------------


def test_absent_config_env_regression_reaches_write_step(
    tmp_path: Path,
) -> None:
    """No pre-existing config.env: the fresh-init path must be unchanged.

    Pins today's fresh-init behavior so a future implementation of the
    new existence check cannot accidentally also break the ordinary
    no-prior-config case: the run must still reach the unconditional
    write step and fail only because this subprocess has no tty (the
    pre-existing, unrelated non-interactive-session guard) -- not
    because of some new and unrelated failure introduced by the
    existence-check refactor.
    """
    _origin, project_root = _make_origin_and_clone(tmp_path)
    # Deliberately no .bh/config.env under project_root.

    proc = _run_init_sandbox(tmp_path, project_root)

    assert "writing sandbox config to" in proc.stdout, (
        "the fresh-init (file-absent) path must still reach the "
        "config.env write step unchanged\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert proc.returncode == 1
    assert "interactive prompts required" in proc.stderr, (
        "the fresh-init path's failure reason must remain the "
        "existing non-interactive-session guard, unchanged by the new "
        "existence-check logic\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _new_provider_config(tmp_path: Path, answers: str) -> tuple[str, str]:
    """Run fresh initialization and return its config and prompts.

    Args:
        tmp_path: Isolated fixture directory.
        answers: Provider/source answers, followed by optional secret IDs.

    Returns:
        The persisted config and prompt/error output.
    """
    _origin, project = _make_origin_and_clone(tmp_path)
    result = _run_init_sandbox(
        tmp_path, project, input_text="111\n999999\n" + answers
    )
    assert result.returncode == 0, result.stderr
    return (project / ".bh/config.env").read_text(), result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_new_bws_config_writes_explicit_provider_and_only_bws_source(
    tmp_path: Path,
) -> None:
    """The BWS choice persists only its selector and secret UUID."""
    config, prompts = _new_provider_config(
        tmp_path, "bws\n11111111-1111-1111-1111-111111111111\n\n\n"
    )
    assert "export BH_GITHUB_APP_KEY_PROVIDER=bws\n" in config
    assert (
        "export BWS_PEM_SECRET_ID='11111111-1111-1111-1111-111111111111'\n"
        in config
    )
    assert "BH_GITHUB_APP_PRIVATE_KEY_FILE" not in config + prompts


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_bws_provider_reprompts_empty_and_invalid_secret_ids(
    tmp_path: Path,
) -> None:
    """Only a complete UUID can become the selected BWS PEM locator."""
    config, prompts = _new_provider_config(
        tmp_path,
        "bws\n\nnot-a-uuid\n"
        "11111111-1111-1111-1111-111111111111\n\n\n",
    )
    assert prompts.count("valid UUID") == 2
    assert "not-a-uuid" not in config
    assert (
        "export BWS_PEM_SECRET_ID='11111111-1111-1111-1111-111111111111'\n"
        in config
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_bws_config_sources_uuid_without_command_execution(
    tmp_path: Path,
) -> None:
    """A metacharacter-bearing BWS locator is rejected before persistence."""
    marker = tmp_path / "bws-injection-marker"
    malicious_id = (
        "11111111-1111-1111-1111-111111111111; touch "
        f"{marker.as_posix()}"
    )
    accepted_id = "22222222-2222-2222-2222-222222222222"
    _origin, project = _make_origin_and_clone(tmp_path)
    result = _run_init_sandbox(
        tmp_path,
        project,
        input_text=(
            "111\n999999\nbws\n"
            f"{malicious_id}\n{accepted_id}\n\n\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "valid UUID" in result.stderr

    source_result = subprocess.run(
        [
            _BASH,
            "-c",
            'source "$1"; printf "%s" "$BWS_PEM_SECRET_ID"',
            "bash",
            str(project / ".bh/config.env"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert source_result.returncode == 0, source_result.stderr
    assert source_result.stdout == accepted_id
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_bws_provider_read_eof_fails_closed_without_config(
    tmp_path: Path,
) -> None:
    """EOF at the required BWS locator prompt cannot write partial config."""
    _origin, project = _make_origin_and_clone(tmp_path)
    result = _run_init_sandbox(
        tmp_path,
        project,
        input_text="111\n999999\nbws\n\x04",
    )
    assert result.returncode != 0
    assert "could not read BWS PEM secret UUID" in result.stderr
    assert not (project / ".bh/config.env").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_new_file_config_writes_explicit_provider_and_only_absolute_file_source(  # noqa: E501
    tmp_path: Path,
) -> None:
    """The file choice persists a locator without requesting a vault source."""
    config, prompts = _new_provider_config(
        tmp_path, "file\n/run/credentials/app.pem\n\n\n"
    )
    assert "export BH_GITHUB_APP_KEY_PROVIDER=file\n" in config
    assert (
        "export BH_GITHUB_APP_PRIVATE_KEY_FILE='/run/credentials/app.pem'\n"
        in config
    )
    assert "BWS_PEM_SECRET_ID" not in config + prompts


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_file_provider_reprompts_relative_path(tmp_path: Path) -> None:
    """An invalid relative locator is rejected before the config is written."""
    config, prompts = _new_provider_config(
        tmp_path, "file\nrelative.pem\n/run/credentials/app.pem\n\n\n"
    )
    assert "absolute path" in prompts
    assert "relative.pem" not in config
    assert (
        "BH_GITHUB_APP_PRIVATE_KEY_FILE='/run/credentials/app.pem'\n"
        in config
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_file_provider_reprompts_path_containing_single_quote(
    tmp_path: Path,
) -> None:
    """A path that cannot be safely single-quoted is never persisted."""
    config, prompts = _new_provider_config(
        tmp_path,
        "file\n/run/credentials/app'unsafe.pem\n"
        "/run/credentials/app.pem\n\n\n",
    )
    assert "single quote" in prompts
    assert "app'unsafe.pem" not in config
    assert (
        "BH_GITHUB_APP_PRIVATE_KEY_FILE='/run/credentials/app.pem'\n"
        in config
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_file_config_sources_literal_path_without_command_execution(
    tmp_path: Path,
) -> None:
    """Sourcing generated config preserves metacharacters without execution."""
    marker = tmp_path / "injection-marker"
    private_key_path = f"/run/app.pem; touch {marker.as_posix()}"
    _origin, project = _make_origin_and_clone(tmp_path)
    result = _run_init_sandbox(
        tmp_path,
        project,
        input_text=(
            "111\n999999\nfile\n"
            f"{private_key_path}\n"
            "\n\n"
        ),
    )
    assert result.returncode == 0, result.stderr

    source_result = subprocess.run(
        [
            _BASH,
            "-c",
            'source "$1"; printf "%s" "$BH_GITHUB_APP_PRIVATE_KEY_FILE"',
            "bash",
            str(project / ".bh/config.env"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert source_result.returncode == 0, source_result.stderr
    assert source_result.stdout == private_key_path
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_unknown_provider_reprompts_without_writing_config(
    tmp_path: Path,
) -> None:
    """An unknown provider does not become a persisted source or selector."""
    config, prompts = _new_provider_config(
        tmp_path, "vault\nfile\n/run/credentials/app.pem\n\n\n"
    )
    assert "bws or file" in prompts
    assert "vault" not in config
    assert "BH_GITHUB_APP_KEY_PROVIDER=file\n" in config


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty required")
def test_generated_config_never_contains_pem_contents(tmp_path: Path) -> None:
    """Initialization accepts only the path; extra PEM input is not copied."""
    config, _prompts = _new_provider_config(
        tmp_path,
        "file\n/run/credentials/app.pem\n\n\n-----BEGIN PRIVATE KEY-----\n",
    )
    assert "BH_GITHUB_APP_KEY_PROVIDER=file\n" in config
    assert "PRIVATE KEY" not in config
