"""Startup reconciliation sweep for the baton-harness daemon.

Runs startup checks at daemon startup (issues #40, #108):

- **G3a** — GitHub token validation (fatal on failure).
- **G3b** — ``ANTHROPIC_API_KEY`` presence check (fatal on failure).
- **G3c** — OAuth credential-volume health-check (fatal on failure).
- **G2** — Ungraceful-prior-exit detection via a marker file.
- **G1** — Orphan ``claude`` process sweep (detect-only, non-fatal).

Each non-fatal check is independently ``try/except``-guarded so one
failure never aborts another or the daemon.  The two credential checks
(G3a/G3b) are intentionally fatal: on failure they emit a critical alert
then raise ``SystemExit(1)``.

The marker file path is::

    <project_root>/.baton-harness/daemon.alive

It is **written** at startup and **cleared** on graceful shutdown by the
caller (``run_daemon`` finally-block).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from baton_harness._auth import TokenValidationError, validate_github_token
from baton_harness.chain.escalation import alert

if TYPE_CHECKING:
    from baton_harness.chain.obs_config import ObsConfig  # noqa: F401
    from baton_harness.chain.registry import RepoConfig
    from baton_harness.chain.runlog import RunLog

_log = logging.getLogger(__name__)

# Marker filename — the literal string "daemon.alive" must appear in source
# (contract pinned by TestMarkerPathConstant).
_ALIVE_MARKER = "daemon.alive"

# OAuth credential file path — structural-only check (presence + readability).
# Tests monkeypatch this seam to avoid touching real credentials.
# Default: ~/.claude/.credentials.json (Claude Code OAuth volume).
_OAUTH_CRED_PATH: Path = Path.home() / ".claude" / ".credentials.json"


def _list_claude_procs() -> list[int]:
    """Return PIDs of any running ``claude -p`` processes.

    Uses POSIX ``pgrep -f 'claude -p'``.  On Windows (where ``pgrep`` is
    unavailable), raises ``FileNotFoundError`` — the caller suppresses this.

    Returns:
        A list of integer PIDs.  Empty list when none are found.

    Raises:
        FileNotFoundError: On Windows where ``pgrep`` is not installed.
        subprocess.SubprocessError: If the subprocess call fails unexpectedly.
    """
    result = subprocess.run(
        ["pgrep", "-f", "claude -p"],
        capture_output=True,
        text=True,
    )
    # pgrep exits 1 when no matches found (not an error).
    if result.returncode == 0:
        return [int(p) for p in result.stdout.split() if p.strip()]
    return []


async def reconcile_startup(
    repo_cfgs: list[RepoConfig],
    obs: ObsConfig | None,
    runlog: RunLog | None,
) -> None:
    """Run the startup reconciliation sweep.

    Executes the three startup checks in order.  Credential failures
    (G3a, G3b) are fatal: they emit a critical alert then call
    ``sys.exit(1)``.  All other checks are non-fatal and independently
    guarded.

    Execution order:
        1. G3a — GitHub token validation (fatal).
        2. G3b — ``ANTHROPIC_API_KEY`` presence (fatal).
        3. G3c — OAuth credential-volume readability (fatal).
        4. G2 — Ungraceful-prior-exit marker check (non-fatal, critical).
        5. G1 — Orphan ``claude`` process sweep (non-fatal, warn).

    Args:
        repo_cfgs: List of repo registry entries.  ``repo_cfgs[0]`` is
            used to derive the marker path and alert owner/repo.
        obs: Loaded observability config.  Not currently used directly
            but provided as a seam for future checks.
        runlog: Optional ``RunLog`` handle for best-effort event
            emission.  Passed through to ``alert()``.
    """
    owner = repo_cfgs[0].owner
    repo = repo_cfgs[0].repo
    project_root = Path(repo_cfgs[0].project_root)
    marker = project_root / ".baton-harness" / _ALIVE_MARKER

    # ------------------------------------------------------------------
    # G3a: GitHub token validation — FATAL.
    # ------------------------------------------------------------------
    try:
        validate_github_token()
    except TokenValidationError as exc:
        alert(
            owner,
            repo,
            None,
            f"Startup credential check failed: GitHub token invalid — {exc}",
            severity="critical",
            runlog=runlog,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # G3b: ANTHROPIC_API_KEY guard — FATAL if SET.
    # Architecture mandates OAuth-via-mounted-volume; a non-empty key
    # means per-token billing is active, which must be refused at startup.
    # Structural check only: presence and non-empty.  Value never
    # inspected or logged (CLAUDE.md § Credentials and Secrets).
    # ------------------------------------------------------------------
    if os.environ.get("ANTHROPIC_API_KEY"):
        alert(
            owner,
            repo,
            None,
            "ANTHROPIC_API_KEY must not be set (OAuth/subscription deployment;"
            " prevents per-token billing)",
            severity="critical",
            runlog=runlog,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # G3c: OAuth credential-volume check — FATAL if absent or unreadable.
    # Structural check only: presence + readability via open().
    # Contents are never read, decoded, or logged (CLAUDE.md § Credentials
    # and Secrets).
    # ------------------------------------------------------------------
    _cred_ok = False
    try:
        with open(_OAUTH_CRED_PATH):  # noqa: PTH123
            pass
        _cred_ok = True
    except OSError:
        pass

    if not _cred_ok:
        alert(
            owner,
            repo,
            None,
            f"OAuth credential file absent or unreadable: {_OAUTH_CRED_PATH}"
            " — mount the Claude credential volume before starting the daemon",
            severity="critical",
            runlog=runlog,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # G2: Ungraceful-prior-exit detection — non-fatal, critical alert.
    # ------------------------------------------------------------------
    try:
        if marker.exists():
            alert(
                owner,
                repo,
                None,
                "Prior daemon run ended ungracefully (possible OOM); "
                "in-flight work may have been lost",
                severity="critical",
                runlog=runlog,
            )
        # (Re)create the marker for this run.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("alive", encoding="utf-8")
    except Exception:  # noqa: BLE001
        _log.warning(
            "reconcile: G2 marker check failed; continuing", exc_info=True
        )

    # ------------------------------------------------------------------
    # G1: Orphan claude-process sweep — non-fatal, warn alert.
    # Detect-only: never kills processes (mirrors worktree_gc="detect").
    # ------------------------------------------------------------------
    try:
        pids = _list_claude_procs()
        if pids:
            alert(
                owner,
                repo,
                None,
                f"Orphan claude processes detected at startup (PIDs: {pids}); "
                "these may be leaked from a prior crashed run"
                " — inspect manually",
                severity="warn",
                runlog=runlog,
            )
    except Exception:  # noqa: BLE001
        _log.warning(
            "reconcile: G1 orphan-process sweep failed; continuing",
            exc_info=True,
        )
