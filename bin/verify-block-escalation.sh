#!/usr/bin/env bash
# bin/verify-block-escalation.sh — #239 self-block escalation verification harness
#
# Exercises the WORKFLOW.md "Confidence / block rule" end to end against a
# live sandbox: seeds a genuinely ambiguous agent-ready issue, runs a single
# bh-daemon poll tick, and asserts the full park+escalate chain fires:
#   agent posts a clarifying question -> agent adds `blocked` -> daemon's
#   post-turn label re-read sees `blocked` -> park path (kind="block",
#   reason="blocked label set") -> alert(severity="warn", kind="block")
#   -> escalation.escalate() posts the durable GitHub comment (and, when
#   configured, a best-effort Slack ping).
#
# PLATFORM NOTE: intended for the Linux server where the daemon is deployed
# (same target as bin/verify-recovery.sh). It has no /proc or pgrep
# dependency, so it is more portable than the sibling script, but it DOES
# spawn a real `claude -p` agent turn via bh-daemon --once — do not run it
# against a repo you are not prepared to have a real agent commit/comment
# on. It is not a decoy-only script like verify-recovery.sh.
#
# SAFETY: this script deliberately dispatches ONE real agent turn against
# ONE seeded issue. The safety gate below aborts if the sandbox already has
# any OTHER open `agent-ready` issue, so the single `--once` tick can only
# ever pick up the issue this script created.
#
# Usage:
#   bin/verify-block-escalation.sh [--help|-h]
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
#   4. GH_TOKEN or GITHUB_TOKEN is set and valid
#   5. ANTHROPIC_API_KEY is NOT set (OAuth/subscription deployment — G3b)
#   6. ~/.claude/.credentials.json is present and readable (G3c)
#   7. Required labels (agent-ready, agent-in-progress, blocked) exist in
#      the target repo
#
# Observability note — what IS locally observable:
#   escalation.escalate() logs a line for every GitHub-comment attempt it
#   makes: INFO ("escalate: GitHub comment posted on issue #N (kind=block)")
#   on success, WARNING ("escalate: failed to post GitHub comment on issue
#   #N ... kind=block ...") on failure. Both substrings include "issue #N"
#   and "kind=block", so this script greps for that pair rather than
#   hard-coding one log level — a real sandbox run is expected to succeed
#   (INFO), but the assertion tolerates the WARNING failure-path text too.
#   Slack notifications (when BH_SLACK_WEBHOOK_URL is set) log similarly:
#   INFO "escalate: Slack notification posted (issue #N kind=block)" or
#   WARNING "escalate: Slack POST failed (issue #N kind=block): ...".
#
#   NOTE: the Slack payload (when sent) is the DAEMON's park summary
#   ("Issue #N parked: blocked label set."), NOT the agent's clarifying
#   question — the question only ever lands on the GitHub issue comment
#   thread. This script does not assert on Slack message content (it
#   cannot inspect Slack directly); it only asserts that a POST was
#   attempted, via the daemon log line above.
#
#   Gates that are NOT locally assertable (no local signal):
#   - The runlog JSONL "escalation" event (written to obs.runlog_path)
#   - The literal content actually delivered to Slack

set -euo pipefail

# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: bin/verify-block-escalation.sh [--help|-h]

Exercises the #239 self-block escalation chain against a configured
sandbox: seeds a genuinely ambiguous agent-ready issue, runs one
bh-daemon --once poll tick, and asserts the park+escalate chain fired.

Required environment variables:
  BH_REPO_OWNER      GitHub repository owner (org or user login)
  BH_REPO_NAME       GitHub repository name (without owner prefix)
  BH_PROJECT_ROOT    Absolute path to the local clone of the managed sandbox repo

Prerequisites:
  - bh-daemon must be on PATH
  - Sandbox must have ZERO open `agent-ready` issues before this script
    seeds its own (safety guard — the single poll tick may only ever pick
    up the issue this script creates)
  - GH_TOKEN or GITHUB_TOKEN must be a valid fine-grained PAT
  - ANTHROPIC_API_KEY must NOT be set
  - ~/.claude/.credentials.json must be present and readable
  - agent-ready / agent-in-progress / blocked labels must exist in the
    target repo

Assertions:
  1. `blocked` label present on the seeded issue after the run
  2. `agent-in-progress` label NOT present after the run
  3. escalation log line present in captured daemon output (kind=block)
  4. at least one comment exists on the issue post-run (clarifying question)
  5. (conditional) if BH_SLACK_WEBHOOK_URL is set, a Slack POST was
     attempted per the daemon log; otherwise this assertion is SKIPPED,
     not failed

This script dispatches ONE real Claude Code agent turn. It is NOT a
decoy-only script like bin/verify-recovery.sh.

PLATFORM: intended for the Linux server where the daemon is deployed.
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
    echo "  This script starts bh-daemon against a LIVE sandbox repo and" >&2
    echo "  dispatches ONE real Claude Code agent turn." >&2
    echo "" >&2
    echo "  The sandbox MUST have ZERO open agent-ready issues before this" >&2
    echo "  script seeds its own — otherwise the poll tick could dispatch" >&2
    echo "  unrelated work. The script aborts before seeding if any exist." >&2
    echo "" >&2
    echo "  The seeded issue is deliberately ambiguous (ACs with more than" >&2
    echo "  one reasonable interpretation) so the agent is expected to" >&2
    echo "  self-block rather than implement anything." >&2
    echo "" >&2
    echo "  Target repo is read from BH_REPO_OWNER / BH_REPO_NAME." >&2
    echo "" >&2
}

print_safety_banner

# ---------------------------------------------------------------------------
# PASS/FAIL/SKIPPED tally
# ---------------------------------------------------------------------------

_PASS=0
_FAIL=0
_SKIPPED=0
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

skipped() {
    local name="$1"
    local reason="${2:-}"
    _SKIPPED=$(( _SKIPPED + 1 ))
    echo "baton-harness: [SKIPPED] ${name}${reason:+ — ${reason}}"
}

# ---------------------------------------------------------------------------
# Source shared env-config loader (host.env -> BH_PROJECT_ROOT;
# .bh/config.env -> BH_REPO_OWNER/BH_REPO_NAME/etc; operator env wins)
# ---------------------------------------------------------------------------

_BH_LOAD_CONFIG="$(dirname "${BASH_SOURCE[0]}")/lib/load-config.sh"
if [[ -f "${_BH_LOAD_CONFIG}" ]]; then
    # shellcheck disable=SC1091
    source "${_BH_LOAD_CONFIG}"
fi
unset _BH_LOAD_CONFIG

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
    echo "  Set them before running bin/verify-block-escalation.sh:" >&2
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

_REPO_SLUG="${BH_REPO_OWNER}/${BH_REPO_NAME}"

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
    echo "               A valid fine-grained PAT is required to seed the issue and run the daemon." >&2
    exit 1
fi
echo "baton-harness: GitHub token env var present (structural check only)"

# ANTHROPIC_API_KEY must NOT be set (G3b) — the daemon refuses to start
# otherwise, and unlike verify-recovery.sh this script does not need to
# exercise that gate, so it simply requires the caller to have it unset.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "baton-harness: error: ANTHROPIC_API_KEY is set in your environment." >&2
    echo "  The daemon refuses to start (G3b) while this key is present" >&2
    echo "  (OAuth/subscription deployment expects it to be absent)." >&2
    echo "  Unset it before running: unset ANTHROPIC_API_KEY" >&2
    exit 1
fi
echo "baton-harness: ANTHROPIC_API_KEY is unset (G3b precondition OK)"

# Safety gate: abort if the sandbox already has open agent-ready issues.
# This script is about to seed exactly one — if others already exist, the
# single --once tick could dispatch the wrong issue (or several).
echo "baton-harness: checking sandbox for open agent-ready issues (safety gate)..."
_ready_count=0
# Fail CLOSED: if gh cannot run (auth error, network error, wrong repo, etc.)
# we must NOT proceed — a failed query cannot prove the sandbox is empty.
if ! _ready_out="$(gh issue list \
    --repo "${_REPO_SLUG}" \
    --label "agent-ready" \
    --state open \
    --json number \
    --jq 'length' 2>&1)"; then
    echo "baton-harness: ABORT: gh issue list failed — cannot prove sandbox has zero" >&2
    echo "  agent-ready issues; refusing to seed a new issue or run the daemon." >&2
    echo "  Check BH_REPO_OWNER/BH_REPO_NAME, GH_TOKEN, and network." >&2
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
    echo "  The single --once poll tick this script runs could dispatch any of" >&2
    echo "  them instead of (or in addition to) the issue this script seeds." >&2
    echo "  Close or re-label all agent-ready issues before running this script." >&2
    exit 1
fi
echo "baton-harness: safety gate OK — zero open agent-ready issues"

# Required-labels preflight (mirrors bin/run-daemon.sh) — this script both
# seeds `agent-ready` and asserts on `blocked` / `agent-in-progress`.
echo "baton-harness: checking required labels in ${_REPO_SLUG}..."
_required_labels=(agent-ready agent-in-progress blocked)
_missing_labels=()
_existing_labels="$(gh label list -R "${_REPO_SLUG}" --limit 200 --json name --jq '.[].name')"
for _label in "${_required_labels[@]}"; do
    if ! echo "${_existing_labels}" | grep -qxF "${_label}"; then
        _missing_labels+=("${_label}")
    fi
done
if [[ ${#_missing_labels[@]} -gt 0 ]]; then
    echo "baton-harness: error: the following required labels are missing from ${_REPO_SLUG}:" >&2
    for _label in "${_missing_labels[@]}"; do
        echo "  missing: ${_label}" >&2
        echo "  fix:     gh label create \"${_label}\" -R \"${_REPO_SLUG}\" --color 0075ca" >&2
    done
    exit 1
fi
echo "baton-harness: all required labels present"

echo "baton-harness: preflight checks passed"
echo ""

# ---------------------------------------------------------------------------
# G3c preflight: OAuth credential file presence check
#
# Mirrors verify-recovery.sh exactly. If absent, bh-daemon --once would
# sys.exit(1) at startup before ever polling, producing a misleading FAIL
# rather than a real signal about the block-escalation chain.
#
# Structural check only: test -r (presence + readability). Never cat/head/
# grep the file — credential-handling discipline (CLAUDE.md § Credentials
# and Secrets).
# ---------------------------------------------------------------------------

_cred_path="${HOME}/.claude/.credentials.json"

if [[ ! -r "${_cred_path}" ]]; then
    echo "baton-harness: G3c preflight: OAuth creds absent at ${_cred_path} — skipping the block-escalation scenario"
    echo "baton-harness: RESULT: SKIPPED"
    exit 0
fi

echo "baton-harness: OAuth creds present (structural check only): ${_cred_path}"
echo ""

# ---------------------------------------------------------------------------
# Global cleanup trap — remove labels from / close the seeded issue, and
# delete any temp files we wrote.
# ---------------------------------------------------------------------------

_ISSUE_NUM=""
_DAEMON_OUTPUT_FILE=""

# shellcheck disable=SC2329  # invoked indirectly via trap EXIT
_cleanup() {
    if [[ -n "${_DAEMON_OUTPUT_FILE}" && -f "${_DAEMON_OUTPUT_FILE}" ]]; then
        rm -f "${_DAEMON_OUTPUT_FILE}" || true
    fi

    if [[ -n "${_ISSUE_NUM}" ]]; then
        echo "baton-harness: cleanup: removing labels from and closing issue #${_ISSUE_NUM}" >&2
        gh issue edit "${_ISSUE_NUM}" \
            --repo "${_REPO_SLUG}" \
            --remove-label "agent-ready,agent-in-progress,blocked,agent-done" \
            &>/dev/null || true
        gh issue close "${_ISSUE_NUM}" \
            --repo "${_REPO_SLUG}" \
            --comment "Closed automatically by bin/verify-block-escalation.sh cleanup." \
            &>/dev/null || true
    fi
}

trap '_cleanup' EXIT

# ---------------------------------------------------------------------------
# Seed a genuinely ambiguous agent-ready issue.
#
# The acceptance criterion below has more than one reasonable
# interpretation on purpose: "a reasonable eviction policy" could mean
# LRU, LFU, TTL-based expiry, or a fixed max-size FIFO — the WORKFLOW.md
# confidence/block rule requires the agent to stop and ask rather than
# guess which one is intended.
# ---------------------------------------------------------------------------

echo "baton-harness: --- Seeding ambiguous agent-ready issue ---"

_ISSUE_TITLE="Add a small cache utility with a reasonable eviction policy"
_ISSUE_BODY="$(cat <<'EOF'
Add a small in-memory cache utility to this project: a function or class
that memoizes the result of an expensive call so repeated calls with the
same input are served from cache instead of recomputing.

## Acceptance Criteria

- [ ] Add a cache utility (function or class) with a `get`/`set`-style
      interface.
- [ ] The cache uses a reasonable eviction policy so it does not grow
      without bound over time.
- [ ] Add a short docstring describing how to use it.

## Notes

This issue was seeded by bin/verify-block-escalation.sh to exercise the
WORKFLOW.md self-block rule. If you are an autonomous agent reading this:
the eviction policy is intentionally unspecified.
EOF
)"

if ! _issue_url="$(gh issue create \
    --repo "${_REPO_SLUG}" \
    --title "${_ISSUE_TITLE}" \
    --body "${_ISSUE_BODY}" \
    --label agent-ready 2>&1)"; then
    echo "baton-harness: error: gh issue create failed:" >&2
    echo "${_issue_url}" >&2
    exit 1
fi

_ISSUE_NUM="${_issue_url##*/}"
if ! [[ "${_ISSUE_NUM}" =~ ^[0-9]+$ ]]; then
    echo "baton-harness: error: could not parse issue number from gh output: ${_issue_url}" >&2
    _ISSUE_NUM=""
    exit 1
fi

echo "baton-harness: seeded issue #${_ISSUE_NUM} (${_issue_url})"
echo ""

# ---------------------------------------------------------------------------
# Run one bh-daemon --once poll tick.
#
# Unlike verify-recovery.sh's decoy-only scenarios, this tick DOES dispatch
# a real agent turn (against the single seeded issue above). Timeout is
# generous (10 minutes) because the agent must read the issue, reason
# about the ambiguity, post a comment, add the `blocked` label, and stop —
# bounded by max_turns in config/WORKFLOW.md, but real model latency can
# still take several minutes.
# ---------------------------------------------------------------------------

echo "baton-harness: --- Running bh-daemon --once (dispatches real agent turn) ---"

_DAEMON_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/bh-verify-block.XXXXXX")"
_BLOCK_TIMEOUT_SECS="${BH_VERIFY_BLOCK_TIMEOUT_SECS:-600}"

_daemon_exit=0
timeout "${_BLOCK_TIMEOUT_SECS}" \
    "${BH_DAEMON_BIN}" \
        --once \
        --workflow "${WORKFLOW_FILE}" \
    > "${_DAEMON_OUTPUT_FILE}" 2>&1 || _daemon_exit=$?

if [[ "${_daemon_exit}" -ne 0 ]]; then
    echo "baton-harness: warning: bh-daemon --once exited ${_daemon_exit} (expected 0 — park is non-fatal)" >&2
    if [[ "${_daemon_exit}" -eq 124 ]]; then
        echo "baton-harness: warning: exit 124 means the ${_BLOCK_TIMEOUT_SECS}s timeout fired" >&2
    fi
    echo "baton-harness: --- last 40 lines of daemon output ---" >&2
    tail -40 "${_DAEMON_OUTPUT_FILE}" >&2 || true
fi

echo ""

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

echo "baton-harness: --- Assertions: block escalation chain for #${_ISSUE_NUM} ---"

# Re-fetch current labels on the seeded issue.
_final_labels="$(gh issue view "${_ISSUE_NUM}" \
    --repo "${_REPO_SLUG}" \
    --json labels \
    --jq '.labels[].name' 2>&1)" || {
    fail "BLOCK-labels-fetch" "gh issue view failed: ${_final_labels}"
    _final_labels=""
}

# --- Assertion 1: `blocked` label present ---
if echo "${_final_labels}" | grep -qxF "blocked"; then
    pass "BLOCK-label-present"
else
    fail "BLOCK-label-present" "issue #${_ISSUE_NUM} does not carry the 'blocked' label; got: $(echo "${_final_labels}" | tr '\n' ' ')"
fi

# --- Assertion 2: `agent-in-progress` label NOT present ---
if echo "${_final_labels}" | grep -qxF "agent-in-progress"; then
    fail "BLOCK-in-progress-cleared" "issue #${_ISSUE_NUM} still carries 'agent-in-progress'"
else
    pass "BLOCK-in-progress-cleared"
fi

# --- Assertion 3: escalation log line present in captured daemon output ---
# escalation.escalate() logs "issue #<n>" and "kind=block" on both the
# success (INFO) and failure (WARNING) paths — see header note. Grep for
# both substrings on the same line rather than hard-coding one log level.
if grep -q "escalate:" "${_DAEMON_OUTPUT_FILE}" \
    && grep -q "issue #${_ISSUE_NUM}" "${_DAEMON_OUTPUT_FILE}" \
    && grep -q "kind=block" "${_DAEMON_OUTPUT_FILE}"; then
    pass "BLOCK-escalation-logged"
else
    fail "BLOCK-escalation-logged" "no 'escalate: ... issue #${_ISSUE_NUM} ... kind=block' line found in daemon output"
fi

# --- Assertion 4: at least one comment exists on the issue post-run ---
_comment_count="$(gh issue view "${_ISSUE_NUM}" \
    --repo "${_REPO_SLUG}" \
    --json comments \
    --jq '.comments | length' 2>&1)" || _comment_count="error"
if [[ "${_comment_count}" =~ ^[0-9]+$ ]] && [[ "${_comment_count}" -ge 1 ]]; then
    pass "BLOCK-comment-posted"
else
    fail "BLOCK-comment-posted" "expected >=1 comment on issue #${_ISSUE_NUM}, got: ${_comment_count}"
fi

# --- Assertion 5 (conditional): Slack block ping attempted ---
# Slack cannot be inspected directly, so this asserts on the daemon's own
# log line for the Slack POST attempt (success or failure — see header
# note). When BH_SLACK_WEBHOOK_URL is unset, Slack is not attempted at
# all and this assertion is SKIPPED, not failed (mirrors verify-recovery.sh
# G3c SKIPPED handling).
if [[ -n "${BH_SLACK_WEBHOOK_URL:-}" ]]; then
    if grep -q "escalate: Slack" "${_DAEMON_OUTPUT_FILE}" \
        && grep -q "issue #${_ISSUE_NUM}" "${_DAEMON_OUTPUT_FILE}" \
        && grep -q "kind=block" "${_DAEMON_OUTPUT_FILE}"; then
        pass "BLOCK-slack-attempted"
    else
        fail "BLOCK-slack-attempted" "BH_SLACK_WEBHOOK_URL is set but no 'escalate: Slack ... issue #${_ISSUE_NUM} ... kind=block' line found in daemon output"
    fi
else
    skipped "BLOCK-slack-attempted" "BH_SLACK_WEBHOOK_URL not set — Slack channel not exercised"
fi

echo ""

# ===========================================================================
# Summary
# ===========================================================================

echo "baton-harness: =============================="
echo "baton-harness: Block escalation verification summary"
echo "baton-harness: =============================="
echo "baton-harness:   PASSED:  ${_PASS}"
echo "baton-harness:   FAILED:  ${_FAIL}"
echo "baton-harness:   SKIPPED: ${_SKIPPED}"

if [[ ${_FAIL} -gt 0 ]]; then
    echo "baton-harness:   Failed assertions:" >&2
    for _s in "${_FAILED_SCENARIOS[@]}"; do
        echo "baton-harness:     - ${_s}" >&2
    done
    echo "" >&2
    echo "baton-harness: RESULT: FAIL" >&2
    exit 1
fi

echo "baton-harness: RESULT: PASS"
exit 0
