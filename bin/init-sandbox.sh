#!/usr/bin/env bash
# bin/init-sandbox.sh — Target sandbox repo initialisation (smoke-test ready)
#
# Prepares a throwaway sandbox GitHub repository for a bh-daemon smoke test:
#   - Creates the five required harness labels (idempotent)
#   - Creates a trivial trigger issue (agent-ready)
#   - Creates a hello-feature milestone with two DAG-ordered issues
#   - Writes a stub CI workflow to the sandbox repo and pushes it
#   - Seeds .symphony/ into the sandbox repo's .gitignore and pushes it
#
# Usage:
#   bin/init-sandbox.sh [--help|-h]
#
# Required environment variables:
#   BH_REPO_OWNER      GitHub repository owner (org or user login)
#   BH_REPO_NAME       GitHub repository name (without owner prefix)
#   BH_PROJECT_ROOT    Absolute path to the local clone of the sandbox repo
#
# WARNING: this script writes to a live GitHub repository and creates issues,
# labels, and pushes a workflow.  ONLY point it at a throwaway sandbox repo.

set -euo pipefail

# ---------------------------------------------------------------------------
# Safety banner (printed to stderr so it is always visible even when stdout
# is redirected; also printed in --help below)
# ---------------------------------------------------------------------------

print_safety_banner() {
    echo "" >&2
    echo "  *** SAFETY WARNING ***" >&2
    echo "" >&2
    echo "  This script writes to a LIVE GitHub repository:" >&2
    echo "    - Creates labels" >&2
    echo "    - Creates GitHub issues" >&2
    echo "    - Pushes a CI workflow file to the default branch" >&2
    echo "    - Seeds .symphony/ into the sandbox repo's .gitignore" >&2
    echo "" >&2
    echo "  ONLY point it at a THROWAWAY SANDBOX repo — never a real project." >&2
    echo "  Target repo is read from BH_REPO_OWNER / BH_REPO_NAME." >&2
    echo "" >&2
    echo "  Creates fresh issues each run; intended for a clean sandbox." >&2
    echo "  Issue/milestone creation may duplicate on re-run — use a fresh" >&2
    echo "  sandbox repo (or clean it manually) before repeating a smoke test." >&2
    echo "" >&2
}

# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: bin/init-sandbox.sh [--help|-h]

Prepares a throwaway sandbox GitHub repository for a bh-daemon smoke test.

Required environment variables:
  BH_REPO_OWNER      GitHub repository owner (org or user login)
  BH_REPO_NAME       GitHub repository name (without owner prefix)
  BH_PROJECT_ROOT    Absolute path to the local clone of the sandbox repo

Steps performed:
  1. Preflight checks (gh auth, git, BH_PROJECT_ROOT is a git repo)
  2. Create required labels (idempotent — dup errors tolerated)
  3. Create a trivial trigger issue (agent-ready, no milestone)
  4. Create hello-feature milestone + 2 DAG-ordered issues (B blocked_by A)
  5. Write stub CI workflow (.github/workflows/ci.yml) to BH_PROJECT_ROOT
     and push to the sandbox default branch (idempotent if unchanged)
  6. Seed .symphony/ into BH_PROJECT_ROOT/.gitignore and push
     (idempotent — skipped if the entry is already present)

Idempotency notes:
  - Labels: duplicates are tolerated; existing labels are left unchanged.
  - Workflow file: if .github/workflows/ci.yml is already identical, the
    commit+push step is skipped.
  - Issues/milestones: new ones are created each run; intended for a CLEAN
    sandbox. Use a fresh repo for repeatable smoke tests.
EOF
    echo ""
    print_safety_banner
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

# Print safety banner at startup (always)
print_safety_banner

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
    echo "  Set them before running bin/init-sandbox.sh:" >&2
    echo "    export BH_REPO_OWNER=<owner>" >&2
    echo "    export BH_REPO_NAME=<repo>" >&2
    echo "    export BH_PROJECT_ROOT=/path/to/local/sandbox/clone" >&2
    exit 1
fi

REPO_SLUG="${BH_REPO_OWNER}/${BH_REPO_NAME}"

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

echo "baton-harness: running preflight checks..."

# gh auth
if ! gh auth status &>/dev/null; then
    echo "baton-harness: error: gh auth status failed — authenticate first:" >&2
    echo "  gh auth login" >&2
    exit 1
fi
echo "baton-harness: gh auth OK"

# git available
if ! command -v git &>/dev/null; then
    echo "baton-harness: error: git not found on PATH" >&2
    exit 1
fi
echo "baton-harness: git OK"

# BH_PROJECT_ROOT exists and is a git repo
if [[ ! -d "${BH_PROJECT_ROOT}" ]]; then
    echo "baton-harness: error: BH_PROJECT_ROOT does not exist: ${BH_PROJECT_ROOT}" >&2
    exit 1
fi
if ! git -C "${BH_PROJECT_ROOT}" rev-parse --git-dir &>/dev/null; then
    echo "baton-harness: error: BH_PROJECT_ROOT is not a git repository: ${BH_PROJECT_ROOT}" >&2
    exit 1
fi
echo "baton-harness: BH_PROJECT_ROOT is a git repo: ${BH_PROJECT_ROOT}"

echo "baton-harness: target repo: ${REPO_SLUG}"

# ---------------------------------------------------------------------------
# Create required labels (idempotent — tolerate already-exists errors)
# ---------------------------------------------------------------------------

echo "baton-harness: creating required labels in ${REPO_SLUG} ..."

_create_label() {
    local name="$1"
    local color="$2"
    local output
    # Capture stderr; gh exits non-zero if the label already exists.
    if output="$(gh label create "${name}" -R "${REPO_SLUG}" --color "${color}" 2>&1)"; then
        echo "baton-harness:   label created: ${name}"
    else
        # Treat "already exists" as success; re-raise any other error.
        if echo "${output}" | grep -qi "already exists"; then
            echo "baton-harness:   label exists, skipping: ${name}"
        else
            echo "baton-harness: error creating label '${name}': ${output}" >&2
            exit 1
        fi
    fi
}

_create_label "agent-ready"       "0075ca"
_create_label "agent-done"        "0e8a16"
_create_label "blocked"           "e4e669"
_create_label "agent-in-progress" "d93f0b"
_create_label "agent-merged"      "5319e7"

echo "baton-harness: all required labels present"

# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

# Echoes the issue URL for an existing OPEN issue whose title matches exactly,
# or empty string if no match.  Uses --limit 200 and exact-title jq filter to
# avoid false positives from gh's substring search.
# NOTE: we use a process-substitution + read to avoid set -o pipefail tripping
# on the head -n1 SIGPIPE that can occur when gh outputs multiple lines.
_find_open_issue_url() {
    local title="$1"
    local result
    result="$(gh issue list -R "${REPO_SLUG}" --state open --limit 200 \
        --json number,title,url \
        --jq ".[] | select(.title == \"${title}\") | .url")" || true
    printf '%s' "${result}" | head -n1 || true
}

# ---------------------------------------------------------------------------
# Create trivial trigger issue (single agent-ready, no milestone)
# ---------------------------------------------------------------------------

echo "baton-harness: creating trivial trigger issue ..."

_trivial_title="add a greet() function"
TRIVIAL_ISSUE_URL="$(_find_open_issue_url "${_trivial_title}")" || true
if [[ -n "${TRIVIAL_ISSUE_URL}" ]]; then
    echo "baton-harness:   trivial issue exists, reusing: ${TRIVIAL_ISSUE_URL}"
else
    TRIVIAL_ISSUE_URL="$(gh issue create \
        --repo "${REPO_SLUG}" \
        --title "${_trivial_title}" \
        --body "Add a Python file (greet.py) with a greet() function that prints 'hello'." \
        --label "agent-ready")"
    echo "baton-harness:   trivial issue created: ${TRIVIAL_ISSUE_URL}"
fi

# ---------------------------------------------------------------------------
# Create DAG milestone and two linked issues
# ---------------------------------------------------------------------------

MILESTONE_TITLE="hello-feature"
echo "baton-harness: creating hello-feature milestone ..."

# Idempotent: look up existing milestone by title first; create only if absent.
# Assign via intermediate variable to avoid set -o pipefail + head -n1 SIGPIPE.
_ms_lookup="$(gh api "repos/${REPO_SLUG}/milestones?state=all" --paginate \
    --jq ".[] | select(.title == \"${MILESTONE_TITLE}\") | .number")" || true
MILESTONE_NUMBER="$(printf '%s' "${_ms_lookup}" | head -n1 || true)"
if [[ -z "${MILESTONE_NUMBER}" ]]; then
    MILESTONE_NUMBER="$(gh api "repos/${REPO_SLUG}/milestones" \
        --method POST \
        -f title="${MILESTONE_TITLE}" \
        --jq '.number')"
    echo "baton-harness:   milestone created: ${MILESTONE_TITLE} (#${MILESTONE_NUMBER})"
else
    echo "baton-harness:   milestone exists, reusing: ${MILESTONE_TITLE} (#${MILESTONE_NUMBER})"
fi
if [[ -z "${MILESTONE_NUMBER}" || ! "${MILESTONE_NUMBER}" =~ ^[0-9]+$ ]]; then
    echo "baton-harness: error: failed to extract milestone number (got: '${MILESTONE_NUMBER}')" >&2
    exit 1
fi

# Issue A (prerequisite — no blocker)
echo "baton-harness: creating issue A (add hello() function) ..."
_issue_a_title="add hello() function"
ISSUE_A_URL="$(_find_open_issue_url "${_issue_a_title}")" || true
if [[ -n "${ISSUE_A_URL}" ]]; then
    echo "baton-harness:   issue A exists, reusing: ${ISSUE_A_URL}"
else
    ISSUE_A_URL="$(gh issue create \
        --repo "${REPO_SLUG}" \
        --title "${_issue_a_title}" \
        --body "Add hello.py with a hello() function." \
        --label "agent-ready" \
        --milestone "${MILESTONE_TITLE}")"
    echo "baton-harness:   issue A created: ${ISSUE_A_URL}"
fi

# Extract issue number from URL (last path segment)
ISSUE_A_NUMBER="${ISSUE_A_URL##*/}"
if [[ -z "${ISSUE_A_NUMBER}" || ! "${ISSUE_A_NUMBER}" =~ ^[0-9]+$ ]]; then
    echo "baton-harness: error: failed to extract issue A number from URL (got: '${ISSUE_A_NUMBER}')" >&2
    exit 1
fi
echo "baton-harness:   issue A: #${ISSUE_A_NUMBER} — ${ISSUE_A_URL}"

# Issue B (blocked by A)
echo "baton-harness: creating issue B (add tests for hello()) ..."
_issue_b_title="add tests for hello()"
ISSUE_B_URL="$(_find_open_issue_url "${_issue_b_title}")" || true
if [[ -n "${ISSUE_B_URL}" ]]; then
    echo "baton-harness:   issue B exists, reusing: ${ISSUE_B_URL}"
else
    ISSUE_B_URL="$(gh issue create \
        --repo "${REPO_SLUG}" \
        --title "${_issue_b_title}" \
        --body "Add pytest tests for the hello() function from the prior issue." \
        --label "agent-ready" \
        --milestone "${MILESTONE_TITLE}")"
    echo "baton-harness:   issue B created: ${ISSUE_B_URL}"
fi

ISSUE_B_NUMBER="${ISSUE_B_URL##*/}"
if [[ -z "${ISSUE_B_NUMBER}" || ! "${ISSUE_B_NUMBER}" =~ ^[0-9]+$ ]]; then
    echo "baton-harness: error: failed to extract issue B number from URL (got: '${ISSUE_B_NUMBER}')" >&2
    exit 1
fi
echo "baton-harness:   issue B: #${ISSUE_B_NUMBER} — ${ISSUE_B_URL}"

# Fetch database IDs (the /dependencies API requires database IDs, not issue numbers)
echo "baton-harness: fetching database IDs for dependency wiring ..."

ISSUE_A_DB_ID="$(gh api "repos/${REPO_SLUG}/issues/${ISSUE_A_NUMBER}" --jq '.id')"
if [[ -z "${ISSUE_A_DB_ID}" || ! "${ISSUE_A_DB_ID}" =~ ^[0-9]+$ ]]; then
    echo "baton-harness: error: failed to extract issue A database ID (got: '${ISSUE_A_DB_ID}')" >&2
    exit 1
fi
ISSUE_B_DB_ID="$(gh api "repos/${REPO_SLUG}/issues/${ISSUE_B_NUMBER}" --jq '.id')"
if [[ -z "${ISSUE_B_DB_ID}" || ! "${ISSUE_B_DB_ID}" =~ ^[0-9]+$ ]]; then
    echo "baton-harness: error: failed to extract issue B database ID (got: '${ISSUE_B_DB_ID}')" >&2
    exit 1
fi

echo "baton-harness:   issue A database ID: ${ISSUE_A_DB_ID}"
echo "baton-harness:   issue B database ID: ${ISSUE_B_DB_ID}"

# Wire B blocked_by A (idempotent — tolerate 422 if link already exists)
echo "baton-harness: wiring dependency: issue B blocked_by issue A ..."
_dep_output=""
_dep_rc=0
_dep_output="$(gh api "repos/${REPO_SLUG}/issues/${ISSUE_B_NUMBER}/dependencies/blocked_by" \
    --method POST \
    -f "issue_id=${ISSUE_A_DB_ID}" 2>&1)" || _dep_rc=$?
if [[ ${_dep_rc} -ne 0 ]]; then
    # Tolerate 422 (dependency already exists); any other failure is fatal.
    if printf '%s' "${_dep_output}" | grep -q "422\|already exists\|already blocked"; then
        echo "baton-harness:   dependency already present, skipping"
    else
        echo "baton-harness: error wiring dependency: ${_dep_output}" >&2
        exit 1
    fi
else
    echo "baton-harness:   dependency wired (B blocked_by A)"
fi

# ---------------------------------------------------------------------------
# Write stub CI workflow to the sandbox repo
#
# Job names MUST match REQUIRED_CHECKS in src/baton_harness/chain/merge.py:
#   - "Lint (ruff)"
#   - "Test (pytest)"
#   - "Type check (mypy)"
# ---------------------------------------------------------------------------

echo "baton-harness: writing stub CI workflow to sandbox repo ..."

# Resolve default branch of the sandbox repo
DEFAULT_BRANCH="$(gh repo view "${REPO_SLUG}" --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || true)"
if [[ -z "${DEFAULT_BRANCH}" || "${DEFAULT_BRANCH}" == "null" ]]; then
    # Empty repo (no default-branch ref yet) — use the local clone's current (possibly unborn) branch.
    DEFAULT_BRANCH="$(git -C "${BH_PROJECT_ROOT}" symbolic-ref --short HEAD 2>/dev/null || true)"
fi
if [[ -z "${DEFAULT_BRANCH}" ]]; then
    DEFAULT_BRANCH="main"
fi
echo "baton-harness:   target branch: ${DEFAULT_BRANCH}"

# Guard: abort if the local clone is in detached-HEAD state
# symbolic-ref -q HEAD: exit 0 on a normal OR unborn branch; non-zero only on detached HEAD.
if ! git -C "${BH_PROJECT_ROOT}" symbolic-ref -q HEAD >/dev/null 2>&1; then
    echo "baton-harness: error: BH_PROJECT_ROOT is in detached-HEAD state — check out a branch before running this script" >&2
    echo "  Example: git -C \"${BH_PROJECT_ROOT}\" checkout ${DEFAULT_BRANCH}" >&2
    exit 1
fi

WORKFLOW_DIR="${BH_PROJECT_ROOT}/.github/workflows"
WORKFLOW_FILE="${WORKFLOW_DIR}/ci.yml"

# Build the expected workflow content
read -r -d '' WORKFLOW_CONTENT <<'YAML' || true
# Stub CI workflow for bh-daemon smoke testing.
# Job names must match REQUIRED_CHECKS in baton_harness/chain/merge.py exactly.
# Each job exits 0 — sufficient for the CI gate to pass.
name: CI

on:
  pull_request:
  push:

jobs:
  lint:
    name: "Lint (ruff)"
    runs-on: ubuntu-latest
    steps:
      - run: "true"

  test:
    name: "Test (pytest)"
    runs-on: ubuntu-latest
    steps:
      - run: "true"

  typecheck:
    name: "Type check (mypy)"
    runs-on: ubuntu-latest
    steps:
      - run: "true"
YAML

mkdir -p "${WORKFLOW_DIR}"
printf '%s\n' "${WORKFLOW_CONTENT}" > "${WORKFLOW_FILE}"

git -C "${BH_PROJECT_ROOT}" add ".github/workflows/ci.yml"
if git -C "${BH_PROJECT_ROOT}" diff --cached --quiet -- ".github/workflows/ci.yml"; then
    echo "baton-harness:   ci.yml unchanged, skipping commit"
else
    git -C "${BH_PROJECT_ROOT}" commit -m "chore: add stub CI workflow for bh-daemon smoke test" -- .github/workflows/ci.yml  # -- <path>: never sweep a pre-staged index into the seed commit
    git -C "${BH_PROJECT_ROOT}" push -u origin HEAD:"${DEFAULT_BRANCH}"
    echo "baton-harness:   ci.yml committed and pushed to sandbox"
fi

# ---------------------------------------------------------------------------
# Seed .symphony/ into the sandbox repo's .gitignore
#
# The daemon writes run state to <repo_root>/.symphony/state.json; without
# this entry, `gh pr create` emits "Warning: 1 uncommitted change".
# ---------------------------------------------------------------------------

echo "baton-harness: seeding .symphony/ into sandbox .gitignore ..."

GITIGNORE_FILE="${BH_PROJECT_ROOT}/.gitignore"
GITIGNORE_SEEDED=0

if [[ ! -f "${GITIGNORE_FILE}" ]]; then
    printf '.symphony/\n' > "${GITIGNORE_FILE}"
    echo "baton-harness:   .gitignore created with .symphony/ entry"
    GITIGNORE_SEEDED=1
elif grep -qxF '.symphony/' "${GITIGNORE_FILE}"; then
    echo "baton-harness:   .symphony/ already in .gitignore, skipping"
else
    if [[ -s "${GITIGNORE_FILE}" && -n "$(tail -c1 "${GITIGNORE_FILE}")" ]]; then
        printf '\n' >> "${GITIGNORE_FILE}"
    fi
    printf '%s\n' '.symphony/' >> "${GITIGNORE_FILE}"
    echo "baton-harness:   .symphony/ appended to .gitignore"
    GITIGNORE_SEEDED=1
fi

# Also seed .baton-harness/ (runlog dir) into the sandbox .gitignore.
if grep -qxF '.baton-harness/' "${GITIGNORE_FILE}"; then
    echo "baton-harness:   .baton-harness/ already in .gitignore, skipping"
else
    if [[ -s "${GITIGNORE_FILE}" && -n "$(tail -c1 "${GITIGNORE_FILE}")" ]]; then
        printf '\n' >> "${GITIGNORE_FILE}"
    fi
    printf '%s\n' '.baton-harness/' >> "${GITIGNORE_FILE}"
    echo "baton-harness:   .baton-harness/ appended to .gitignore"
    GITIGNORE_SEEDED=1
fi

if [[ "${GITIGNORE_SEEDED}" == 1 ]]; then
    git -C "${BH_PROJECT_ROOT}" add ".gitignore"
    if git -C "${BH_PROJECT_ROOT}" diff --cached --quiet -- ".gitignore"; then
        echo "baton-harness:   .gitignore unchanged, skipping commit"
    else
        git -C "${BH_PROJECT_ROOT}" commit -m "chore: gitignore .symphony/ daemon state" -- .gitignore  # -- <path>: never sweep a pre-staged index into the seed commit
        git -C "${BH_PROJECT_ROOT}" push -u origin HEAD:"${DEFAULT_BRANCH}"
        echo "baton-harness:   .gitignore committed and pushed to sandbox"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "baton-harness: sandbox initialisation complete."
echo ""
echo "  Sandbox repo:    ${REPO_SLUG}"
echo "  Local clone:     ${BH_PROJECT_ROOT}"
echo ""
echo "  Created:"
echo "    - 5 required labels"
echo "    - Trivial trigger issue:  ${TRIVIAL_ISSUE_URL}"
echo "    - Milestone 'hello-feature' (#${MILESTONE_NUMBER})"
echo "    - Issue A:  ${ISSUE_A_URL}"
echo "    - Issue B:  ${ISSUE_B_URL}  (blocked_by A)"
echo "    - Stub CI workflow: .github/workflows/ci.yml"
echo "    - .symphony/ entry in: .gitignore"
echo ""
echo "  Next steps:"
echo "    1. Set BH_REPO_OWNER, BH_REPO_NAME, BH_PROJECT_ROOT in your shell"
echo "    2. Run:  bin/run-daemon.sh --once"
echo ""
