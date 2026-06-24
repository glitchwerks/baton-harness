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


def _write_claude_settings(issue: int, cwd: Path, venv_root: Path) -> int:
    """Drop a per-worktree .claude/settings.json.

    Registers the force-pr-not-merge PreToolUse hook.

    Args:
        issue: Issue number (used in log prefix).
        cwd: The freshly-created worktree directory.
        venv_root: Absolute path to the harness venv.

    Returns:
        ``0`` on success; non-zero on filesystem error.
    """
    settings = claude_settings_json_for_worktree(venv_root)
    out_dir = cwd / ".claude"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "settings.json"
    try:
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
        install command) on install failure.
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
    rc_settings = _write_claude_settings_if_configured(
        issue=issue, cwd=cwd
    )
    if rc_settings != 0:
        return rc_settings

    return 0


if __name__ == "__main__":
    sys.exit(main())
