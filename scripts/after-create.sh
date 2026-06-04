#!/usr/bin/env bash
# scripts/after-create.sh — Baton after_create hook
#
# Runs once after a worktree is created; cwd is the new worktree.
# Detects the project type and installs per-worktree dependencies so each
# worktree is self-contained from the start.
#
# This is a PARTIAL mitigation for worktree isolation (architecture-spec S2.4):
# it handles dependency installation only.  Shared resources such as ports,
# databases, and other services are NOT addressed here and require manual
# coordination during the pilot phase.
#
# Baton passes no env-var context to hooks (spike finding F2).  Issue number is
# derived from `basename "$PWD"` — the worktree directory is named by issue
# number, which is the intended pattern, not a workaround.
#
# Usage (invoked by Baton, or standalone for testing):
#   after-create.sh [issue-number]
#
# Arguments:
#   issue-number  (optional) Label for log messages.  Derived from
#                 `basename "$PWD"` when absent.

set -euo pipefail

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

ISSUE="${1:-$(basename "$PWD")}"
log() { echo "[after-create #${ISSUE}] $*"; }

# ---------------------------------------------------------------------------
# Dependency detection and install
# ---------------------------------------------------------------------------

installed=false

# --- Node.js / npm ---
if [[ -f "package.json" ]]; then
    if [[ -f "package-lock.json" ]]; then
        log "package.json + package-lock.json found → running: npm ci"
        npm ci
    else
        log "package.json found (no lockfile) → running: npm install"
        npm install
    fi
    installed=true
fi

# --- Python (requirements.txt) ---
if [[ -f "requirements.txt" ]]; then
    if command -v uv &>/dev/null; then
        log "requirements.txt found, uv available → running: uv pip install -r requirements.txt"
        uv pip install -r requirements.txt
    else
        log "requirements.txt found, uv not found → running: pip install -r requirements.txt"
        pip install -r requirements.txt
    fi
    installed=true
fi

# --- Python (pyproject.toml) ---
# Install as an editable package.  Try the [dev] extra first (covers the common
# case of test/lint extras declared under [project.optional-dependencies]); fall
# back to plain -e . if [dev] doesn't exist.  Using -e ".[dev]" is simple and
# documented — adjust per project if a different extra name is needed.
if [[ -f "pyproject.toml" ]]; then
    if command -v uv &>/dev/null; then
        log "pyproject.toml found, uv available → running: uv pip install -e .[dev]"
        uv pip install -e ".[dev]" 2>/dev/null \
            || { log "  [dev] extra not present, retrying without it"; uv pip install -e .; }
    else
        log "pyproject.toml found, uv not found → running: pip install -e .[dev]"
        pip install -e ".[dev]" 2>/dev/null \
            || { log "  [dev] extra not present, retrying without it"; pip install -e .; }
    fi
    installed=true
fi

# --- No recognised project type ---
if [[ "${installed}" == "false" ]]; then
    log "no dependency files found (package.json / requirements.txt / pyproject.toml) — nothing to install"
fi

log "done"
