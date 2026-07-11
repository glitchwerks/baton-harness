"""Startup reconciliation sweep for the baton-harness daemon.

Runs startup checks at daemon startup (issues #40, #108, #219):

- **G3a** — GitHub token validation (fatal on failure).
- **G3b** — ``ANTHROPIC_API_KEY`` presence check (fatal on failure).
- **G3c** — OAuth credential-volume health-check (fatal on failure).
- **G3d** — git push credential-helper presence check (fatal on failure).
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

G3d (issue #219) is a presence/shape check only: it inspects ``git
config`` key NAMES to confirm a credential helper is configured, and
never reads or logs a helper's VALUE or any credential material (see
CLAUDE.md § Credentials and Secrets).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from baton_harness._auth import (
    TokenValidationError,
    validate_daemon_token,
    validate_github_token,  # noqa: F401 — kept for test patch target
)
from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    resolve_installation_token,
)
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
    result = (
        subprocess.run(  # identity: env-exempt -- local pgrep liveness probe
            ["pgrep", "-f", "claude -p"],
            capture_output=True,
            text=True,
        )
    )
    # pgrep exits 1 when no matches found (not an error).
    if result.returncode == 0:
        return [int(p) for p in result.stdout.split() if p.strip()]
    return []


# git config keys probed by G3d, in priority order.  The github.com-scoped
# key is checked first; the global ``credential.helper`` is the fallback
# used when no host-scoped helper is configured.
_GIT_CREDENTIAL_HELPER_KEYS: tuple[str, ...] = (
    "credential.https://github.com.helper",
    "credential.helper",
)


def _get_git_credential_helpers() -> list[str]:
    """Return configured git credential helper NAMES for github.com push.

    Probes ``git config --get-all`` for a github.com-scoped credential
    helper first, falling back to the global ``credential.helper`` key
    when the scoped one is absent.  This is a structural presence/shape
    check only — helper command strings may be returned (they name a
    helper program, e.g. ``manager`` or ``!gh auth git-credential``),
    but no credential VALUE (token, password) is ever read or returned;
    ``git config`` never exposes secret material for this key.

    Returns:
        A list of configured helper command strings for the first key
        (in priority order) that has any value.  Empty list when
        neither the github.com-scoped nor the global helper is
        configured, or when ``git`` itself cannot be invoked.
    """
    for key in _GIT_CREDENTIAL_HELPER_KEYS:
        try:
            result = subprocess.run(  # identity: env-exempt
                ["git", "config", "--get-all", key],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except OSError:
            # git not on PATH — treat as "no helper configured" rather
            # than crashing the startup gate.
            return []
        if result.returncode == 0:
            helpers = [
                line for line in result.stdout.splitlines() if line.strip()
            ]
            if helpers:
                return helpers
    return []


async def reconcile_startup(
    repo_cfgs: list[RepoConfig],
    obs: ObsConfig | None,
    runlog: RunLog | None,
    *,
    installation_token: InstallationTokenSource = "",
) -> None:
    """Run the startup reconciliation sweep.

    Executes the startup checks in order.  Credential failures (G3a,
    G3b, G3c, G3d) are fatal: they emit a critical alert then call
    ``sys.exit(1)``.  All other checks are non-fatal and independently
    guarded.

    Execution order:
        1. G3a — GitHub token validation (fatal).
        2. G3b — ``ANTHROPIC_API_KEY`` presence (fatal).
        3. G3c — OAuth credential-volume readability (fatal).
        4. G3d — git push credential-helper presence (fatal).
        5. G2 — Ungraceful-prior-exit marker check (non-fatal, critical).
        6. G1 — Orphan ``claude`` process sweep (non-fatal, warn).

    Args:
        repo_cfgs: List of repo registry entries.  ``repo_cfgs[0]`` is
            used to derive the marker path and alert owner/repo.
        obs: Loaded observability config.  Not currently used directly
            but provided as a seam for future checks.
        runlog: Optional ``RunLog`` handle for best-effort event
            emission.  Passed through to ``alert()``.
        installation_token: The minted GitHub App installation access
            token (``ghs_`` prefix) returned by ``bootstrap_secrets()``.
            When non-empty, this value is passed directly to
            ``validate_daemon_token`` — ``os.environ`` is never read
            for the token (env-discipline invariant).  Pass ``""``
            (default) to fall back to the ambient ``GH_TOKEN`` /
            ``GITHUB_TOKEN`` env var (legacy / test path).
    """
    owner = repo_cfgs[0].owner
    repo = repo_cfgs[0].repo
    project_root = Path(repo_cfgs[0].project_root)
    marker = project_root / ".baton-harness" / _ALIVE_MARKER

    # ------------------------------------------------------------------
    # G3a: GitHub token validation — FATAL.
    # Daemon path uses validate_daemon_token (accepts ghs_ installation
    # tokens).  When installation_token is provided by-value (slice 3a
    # env-discipline), validate that token directly — do NOT read from
    # os.environ.  Fall back to os.environ only when no token was
    # threaded (legacy / test path).
    # ------------------------------------------------------------------
    if installation_token:
        token = resolve_installation_token(installation_token)
    else:
        token = os.environ.get("GH_TOKEN", "") or os.environ.get(
            "GITHUB_TOKEN", ""
        )
    try:
        validate_daemon_token(token)
    except TokenValidationError as exc:
        alert(
            owner,
            repo,
            None,
            f"Startup credential check failed: GitHub token invalid — {exc}",
            severity="critical",
            runlog=runlog,
            installation_token=installation_token,
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
            installation_token=installation_token,
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
            installation_token=installation_token,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # G3d: git push credential-helper presence — FATAL if absent (#219).
    # `gh auth login` authenticates the gh CLI but does NOT install a git
    # credential helper, so a bare `git push` fails at first use with
    # "Password authentication is not supported" even though GH_TOKEN is
    # present.  Fail loud at startup instead of at first push.
    # Structural check only: probes `git config` key NAMES for a
    # github.com-scoped or global credential helper; never reads or logs
    # a helper's value or any credential material.
    # ------------------------------------------------------------------
    if not _get_git_credential_helpers():
        alert(
            owner,
            repo,
            None,
            "No git credential helper configured for github.com (or "
            "globally) — `git push` will fail with 'Password "
            "authentication is not supported' even though a GitHub "
            "token is present. Fix: run `gh auth setup-git`.",
            severity="critical",
            runlog=runlog,
            installation_token=installation_token,
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
                installation_token=installation_token,
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
                installation_token=installation_token,
            )
    except Exception:  # noqa: BLE001
        _log.warning(
            "reconcile: G1 orphan-process sweep failed; continuing",
            exc_info=True,
        )
