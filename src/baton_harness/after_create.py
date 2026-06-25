"""Hook: after_create — per-worktree dependency setup.

Invoked by Baton immediately after a new worktree is created (before the
agent's first run).  Detects the project type from files present in the
worktree (``package.json``, ``requirements.txt``, ``pyproject.toml``) and
installs dependencies using the appropriate tool.

This is a **partial** mitigation for worktree-isolation limits (S2.4 in the
architecture spec): it handles dependency installation only.  Shared
ports/services that cannot be replicated per-worktree are outside scope.

Entry point: ``bh-after-create`` (defined in ``pyproject.toml``).

WORKFLOW.md hook line (issue #5)::

    after_create: bh-after-create

Context:
    The hook runs with ``$PWD`` set to the newly created worktree directory.
    The issue number is inferred from ``basename($PWD)`` via
    ``baton_harness._cli.resolve_issue_number`` (spike finding F2: Baton
    passes no env-var context to hooks).
    Baton names worktrees ``<repo>/.symphony/worktrees/<issue>`` (a bare
    integer); the harness's own convention is ``<repo>/.worktrees/<branch>``
    (``<prefix>-<issue>[-<slug>]``).  Both forms are accepted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from baton_harness._cli import (
    claude_settings_json_for_worktree,
    err,
    log,
    resolve_issue_number,
)

#: Short name used in log/err prefixes.
_HOOK = "after-create"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command and return its CompletedProcess.

    Streams stdout/stderr to the terminal so Baton's log captures tool
    output in real time.  Always uses ``encoding="utf-8"`` to avoid
    Windows cp1252 mangling of non-ASCII output.

    Args:
        cmd: The command and arguments to execute.

    Returns:
        The :class:`subprocess.CompletedProcess` result with ``returncode``
        populated.
    """
    return subprocess.run(
        cmd,
        encoding="utf-8",
        check=False,
    )


def _install_npm(issue: int, cwd: Path) -> int:
    """Install Node.js dependencies in *cwd*.

    Uses ``npm ci`` when ``package-lock.json`` is present (reproducible
    install); falls back to ``npm install`` otherwise.

    Args:
        issue: GitHub issue number for log prefixes.
        cwd: Worktree directory (should be ``Path.cwd()``).

    Returns:
        ``0`` on success, non-zero on failure.
    """
    if (cwd / "package-lock.json").exists():
        cmd = ["npm", "ci"]
    else:
        cmd = ["npm", "install"]

    log(_HOOK, issue, f"running {' '.join(cmd)}")
    result = _run(cmd)
    if result.returncode != 0:
        err(_HOOK, issue, f"{' '.join(cmd)} failed (exit {result.returncode})")
    return result.returncode


def _install_requirements(issue: int) -> int:
    """Install Python dependencies from ``requirements.txt``.

    Prefers ``uv pip install`` when ``uv`` is available on ``PATH``; falls
    back to plain ``pip install`` otherwise.

    Args:
        issue: GitHub issue number for log prefixes.

    Returns:
        ``0`` on success, non-zero on failure.
    """
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "-r", "requirements.txt"]
    else:
        cmd = ["pip", "install", "-r", "requirements.txt"]

    log(_HOOK, issue, f"running {' '.join(cmd)}")
    result = _run(cmd)
    if result.returncode != 0:
        err(_HOOK, issue, f"{' '.join(cmd)} failed (exit {result.returncode})")
    return result.returncode


def _install_pyproject(issue: int) -> int:
    """Install the package declared in ``pyproject.toml`` in editable mode.

    Tries ``pip install -e '.[dev]'`` first to include dev extras.  If that
    fails (e.g. the project declares no ``[dev]`` extra), retries with the
    bare ``pip install -e .`` form.

    Args:
        issue: GitHub issue number for log prefixes.

    Returns:
        ``0`` on success, non-zero when both install attempts fail.
    """
    dev_cmd = ["pip", "install", "-e", ".[dev]"]
    log(_HOOK, issue, f"running {' '.join(dev_cmd)}")
    result = _run(dev_cmd)

    if result.returncode == 0:
        return 0

    log(
        _HOOK,
        issue,
        ".[dev] extra absent or install failed — retrying without extra",
    )
    bare_cmd = ["pip", "install", "-e", "."]
    log(_HOOK, issue, f"running {' '.join(bare_cmd)}")
    result = _run(bare_cmd)
    if result.returncode != 0:
        err(
            _HOOK,
            issue,
            f"{' '.join(bare_cmd)} failed (exit {result.returncode})",
        )
    return result.returncode


_EXCLUDE_LINE = ".claude/settings.json"

#: Return code from ``_register_git_exclude`` when ``.git`` is absent.
#: Distinct from 1 (OSError) so callers can choose whether to treat it
#: as fatal or degraded.
_RC_NO_GIT = 2


def _register_git_exclude(issue: int, cwd: Path) -> int:
    """Add ``.claude/settings.json`` to the git exclude file in *cwd*.

    The per-checkout exclude file (``info/exclude`` under the git dir)
    is never committed.  Adding the settings path here prevents
    ``git add -A`` from staging the harness-injected file into the
    target repo.

    Works correctly for both plain repos (where ``.git`` is a
    directory) and linked worktrees created by ``git worktree add``
    (where ``.git`` is a pointer file).  The actual git dir is
    resolved via ``git rev-parse --git-path info/exclude`` so that
    the real ``info/exclude`` path is always used regardless of
    worktree topology.

    Idempotent: the line is only appended if not already present.

    Args:
        issue: Issue number (used in log prefix).
        cwd: The freshly-created worktree directory.

    Returns:
        ``0`` on success; ``_RC_NO_GIT`` (2) when *cwd* is not inside
        a git worktree (``git rev-parse`` fails); ``1`` on an OSError
        writing the file.

    Raises:
        Nothing — all errors surface via :func:`err` or :func:`log`
        and a non-zero return code.
    """
    # Resolve the real exclude path via git so that linked worktrees
    # (where .git is a pointer file, not a directory) are handled
    # correctly.  For plain repos this returns a relative path like
    # ".git/info/exclude"; for linked worktrees it returns the
    # absolute path under <main>/.git/worktrees/<name>/info/exclude.
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        err(
            _HOOK,
            issue,
            f"cwd {cwd} is not inside a git worktree "
            "(git rev-parse --git-path failed) — "
            "cannot register git exclude entry",
        )
        return _RC_NO_GIT

    raw_path = result.stdout.strip()
    # git may return a relative or absolute path depending on version.
    if Path(raw_path).is_absolute():
        exclude_path = Path(raw_path)
    else:
        exclude_path = (cwd / raw_path).resolve()

    info_dir = exclude_path.parent
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        # Read existing content to check idempotency.
        existing = ""
        if exclude_path.exists():
            existing = exclude_path.read_text(encoding="utf-8")
        # Check each line for an exact match (strip trailing whitespace).
        lines = [ln.rstrip() for ln in existing.splitlines()]
        if _EXCLUDE_LINE in lines:
            log(
                _HOOK,
                issue,
                f"git exclude already contains '{_EXCLUDE_LINE}' — skip",
            )
            return 0
        # Ensure file ends with a newline before appending.
        if existing and not existing.endswith("\n"):
            existing += "\n"
        with open(exclude_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(_EXCLUDE_LINE + "\n")
    except OSError as exc:
        err(
            _HOOK,
            issue,
            f"failed to update git exclude file: {exc}",
        )
        return 1

    log(
        _HOOK,
        issue,
        f"registered '{_EXCLUDE_LINE}' in git exclude ({exclude_path})",
    )
    return 0


def _write_claude_settings(issue: int, cwd: Path, venv_root: Path) -> int:
    """Drop a per-worktree .claude/settings.json.

    Registers the force-pr-not-merge PreToolUse hook.  Before writing,
    attempts to add ``.claude/settings.json`` to the git exclude file
    so that ``git add -A`` will not stage the harness-local file into
    the target repo.  If *cwd* is not inside a git worktree the exclude
    step is skipped with a warning (degraded mode — the file is still
    written; callers in real worktrees always have a ``.git``).  A hard
    OSError writing the exclude file is fatal and propagates as ``1``.

    If ``.claude/settings.json`` is **tracked** by git in the target
    repo, the hook refuses to overwrite it and returns ``1`` (FATAL).
    The git exclude entry only protects untracked files, so overwriting
    a tracked file would cause ``git add -A`` to stage the harness
    payload as a modification, potentially deleting the repo's real
    Claude configuration from the PR.  Running a worker without the
    PreToolUse tripwire is worse than refusing startup; the operator
    must resolve the tracked-file conflict explicitly.

    If ``.claude/settings.json`` is **untracked but already present**,
    it is backed up to ``.claude/settings.json.bh-backup`` before being
    overwritten so the operator's local-only settings are preserved.

    Args:
        issue: Issue number (used in log prefix).
        cwd: The freshly-created worktree directory.
        venv_root: Absolute path to the harness venv.

    Returns:
        ``0`` on success; ``1`` on filesystem error or when the target
        repo has a tracked ``.claude/settings.json``.
    """
    # Step 1: register in git exclude BEFORE writing the file so that
    # even a crash between step 1 and step 2 leaves git ignoring the
    # path.  Missing git worktree → degraded mode (warn, continue).
    rc = _register_git_exclude(issue, cwd)
    if rc == 1:
        # Hard OSError — propagate.
        return 1
    # rc == _RC_NO_GIT (2) → degraded; rc == 0 → exclude registered.

    out_dir = cwd / ".claude"
    out_path = out_dir / "settings.json"
    backup_path = out_dir / "settings.json.bh-backup"

    try:
        out_dir.mkdir(exist_ok=True)

        # Step 2: detect tracked .claude/settings.json — FATAL if
        # present.  The git exclude entry only prevents staging of
        # *untracked* files; if the file is already tracked, git add -A
        # would stage our overwrite as a modification, clobbering the
        # target repo's real Claude configuration in the PR.
        ls_result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".claude/settings.json"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if ls_result.returncode == 0:
            err(
                _HOOK,
                issue,
                "FATAL: target repo has a tracked .claude/settings.json — "
                "refusing to overwrite. Worker will NOT start. "
                "Operator must remove the tracked file "
                "(`git rm .claude/settings.json`) or merge the harness "
                "hook config into the existing file manually.",
            )
            return 1

        # Step 3: backup any untracked pre-existing settings file so
        # the operator's local-only Claude config is not silently lost.
        if out_path.exists():
            shutil.copy2(str(out_path), str(backup_path))
            log(
                _HOOK,
                issue,
                "backed up existing .claude/settings.json to "
                ".claude/settings.json.bh-backup",
            )
            print(
                f"[{_HOOK}] WARNING: target repo has an untracked "
                ".claude/settings.json; backed up to "
                ".claude/settings.json.bh-backup before injecting "
                "harness settings.  Restore manually after run "
                "or add bh-after-run cleanup if needed.",
                file=sys.stderr,
                flush=True,
            )

        # Step 4: write harness settings.
        settings = claude_settings_json_for_worktree(venv_root)
        out_path.write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        err(_HOOK, issue, f"failed to write .claude/settings.json: {exc}")
        return 1

    log(_HOOK, issue, f"wrote {out_path}")
    return 0


def _write_claude_settings_if_configured(issue: int, cwd: Path) -> int:
    """Drop .claude/settings.json or FAIL LOUDLY if BH_VENV is absent (C4).

    Args:
        issue: Issue number (used in log prefix).
        cwd: The freshly-created worktree directory.

    Returns:
        ``0`` on success; non-zero on misconfiguration or write failure.
        Specifically: BH_VENV absent returns ``1`` — a worker without the
        force-pr-not-merge hook would silently lose defense-in-depth, and
        the operator MUST notice at first worktree creation rather than at
        first merge attempt.
    """
    venv_root_env = os.environ.get("BH_VENV")
    if not venv_root_env:
        err(
            _HOOK,
            issue,
            "BH_VENV not set — refusing to create worktree without the "
            "force-pr-not-merge PreToolUse hook. Set BH_VENV in the "
            "daemon environment (bin/run-daemon.sh:L65-L66 normally "
            "exports it) and re-run.",
        )
        return 1
    return _write_claude_settings(
        issue=issue, cwd=cwd, venv_root=Path(venv_root_env)
    )


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    """Entry point for the ``bh-after-create`` console script.

    Detects the project type from files present in the current working
    directory and runs the appropriate dependency-install command.  Logs
    each action via :func:`baton_harness._cli.log`.

    Args:
        argv: Unused; accepted for interface symmetry with other hooks.

    Returns:
        ``0`` on success or when no project files are found; ``1`` when the
        issue number cannot be resolved; non-zero (propagated from the
        install command) on install failure; non-zero (``1``) when
        ``BH_VENV`` is absent or unset (C4 fatal: workers without the
        force-pr-not-merge hook lose defense-in-depth).
    """
    issue = resolve_issue_number()
    if issue is None:
        print(
            f"[{_HOOK}] error: could not derive issue number from cwd — "
            "expected a bare integer (Baton: .symphony/worktrees/<issue>) "
            "or <prefix>-<issue>[-<slug>] (harness: .worktrees/<branch>)",
            file=sys.stderr,
            flush=True,
        )
        return 1

    cwd = Path.cwd()

    if (cwd / "package.json").exists():
        rc_install = _install_npm(issue, cwd)
    elif (cwd / "requirements.txt").exists():
        rc_install = _install_requirements(issue)
    elif (cwd / "pyproject.toml").exists():
        rc_install = _install_pyproject(issue)
    else:
        log(
            _HOOK,
            issue,
            "no recognised project files found "
            "(package.json / requirements.txt / pyproject.toml) — skipping",
        )
        rc_install = 0

    if rc_install != 0:
        return rc_install

    # Slice 3b — install the force-pr-not-merge PreToolUse hook so any
    # worker-side `gh pr merge` is stopped before the ruleset would have
    # denied it at the API layer. BH_VENV absence is FATAL (C4) — a
    # silent skip would ship workers without defense-in-depth.
    rc_settings = _write_claude_settings_if_configured(issue=issue, cwd=cwd)
    if rc_settings != 0:
        return rc_settings

    return 0


if __name__ == "__main__":
    sys.exit(main())
