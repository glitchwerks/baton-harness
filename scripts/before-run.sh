#!/usr/bin/env bash
# scripts/before-run.sh — Baton before_run hook
#
# Syncs the current worktree branch onto the latest origin/main before the
# agent runs, so the agent always works from a current base.
#
# This script is IDEMPOTENT: running it multiple times on an already-rebased
# branch exits 0 with an informative message.
#
# Conflict handling: if the rebase cannot complete cleanly, the script aborts
# the rebase (restoring the worktree to its pre-rebase state) and exits
# non-zero with a clear message so Baton sees the failure and does not
# proceed with a mid-rebase worktree.
#
# Baton passes no env-var context to hooks (spike finding F2).  Issue number is
# derived from `basename "$PWD"` — the worktree directory is named by issue
# number, which is the intended pattern, not a workaround.
#
# Usage (invoked by Baton, or standalone for testing):
#   before-run.sh [issue-number]
#
# Arguments:
#   issue-number  (optional) Label for log messages.  Derived from
#                 `basename "$PWD"` when absent.

set -euo pipefail

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

ISSUE="${1:-$(basename "$PWD")}"
log()  { echo "[before-run #${ISSUE}] $*"; }
err()  { echo "[before-run #${ISSUE}] ERROR: $*" >&2; }

# ---------------------------------------------------------------------------
# Fetch latest main from origin
# ---------------------------------------------------------------------------

log "fetching origin/main..."
git fetch origin main

# ---------------------------------------------------------------------------
# Rebase current branch onto origin/main
#
# `git rebase origin/main` is idempotent when the branch is already up-to-date:
# git reports "Current branch <name> is up to date" and exits 0.
# ---------------------------------------------------------------------------

log "rebasing onto origin/main..."
if git rebase origin/main; then
    log "rebase complete — branch is up to date with origin/main"
else
    # Rebase failed (conflict or other error).  Abort to restore clean state
    # so the worktree is not left mid-rebase.
    err "rebase failed — aborting to restore clean worktree state"
    git rebase --abort 2>/dev/null || true
    err "rebase conflict with origin/main; resolve conflicts manually and re-run"
    err "branch has NOT been modified — worktree is back to its pre-rebase state"
    exit 1
fi
