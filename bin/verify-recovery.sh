#!/usr/bin/env bash
# bin/verify-recovery.sh — #40 startup-recovery verification harness (issue #116)
#
# Exercises each startup-reconciliation recovery gate (G3b, G3a, G2, G1, SIGTERM)
# against a configured sandbox and reports PASS/FAIL per scenario.
#
# PLATFORM NOTE: This script is POSIX/Linux only.  It relies on:
#   pgrep, kill -TERM, /proc/*, and standard /bin POSIX semantics.
# It does NOT run on Windows or Git-Bash dev hosts — it is intended for the
# Linux server where the daemon is deployed.  Do not attempt a Windows fallback.
#
# SAFETY: Recovery tests must NOT spawn real `claude -p` agents.
# Scenarios that pass the fatal G3 gates (G2, G1, SIGTERM) reach the poll loop.
# The sandbox MUST have zero `agent-ready` issues so the poll tick finds no work
# and dispatches nothing.  The decoy process in scenario 4 is a harmless `sleep`
# invoked so its argv contains "claude -p" — it is never a real Claude binary.
#
# Usage:
#   bin/verify-recovery.sh [--help|-h]
#
# Required environment variables:
#   BH_REPO_OWNER      GitHub repository owner (org or user login)
#   BH_REPO_NAME       GitHub repository name (without owner prefix)
#   BH_PROJECT_ROOT    Absolute path to the local clone of the managed sandbox repo
#   bh-daemon          Must be on PATH (install the harness first)
#
# Hard prerequisites (checked at startup):
#   1. bh-daemon is on PATH
#   2. The sandbox repo has NO open `agent-ready` issues (safety gate)
#   3. BH_PROJECT_ROOT is a git repo
#   4. GH_TOKEN or GITHUB_TOKEN is set and valid (needed by G3a pass path)
#
# Observability note — what IS locally observable:
#   The `alert()` function routes through `escalate()` which calls
#   `gh issue comment` (which fails — issue=None — and logs a WARNING)
#   and optionally posts to Slack.  Neither is silent: Python logging
#   emits the alert summary to stderr via the `escalate` WARNING log line.
#   Specifically, for `severity="critical"` the escalate() warning log
#   includes the prefixed body ("🚨 CRITICAL: <message>"), and for
#   `severity="warn"` the warning log includes the plain summary.
#   Assertions grep the captured daemon stderr for these log lines.
#
#   Gates that are NOT locally assertable (no local signal):
#   - The runlog JSONL event (written to obs.runlog_path — we don't read it here)
#   - Slack notification (BH_SLACK_WEBHOOK_URL not set in test env)
#   These are documented below in each scenario but not asserted.

set -euo pipefail

# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: bin/verify-recovery.sh [--help|-h]

Exercises each #40 startup-recovery gate and reports PASS/FAIL per scenario.

Required environment variables:
  BH_REPO_OWNER      GitHub repository owner (org or user login)
  BH_REPO_NAME       GitHub repository name (without owner prefix)
  BH_PROJECT_ROOT    Absolute path to the local clone of the managed sandbox repo

Prerequisites:
  - bh-daemon must be on PATH
  - Sandbox must have ZERO open `agent-ready` issues (safety guard against
    accidental agent dispatch — script aborts if any are found)
  - GH_TOKEN or GITHUB_TOKEN must be a valid fine-grained PAT

Scenarios exercised:
  G3b  ANTHROPIC_API_KEY set    → daemon refuses to start (exit != 0)
  G3a  Bogus GH_TOKEN           → daemon refuses to start (exit != 0)
  G2   Stale daemon.alive marker → critical alert fired; marker re-written
  G1   Decoy "claude -p" process → warn alert lists the decoy PID
  SIGTERM  Graceful shutdown     → daemon exits 0; daemon.alive removed

PLATFORM: Linux only (uses pgrep, kill -TERM, POSIX /bin semantics).
          Do NOT run on Windows or Git-Bash dev hosts.
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

# ---------------------------------------------------------------------------
# Safety banner (stderr so it is visible even when stdout is redirected)
# ---------------------------------------------------------------------------

print_safety_banner() {
    echo "" >&2
    echo "  *** SAFETY WARNING ***" >&2
    echo "" >&2
    echo "  This script starts bh-daemon against a LIVE sandbox repo." >&2
    echo "  The sandbox MUST have ZERO open agent-ready issues." >&2
    echo "  If any agent-ready issues exist the script ABORTS before" >&2
    echo "  running any scenario that reaches the daemon poll loop." >&2
    echo "" >&2
    echo "  The decoy 'claude -p' process is a harmless sleep — NOT a" >&2
    echo "  real Claude binary.  No agents are dispatched by this script." >&2
    echo "" >&2
    echo "  Target repo is read from BH_REPO_OWNER / BH_REPO_NAME." >&2
    echo "" >&2
}

print_safety_banner

# ---------------------------------------------------------------------------
# PASS/FAIL tally
# ---------------------------------------------------------------------------

_PASS=0
_FAIL=0
_FAILED_SCENARIOS=()

pass() {
    local name="$1"
    _PASS=$(( _PASS + 1 ))
    echo "baton-harness: [PASS] ${name}"
}

fail() {
    local name="$1"
    local reason="${2:-}"
    _FAIL=$(( _FAIL + 1 ))
    _FAILED_SCENARIOS+=("${name}")
    echo "baton-harness: [FAIL] ${name}${reason:+ — ${reason}}" >&2
}

# ---------------------------------------------------------------------------
# Validate required environment variables
# ---------------------------------------------------------------------------

_missing_env=()
for _var in BH_REPO_OWNER BH_REPO_NAME BH_PROJECT_ROOT; do
    if [[ -z "${!_var:-}" ]]; then
        _missing_env+=("${_var}")
    fi
done

if [[ ${#_missing_env[@]} -gt 0 ]]; then
    echo "baton-harness: error: the following required environment variables are not set:" >&2
    for _var in "${_missing_env[@]}"; do
        echo "  missing: ${_var}" >&2
    done
    echo "" >&2
    echo "  Set them before running bin/verify-recovery.sh:" >&2
    echo "    export BH_REPO_OWNER=<owner>" >&2
    echo "    export BH_REPO_NAME=<repo>" >&2
    echo "    export BH_PROJECT_ROOT=/path/to/local/sandbox/clone" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Locate bh-daemon (must be on PATH)
# ---------------------------------------------------------------------------

BH_DAEMON_BIN="$(command -v bh-daemon)" || {
    echo "baton-harness: error: bh-daemon not found on PATH — install the harness first" >&2
    echo "               uv pip install -e ." >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BATON_HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export BATON_HARNESS_DIR

WORKFLOW_FILE="${BATON_HARNESS_DIR}/config/WORKFLOW.md"
if [[ ! -f "${WORKFLOW_FILE}" ]]; then
    echo "baton-harness: error: workflow config not found: ${WORKFLOW_FILE}" >&2
    exit 1
fi

MARKER_PATH="${BH_PROJECT_ROOT}/.baton-harness/daemon.alive"

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

echo "baton-harness: running preflight checks..."

# BH_PROJECT_ROOT must be a git repo
if [[ ! -d "${BH_PROJECT_ROOT}" ]]; then
    echo "baton-harness: error: BH_PROJECT_ROOT does not exist: ${BH_PROJECT_ROOT}" >&2
    exit 1
fi
if ! git -C "${BH_PROJECT_ROOT}" rev-parse --git-dir &>/dev/null; then
    echo "baton-harness: error: BH_PROJECT_ROOT is not a git repository: ${BH_PROJECT_ROOT}" >&2
    exit 1
fi
echo "baton-harness: BH_PROJECT_ROOT is a git repo: ${BH_PROJECT_ROOT}"

# GH_TOKEN / GITHUB_TOKEN must be set (structural presence only — value never inspected)
_token_env_set=0
if [[ -n "${GH_TOKEN:-}" ]]; then
    _token_env_set=1
fi
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    _token_env_set=1
fi
if [[ "${_token_env_set}" -eq 0 ]]; then
    echo "baton-harness: error: neither GH_TOKEN nor GITHUB_TOKEN is set." >&2
    echo "               A valid fine-grained PAT is required for the G3a pass path." >&2
    exit 1
fi
echo "baton-harness: GitHub token env var present (structural check only)"

# Safety gate: abort if the sandbox has open agent-ready issues.
# Any such issues could be dispatched by the daemon in scenarios that
# reach the poll loop (G2, G1, SIGTERM).
echo "baton-harness: checking sandbox for open agent-ready issues (safety gate)..."
_ready_count=0
# Fail CLOSED: if gh cannot run (auth error, network error, wrong repo, etc.)
# we must NOT proceed — a failed query cannot prove the sandbox is empty, so
# allowing the scenarios that reach the poll loop would silently disable the
# safety guarantee.  Only a successful query returning exactly 0 may continue.
if ! _ready_out="$(gh issue list \
    --repo "${BH_REPO_OWNER}/${BH_REPO_NAME}" \
    --label "agent-ready" \
    --state open \
    --json number \
    --jq 'length' 2>&1)"; then
    echo "baton-harness: ABORT: gh issue list failed — cannot prove sandbox has zero" >&2
    echo "  agent-ready issues; refusing to run recovery scenarios that reach the" >&2
    echo "  poll loop.  Check BH_REPO_OWNER/BH_REPO_NAME, GH_TOKEN, and network." >&2
    echo "  gh output: ${_ready_out}" >&2
    exit 1
fi
# Guard against non-numeric output (e.g. jq parse error)
if ! [[ "${_ready_out}" =~ ^[0-9]+$ ]]; then
    echo "baton-harness: ABORT: gh issue list returned non-integer output '${_ready_out}'" >&2
    echo "  Cannot prove sandbox has zero agent-ready issues — refusing to proceed." >&2
    exit 1
fi
_ready_count="${_ready_out}"

if [[ "${_ready_count}" -gt 0 ]]; then
    echo "baton-harness: ABORT: sandbox has ${_ready_count} open agent-ready issue(s)." >&2
    echo "  Scenarios G2, G1, and SIGTERM start the daemon in continuous mode and" >&2
    echo "  WILL dispatch agents against any agent-ready issue they find." >&2
    echo "  Close or re-label all agent-ready issues before running this script." >&2
    exit 1
fi
echo "baton-harness: safety gate OK — zero open agent-ready issues"

echo "baton-harness: preflight checks passed"
echo ""

# ---------------------------------------------------------------------------
# G3c preflight: OAuth credential file presence check
#
# Once the G3c gate lands in reconcile.py, every scenario that starts the
# daemon will sys.exit(1) when ~/.claude/.credentials.json is absent (CI is
# the canonical case).  Skip the entire scenario block rather than running
# five tests that are guaranteed to fail.
#
# Path mirrors reconcile.py _OAUTH_CRED_PATH exactly:
#   Path.home() / ".claude" / ".credentials.json"  →  ${HOME}/.claude/.credentials.json
# No $CLAUDE_HOME indirection in reconcile.py — mirror that exactly.
#
# Structural check only: test -r (presence + readability). Never cat/head/grep
# the file — credential-handling discipline (CLAUDE.md § Credentials and Secrets).
# ---------------------------------------------------------------------------

_cred_path="${HOME}/.claude/.credentials.json"

if [[ ! -r "${_cred_path}" ]]; then
    echo "baton-harness: G3c preflight: OAuth creds absent at ${_cred_path} — skipping all daemon-startup scenarios"
    echo "baton-harness: RESULT: SKIPPED"
    exit 0
fi

echo "baton-harness: OAuth creds present (structural check only): ${_cred_path}"
echo ""

# ---------------------------------------------------------------------------
# Global cleanup trap — reap any decoy process and remove any marker we wrote
# ---------------------------------------------------------------------------

_DECOY_PID=""
_MARKER_WAS_CREATED_BY_US=0

# shellcheck disable=SC2329  # invoked indirectly via trap EXIT
_cleanup() {
    # Reap decoy process if still running
    if [[ -n "${_DECOY_PID}" ]] && kill -0 "${_DECOY_PID}" 2>/dev/null; then
        kill "${_DECOY_PID}" 2>/dev/null || true
        wait "${_DECOY_PID}" 2>/dev/null || true
    fi
    _DECOY_PID=""

    # Remove the marker only if this script created it (not one left by daemon)
    if [[ "${_MARKER_WAS_CREATED_BY_US}" -eq 1 ]] && [[ -f "${MARKER_PATH}" ]]; then
        rm -f "${MARKER_PATH}" || true
        _MARKER_WAS_CREATED_BY_US=0
    fi

    # Restore GH_TOKEN if we stashed it
    if [[ -n "${_SAVED_GH_TOKEN+x}" ]]; then
        export GH_TOKEN="${_SAVED_GH_TOKEN}"
        unset _SAVED_GH_TOKEN
    fi

    # Unset ANTHROPIC_API_KEY if it was set by this script
    # (we never set it if the caller had it set — we abort in that case)
    if [[ "${_WE_SET_ANTHROPIC_API_KEY:-0}" -eq 1 ]]; then
        unset ANTHROPIC_API_KEY
        _WE_SET_ANTHROPIC_API_KEY=0
    fi
}

trap '_cleanup' EXIT

# Guard: if ANTHROPIC_API_KEY is already set in the caller's env, the daemon
# would refuse to start even for pass-path scenarios.  Abort early so the
# operator knows to unset it before running the harness.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "baton-harness: error: ANTHROPIC_API_KEY is already set in your environment." >&2
    echo "  This script sets and unsets it as needed for scenario G3b." >&2
    echo "  Unset it before running: unset ANTHROPIC_API_KEY" >&2
    exit 1
fi

# Save the real GH_TOKEN so G3a can temporarily replace it
_SAVED_GH_TOKEN="${GH_TOKEN:-}"

# ---------------------------------------------------------------------------
# Helper: run bh-daemon --once with a timeout, capture output, return exit code
#
# Usage: _run_daemon_once <timeout_secs> <output_var> [extra env assignments...]
#   Runs bh-daemon --once under timeout(1); captures merged stdout+stderr into
#   the variable named by <output_var>; sets _DAEMON_EXIT to the exit code.
#   Extra env assignments (e.g. "GH_TOKEN=bogus") are applied inline.
# ---------------------------------------------------------------------------

_DAEMON_EXIT=0

_run_daemon_once() {
    local timeout_secs="$1"
    local output_var="$2"
    shift 2
    # Remaining args: "KEY=value" inline env overrides

    local _out
    local _rc=0

    # shellcheck disable=SC2016
    _out="$(
        env "$@" \
            timeout "${timeout_secs}" \
            "${BH_DAEMON_BIN}" \
                --once \
                --workflow "${WORKFLOW_FILE}" \
            2>&1
    )" || _rc=$?

    # timeout exits 124 when it kills the process — treat as non-zero daemon exit
    _DAEMON_EXIT="${_rc}"
    # Assign to caller's variable via printf + read trick (POSIX-safe nameref alt)
    printf -v "${output_var}" '%s' "${_out}"
}

# ---------------------------------------------------------------------------
# Helper: run bh-daemon in continuous mode (background), return PID
# ---------------------------------------------------------------------------

_DAEMON_BG_PID=""

_start_daemon_bg() {
    local output_file="$1"

    # Start in background, redirect all output to a temp file the caller supplies
    env \
        "${BH_DAEMON_BIN}" \
            --workflow "${WORKFLOW_FILE}" \
        > "${output_file}" 2>&1 &

    _DAEMON_BG_PID=$!
}

# ===========================================================================
# SCENARIO G3b — ANTHROPIC_API_KEY set → daemon refuses to start
# ===========================================================================
#
# Gate: reconcile.py line ~125: `if os.environ.get("ANTHROPIC_API_KEY"):`
#       → calls alert(..., severity="critical") then sys.exit(1)
#
# Assertion: non-zero exit code.
# Alert observability: alert() routes to escalate() which logs a WARNING
#   containing the critical-prefixed body.  We grep daemon stderr for the
#   key substring "ANTHROPIC_API_KEY must not be set".
#
# NOT locally assertable: runlog event, Slack notification.

echo "baton-harness: --- Scenario G3b: ANTHROPIC_API_KEY set ---"

_WE_SET_ANTHROPIC_API_KEY=1
_g3b_out=""
_run_daemon_once 30 _g3b_out "ANTHROPIC_API_KEY=dummy-value-for-test"
_g3b_exit="${_DAEMON_EXIT}"
_WE_SET_ANTHROPIC_API_KEY=0
unset ANTHROPIC_API_KEY 2>/dev/null || true

_g3b_passed=1
if [[ "${_g3b_exit}" -eq 0 ]]; then
    fail "G3b" "daemon exited 0 — expected non-zero when ANTHROPIC_API_KEY is set"
    _g3b_passed=0
fi

if ! echo "${_g3b_out}" | grep -q "ANTHROPIC_API_KEY must not be set"; then
    fail "G3b" "critical alert text not found in daemon output (expected 'ANTHROPIC_API_KEY must not be set')"
    _g3b_passed=0
fi

if [[ "${_g3b_passed}" -eq 1 ]]; then
    pass "G3b"
fi

echo ""

# ===========================================================================
# SCENARIO G3a — Bogus GH_TOKEN → validate_github_token() raises → daemon exits
# ===========================================================================
#
# Gate: _auth.py validate_github_token() reads GH_TOKEN.
#   A token without the "github_pat_" prefix → TokenValidationError immediately.
#   reconcile.py catches it → alert(..., severity="critical") then sys.exit(1).
#
# We force failure deterministically by supplying a token with a classic-PAT
# prefix ("ghp_BOGUS") — _auth.py rejects classic PATs before any network call.
# This avoids hitting the GitHub API and is instant.
#
# Assertion: non-zero exit code.
# Alert observability: we grep daemon stderr for "Startup credential check failed"
#   which appears in the alert summary emitted to escalate()'s WARNING log.
#
# NOT locally assertable: runlog event, Slack notification.

echo "baton-harness: --- Scenario G3a: bogus GH_TOKEN → token validation fatal ---"

# Temporarily replace GH_TOKEN with a classic-PAT-prefixed bogus value.
# _SAVED_GH_TOKEN holds the real token so we can restore it after.
export GH_TOKEN="ghp_BOGUS_TOKEN_FOR_TESTING"

_g3a_out=""
_run_daemon_once 30 _g3a_out
_g3a_exit="${_DAEMON_EXIT}"

# Restore real token
if [[ -n "${_SAVED_GH_TOKEN}" ]]; then
    export GH_TOKEN="${_SAVED_GH_TOKEN}"
else
    unset GH_TOKEN
fi

_g3a_passed=1
if [[ "${_g3a_exit}" -eq 0 ]]; then
    fail "G3a" "daemon exited 0 — expected non-zero when GH_TOKEN is invalid"
    _g3a_passed=0
fi

if ! echo "${_g3a_out}" | grep -q "Startup credential check failed"; then
    fail "G3a" "fatal alert text not found in daemon output (expected 'Startup credential check failed')"
    _g3a_passed=0
fi

if [[ "${_g3a_passed}" -eq 1 ]]; then
    pass "G3a"
fi

echo ""

# ===========================================================================
# SCENARIO G2 — Stale daemon.alive marker → critical alert fired
# ===========================================================================
#
# Gate: reconcile.py ~line 141: `if marker.exists():` → critical alert
#   ("Prior daemon run ended ungracefully (possible OOM); in-flight work may
#    have been lost"), then marker is (re)written mid-reconcile.  NON-FATAL.
#
# Setup: pre-create the marker file before running the daemon.
# Assertions:
#   1. Daemon exits 0 (non-fatal — daemon continues normally).
#   2. Daemon output contains "Prior daemon run ended ungracefully".
#
# NOTE: the marker is NOT asserted post-exit.  daemon.py's `finally` block
#   calls _daemon_marker.unlink(missing_ok=True) on every clean exit including
#   --once, so the marker is always absent after a successful run.  The gate
#   fired correctly if and only if the exit is 0 AND the alert text is present.
#
# SAFETY: daemon runs --once; sandbox has zero agent-ready issues (verified
#   in preflight), so the poll loop finds no work and dispatches nothing.
#
# NOT locally assertable: runlog event, Slack notification.

echo "baton-harness: --- Scenario G2: stale daemon.alive marker ---"

# Pre-create marker
mkdir -p "$(dirname "${MARKER_PATH}")"
echo "stale-from-prior-run" > "${MARKER_PATH}"
_MARKER_WAS_CREATED_BY_US=1

_g2_out=""
_run_daemon_once 60 _g2_out
_g2_exit="${_DAEMON_EXIT}"

_g2_passed=1

# Expect exit 0 (non-fatal gate)
if [[ "${_g2_exit}" -ne 0 ]]; then
    fail "G2" "daemon exited ${_g2_exit} — expected 0 (G2 is non-fatal)"
    _g2_passed=0
fi

# Expect the critical alert text in daemon output
if ! echo "${_g2_out}" | grep -q "Prior daemon run ended ungracefully"; then
    fail "G2" "critical alert text not found in daemon output (expected 'Prior daemon run ended ungracefully')"
    _g2_passed=0
fi

if [[ "${_g2_passed}" -eq 1 ]]; then
    pass "G2"
fi

# Clean up any marker left by setup (daemon --once finally block removes it on
# graceful exit; rm -f here is a belt-and-suspenders no-op in the happy path).
rm -f "${MARKER_PATH}" || true
_MARKER_WAS_CREATED_BY_US=0

echo ""

# ===========================================================================
# SCENARIO G1 — Decoy "claude -p" process → orphan warn alert lists PID
# ===========================================================================
#
# Gate: reconcile.py ~line 165: `pids = _list_claude_procs()`
#   `pgrep -f 'claude -p'` matches the decoy; if non-empty → warn alert:
#   "Orphan claude processes detected at startup (PIDs: [<pid>]) …"
#
# SAFETY: The decoy is `sleep 999 claude -p` — a harmless process whose argv
#   contains the literal string "claude -p" (matching pgrep -f 'claude -p')
#   without invoking any actual Claude binary.  The decoy is reaped in the
#   EXIT trap unconditionally.
#
# Daemon runs --once with zero agent-ready issues in sandbox → no dispatch.
#
# Assertions:
#   1. Daemon exits 0 (G1 is non-fatal).
#   2. Daemon output contains "Orphan claude processes detected at startup".
#   3. Daemon output contains the decoy PID.
#
# NOT locally assertable: runlog event, Slack notification.

echo "baton-harness: --- Scenario G1: decoy 'claude -p' process → orphan sweep ---"

# Spawn decoy: a long sleep whose argv includes "claude -p" so pgrep -f matches.
# We use a shell wrapper so the argv of the child contains "claude -p".
# shellcheck disable=SC2016
( exec -a 'sleep 999 claude -p' sleep 999 ) &
_DECOY_PID=$!

# Brief pause to ensure the decoy is visible to pgrep before we start the daemon
sleep 1

_g1_out=""
_run_daemon_once 60 _g1_out
_g1_exit="${_DAEMON_EXIT}"

# Reap decoy immediately
if [[ -n "${_DECOY_PID}" ]] && kill -0 "${_DECOY_PID}" 2>/dev/null; then
    kill "${_DECOY_PID}" 2>/dev/null || true
    wait "${_DECOY_PID}" 2>/dev/null || true
fi
_DECOY_PID=""

_g1_passed=1

# Expect exit 0 (non-fatal gate)
if [[ "${_g1_exit}" -ne 0 ]]; then
    fail "G1" "daemon exited ${_g1_exit} — expected 0 (G1 is non-fatal)"
    _g1_passed=0
fi

# Expect the warn alert text in daemon output
if ! echo "${_g1_out}" | grep -q "Orphan claude processes detected at startup"; then
    fail "G1" "warn alert text not found in daemon output (expected 'Orphan claude processes detected at startup')"
    _g1_passed=0
fi

if [[ "${_g1_passed}" -eq 1 ]]; then
    pass "G1"
fi

# Clean up marker left by daemon --once finally block (should already be gone)
rm -f "${MARKER_PATH}" || true
_MARKER_WAS_CREATED_BY_US=0

echo ""

# ===========================================================================
# SCENARIO SIGTERM — Graceful shutdown clears daemon.alive marker
# ===========================================================================
#
# Gate: daemon.py SIGTERM handler: unlinks marker then raises SystemExit(0).
#   finally block also calls _daemon_marker.unlink(missing_ok=True).
#
# SAFETY: daemon runs in continuous mode (no --once); sandbox has zero
#   agent-ready issues so the outer loop finds no work and does not dispatch.
#   We wait for the daemon.alive marker to appear (confirming startup completed
#   past reconcile_startup), then send SIGTERM.
#
# Assertions:
#   1. Daemon process exits with code 0.
#   2. daemon.alive marker does NOT exist after clean exit.
#
# Note on exit-code from SIGTERM: Python's SystemExit(0) exits 0.
#   The SIGTERM handler explicitly raises SystemExit(0), so exit code = 0.
#
# NOT locally assertable: runlog daemon_start / daemon_stop events, Slack.

echo "baton-harness: --- Scenario SIGTERM: graceful shutdown clears daemon.alive ---"

# Temp file to collect background daemon output
_SIGTERM_OUTPUT_FILE="$(mktemp /tmp/bh-verify-sigterm.XXXXXX)"

# Start daemon in continuous mode (background)
_start_daemon_bg "${_SIGTERM_OUTPUT_FILE}"
_sigterm_daemon_pid="${_DAEMON_BG_PID}"
_DAEMON_BG_PID=""

# Wait for daemon.alive marker to appear (up to 60 seconds)
_marker_appeared=0
for _i in $(seq 1 60); do
    if [[ -f "${MARKER_PATH}" ]]; then
        _marker_appeared=1
        break
    fi
    sleep 1
done

if [[ "${_marker_appeared}" -eq 0 ]]; then
    # Daemon never wrote marker — something went wrong at startup
    kill "${_sigterm_daemon_pid}" 2>/dev/null || true
    wait "${_sigterm_daemon_pid}" 2>/dev/null || true
    fail "SIGTERM" "daemon.alive marker never appeared within 60s — daemon may have failed at startup"
    _sigterm_out="$(cat "${_SIGTERM_OUTPUT_FILE}" 2>/dev/null || true)"
    echo "  daemon output:" >&2
    echo "${_sigterm_out}" | head -20 >&2
    rm -f "${_SIGTERM_OUTPUT_FILE}" || true
    echo ""
else
    echo "baton-harness:   daemon.alive marker appeared — sending SIGTERM to PID ${_sigterm_daemon_pid}"

    # Send SIGTERM and wait for daemon to exit
    kill -TERM "${_sigterm_daemon_pid}" 2>/dev/null || true
    _sigterm_exit=0
    wait "${_sigterm_daemon_pid}" 2>/dev/null || _sigterm_exit=$?

    _sigterm_out="$(cat "${_SIGTERM_OUTPUT_FILE}" 2>/dev/null || true)"
    rm -f "${_SIGTERM_OUTPUT_FILE}" || true

    _sigterm_passed=1

    # Expect exit 0 (SystemExit(0) from SIGTERM handler)
    # Note: on some systems, a process killed by SIGTERM exits 143 (128+15).
    # Python's SIGTERM handler calls raise SystemExit(0), so Python exits 0.
    # If the handler did not fire (killed externally), exit would be 143.
    if [[ "${_sigterm_exit}" -ne 0 ]]; then
        fail "SIGTERM" "daemon exited ${_sigterm_exit} — expected 0 (SIGTERM handler raises SystemExit(0))"
        _sigterm_passed=0
    fi

    # Expect marker to be removed
    if [[ -f "${MARKER_PATH}" ]]; then
        fail "SIGTERM" "daemon.alive marker still exists after graceful SIGTERM — not removed by handler/finally"
        rm -f "${MARKER_PATH}" || true
        _sigterm_passed=0
    fi

    if [[ "${_sigterm_passed}" -eq 1 ]]; then
        pass "SIGTERM"
    fi

    echo ""
fi

# Ensure marker is cleaned up regardless
rm -f "${MARKER_PATH}" || true
_MARKER_WAS_CREATED_BY_US=0

# ===========================================================================
# Summary
# ===========================================================================

echo "baton-harness: =============================="
echo "baton-harness: Recovery verification summary"
echo "baton-harness: =============================="
echo "baton-harness:   PASSED: ${_PASS}"
echo "baton-harness:   FAILED: ${_FAIL}"

if [[ ${_FAIL} -gt 0 ]]; then
    echo "baton-harness:   Failed scenarios:" >&2
    for _s in "${_FAILED_SCENARIOS[@]}"; do
        echo "baton-harness:     - ${_s}" >&2
    done
    echo "" >&2
    echo "baton-harness: RESULT: FAIL" >&2
    exit 1
fi

echo "baton-harness: RESULT: PASS"
exit 0
