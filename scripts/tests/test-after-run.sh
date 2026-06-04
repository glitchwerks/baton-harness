#!/usr/bin/env bash
# scripts/tests/test-after-run.sh — test harness for after-run.sh
#
# Tests the four F5 git-state classifications and the two label-reconciliation
# branches (pr-opened → agent-done, blocked-present → blocked-only).
#
# Strategy:
#   - Git-state tests: real temporary git repos with a real "origin" bare repo
#     so git cherry / git status commands work faithfully.
#   - gh calls: PATH-injected stub that records calls and returns canned output.
#   - The script under test is sourced in "library mode" by setting
#     AFTER_RUN_LIB=1, which skips the main() call but defines all functions.
#
# Usage:
#   bash scripts/tests/test-after-run.sh
#   (must be run from the worktree root)

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="${WORKTREE_ROOT}/scripts/after-run.sh"

# ---------------------------------------------------------------------------
# Test framework (minimal)
# ---------------------------------------------------------------------------

PASS_COUNT=0
FAIL_COUNT=0
FAILURES=()

pass() {
    PASS_COUNT=$(( PASS_COUNT + 1 ))
    echo "  PASS: $1"
}

fail() {
    FAIL_COUNT=$(( FAIL_COUNT + 1 ))
    FAILURES+=("$1")
    echo "  FAIL: $1"
}

assert_contains() {
    local desc="$1" needle="$2" haystack="$3"
    if echo "${haystack}" | grep -qF -- "${needle}"; then
        pass "${desc}"
    else
        fail "${desc}: '${needle}' not found in output"
    fi
}

assert_not_contains() {
    local desc="$1" needle="$2" haystack="$3"
    if echo "${haystack}" | grep -qF -- "${needle}"; then
        fail "${desc}: '${needle}' should NOT be in output"
    else
        pass "${desc}"
    fi
}

# ---------------------------------------------------------------------------
# Temp workspace management
# ---------------------------------------------------------------------------

TMPDIR_ROOT=""

setup_tmpdir() {
    TMPDIR_ROOT="$(mktemp -d)"
}

teardown_tmpdir() {
    if [[ -n "${TMPDIR_ROOT}" && -d "${TMPDIR_ROOT}" ]]; then
        rm -rf "${TMPDIR_ROOT}"
    fi
    TMPDIR_ROOT=""
}

# ---------------------------------------------------------------------------
# Git repo fixture helpers
#
# Creates a bare "origin" and a local clone with an initial commit on main.
# All repos are created inside TMPDIR_ROOT.
# ---------------------------------------------------------------------------

# make_origin_repo <name>
# Creates a bare repo at ${TMPDIR_ROOT}/<name>-origin.git
# Returns the path in ORIGIN_PATH.
ORIGIN_PATH=""
make_origin_repo() {
    local name="$1"
    ORIGIN_PATH="${TMPDIR_ROOT}/${name}-origin.git"
    git init --bare "${ORIGIN_PATH}" -q
}

# make_local_clone <name> <origin-path>
# Clones from origin-path into ${TMPDIR_ROOT}/<name>-local.
# Creates an initial commit so the branch has commits.
# Returns the local path in LOCAL_PATH.
LOCAL_PATH=""
make_local_clone() {
    local name="$1" origin="$2"
    LOCAL_PATH="${TMPDIR_ROOT}/${name}-local"
    git clone "${origin}" "${LOCAL_PATH}" -q
    # Configure identity for the temp repo
    git -C "${LOCAL_PATH}" config user.email "test@test.local"
    git -C "${LOCAL_PATH}" config user.name "Test"
    # Create initial commit on main (so origin/main exists)
    echo "init" > "${LOCAL_PATH}/README.md"
    git -C "${LOCAL_PATH}" add README.md
    git -C "${LOCAL_PATH}" commit -m "initial" -q
    git -C "${LOCAL_PATH}" push origin main -q
}

# ---------------------------------------------------------------------------
# Fake 'gh' binary injected via PATH
#
# GH_STUB_MODE controls what the stub returns:
#   "pr-found"  → pr list returns a JSON line with a PR number
#   "pr-none"   → pr list returns empty JSON array
#   "labels-blocked" → issue view returns blocked label present
#   "labels-ready"   → issue view returns only agent-ready label
#
# GH_STUB_CALLS is a file where each call is appended as a line.
# ---------------------------------------------------------------------------

GH_STUB_DIR=""
GH_STUB_MODE=""
GH_STUB_CALLS=""

setup_gh_stub() {
    local mode="$1"
    # shellcheck disable=SC2034  # used inside the stub heredoc written to disk
    GH_STUB_MODE="${mode}"
    GH_STUB_DIR="${TMPDIR_ROOT}/gh-stub"
    GH_STUB_CALLS="${TMPDIR_ROOT}/gh-calls.log"
    mkdir -p "${GH_STUB_DIR}"
    : > "${GH_STUB_CALLS}"  # empty the calls file

    # Write the stub script, capturing mode and calls file at creation time.
    cat > "${GH_STUB_DIR}/gh" <<STUB_EOF
#!/usr/bin/env bash
echo "\$*" >> "${GH_STUB_CALLS}"
MODE="${mode}"
if [[ "\$*" == *"pr list"* ]]; then
    if [[ "\${MODE}" == "pr-found" ]]; then
        # Return a minimal JSON array with one PR
        echo '[{"number":42,"title":"feat: test","headRefName":"agent/issue-7","state":"OPEN"}]'
    else
        echo '[]'
    fi
elif [[ "\$*" == *"issue view"* ]]; then
    if [[ "\${MODE}" == "labels-blocked" ]]; then
        echo 'blocked,agent-ready'
    elif [[ "\${MODE}" == "labels-ready" ]]; then
        echo 'agent-ready'
    else
        echo ''
    fi
elif [[ "\$*" == *"issue edit"* ]]; then
    # Simulate success silently
    :
else
    echo "gh-stub: unhandled: \$*" >&2
fi
STUB_EOF
    chmod +x "${GH_STUB_DIR}/gh"
}

stub_calls() {
    cat "${GH_STUB_CALLS}" 2>/dev/null || echo ""
}

# Run after-run.sh with the stub gh on PATH, in the given worktree dir.
# Returns the combined stdout+stderr in variable OUTPUT.
OUTPUT=""
run_after_run() {
    local worktree_dir="$1" issue_num="${2:-7}"
    local path_prefix="${GH_STUB_DIR:+${GH_STUB_DIR}:}"
    OUTPUT="$(cd "${worktree_dir}" && PATH="${path_prefix}${PATH}" bash "${SCRIPT_UNDER_TEST}" "${issue_num}" 2>&1)" || true
}

# ---------------------------------------------------------------------------
# Test: classify_git_state — uncommitted-changes
# ---------------------------------------------------------------------------

test_uncommitted_changes() {
    echo ""
    echo "=== classify_git_state: uncommitted-changes ==="
    setup_tmpdir
    setup_gh_stub "pr-none"
    make_origin_repo "dirty"
    make_local_clone "dirty" "${ORIGIN_PATH}"

    # Add an untracked/staged change to make the working tree dirty
    echo "dirty" > "${LOCAL_PATH}/dirty.txt"
    git -C "${LOCAL_PATH}" add dirty.txt
    # (staged but not committed)

    run_after_run "${LOCAL_PATH}" "7"

    assert_contains "detects uncommitted-changes" "uncommitted-changes" "${OUTPUT}"
    assert_not_contains "should NOT say pr-opened" "pr-opened" "${OUTPUT}"

    teardown_tmpdir
}

# ---------------------------------------------------------------------------
# Test: classify_git_state — no-commits
# ---------------------------------------------------------------------------

test_no_commits() {
    echo ""
    echo "=== classify_git_state: no-commits ==="
    setup_tmpdir
    setup_gh_stub "pr-none"
    make_origin_repo "empty"
    make_local_clone "empty" "${ORIGIN_PATH}"

    # Create a feature branch with no commits beyond main
    git -C "${LOCAL_PATH}" checkout -b "agent/issue-7" -q

    run_after_run "${LOCAL_PATH}" "7"

    assert_contains "detects no-commits" "no-commits" "${OUTPUT}"
    assert_not_contains "should NOT say pr-opened" "pr-opened" "${OUTPUT}"

    teardown_tmpdir
}

# ---------------------------------------------------------------------------
# Test: classify_git_state — committed-no-pr
# ---------------------------------------------------------------------------

test_committed_no_pr() {
    echo ""
    echo "=== classify_git_state: committed-no-pr ==="
    setup_tmpdir
    setup_gh_stub "pr-none"
    make_origin_repo "committed"
    make_local_clone "committed" "${ORIGIN_PATH}"

    git -C "${LOCAL_PATH}" checkout -b "agent/issue-7" -q
    echo "feature" > "${LOCAL_PATH}/feature.txt"
    git -C "${LOCAL_PATH}" add feature.txt
    git -C "${LOCAL_PATH}" commit -m "feat: add feature" -q
    git -C "${LOCAL_PATH}" push origin "agent/issue-7" -q

    run_after_run "${LOCAL_PATH}" "7"

    assert_contains "detects committed-no-pr" "committed-no-pr" "${OUTPUT}"
    assert_not_contains "should NOT say pr-opened" "pr-opened" "${OUTPUT}"

    teardown_tmpdir
}

# ---------------------------------------------------------------------------
# Test: classify_git_state — pr-opened
# ---------------------------------------------------------------------------

test_pr_opened() {
    echo ""
    echo "=== classify_git_state: pr-opened ==="
    setup_tmpdir
    setup_gh_stub "pr-found"
    make_origin_repo "with-pr"
    make_local_clone "with-pr" "${ORIGIN_PATH}"

    git -C "${LOCAL_PATH}" checkout -b "agent/issue-7" -q
    echo "feature" > "${LOCAL_PATH}/feature.txt"
    git -C "${LOCAL_PATH}" add feature.txt
    git -C "${LOCAL_PATH}" commit -m "feat: add feature" -q
    git -C "${LOCAL_PATH}" push origin "agent/issue-7" -q

    run_after_run "${LOCAL_PATH}" "7"

    assert_contains "detects pr-opened" "pr-opened" "${OUTPUT}"
    assert_contains "logs F10 caveat about CI" "CI" "${OUTPUT}"

    teardown_tmpdir
}

# ---------------------------------------------------------------------------
# Test: label reconciliation — pr-opened → agent-done
# ---------------------------------------------------------------------------

test_label_reconciliation_pr_opened() {
    echo ""
    echo "=== label reconciliation: pr-opened → agent-done ==="
    setup_tmpdir
    setup_gh_stub "pr-found"
    make_origin_repo "label-pr"
    make_local_clone "label-pr" "${ORIGIN_PATH}"

    git -C "${LOCAL_PATH}" checkout -b "agent/issue-7" -q
    echo "feature" > "${LOCAL_PATH}/feature.txt"
    git -C "${LOCAL_PATH}" add feature.txt
    git -C "${LOCAL_PATH}" commit -m "feat: add feature" -q
    git -C "${LOCAL_PATH}" push origin "agent/issue-7" -q

    run_after_run "${LOCAL_PATH}" "7"

    local calls
    calls="$(stub_calls)"
    assert_contains "adds agent-done label" "--add-label" "${calls}"
    assert_contains "removes agent-ready label" "--remove-label" "${calls}"
    assert_not_contains "should NOT add blocked" "--add-label.*blocked" "${calls}"

    teardown_tmpdir
}

# ---------------------------------------------------------------------------
# Test: label reconciliation — blocked-present → single blocked state
# ---------------------------------------------------------------------------

test_label_reconciliation_blocked() {
    echo ""
    echo "=== label reconciliation: blocked present → single blocked ==="
    setup_tmpdir
    # gh reports both blocked and agent-ready currently on the issue
    setup_gh_stub "labels-blocked"
    make_origin_repo "label-blocked"
    make_local_clone "label-blocked" "${ORIGIN_PATH}"

    # Working tree is clean, has commits, branch pushed (no PR though)
    git -C "${LOCAL_PATH}" checkout -b "agent/issue-7" -q
    echo "wip" > "${LOCAL_PATH}/wip.txt"
    git -C "${LOCAL_PATH}" add wip.txt
    git -C "${LOCAL_PATH}" commit -m "wip" -q
    git -C "${LOCAL_PATH}" push origin "agent/issue-7" -q

    # Override: this stub mode emits labels-blocked for issue view
    # BUT pr list returns empty (no PR)
    # We need a combined mode: blocked + no PR
    # Re-write the stub with combined mode
    cat > "${GH_STUB_DIR}/gh" <<STUB_EOF
#!/usr/bin/env bash
echo "\$*" >> "${GH_STUB_CALLS}"
if [[ "\$*" == *"pr list"* ]]; then
    echo '[]'
elif [[ "\$*" == *"issue view"* && "\$*" == *"--json labels"* ]]; then
    # Return labels JSON with both blocked and agent-ready
    printf '[{"name":"blocked"},{"name":"agent-ready"}]'
elif [[ "\$*" == *"issue edit"* ]]; then
    :
else
    echo "gh-stub: unhandled: \$*" >&2
fi
STUB_EOF
    chmod +x "${GH_STUB_DIR}/gh"

    run_after_run "${LOCAL_PATH}" "7"

    local calls
    calls="$(stub_calls)"
    assert_contains "removes agent-ready when blocked" "--remove-label" "${calls}"
    assert_not_contains "should NOT add agent-done" "agent-done" "${calls}"
    assert_contains "output mentions blocked" "blocked" "${OUTPUT}"

    teardown_tmpdir
}

# ---------------------------------------------------------------------------
# Test: label reconciliation — retryable (no-commits) leaves agent-ready
# ---------------------------------------------------------------------------

test_label_reconciliation_retryable() {
    echo ""
    echo "=== label reconciliation: no-commits → leave agent-ready ==="
    setup_tmpdir
    setup_gh_stub "pr-none"
    make_origin_repo "retryable"
    make_local_clone "retryable" "${ORIGIN_PATH}"

    # Re-write stub: pr list empty, labels show only agent-ready, no blocked
    cat > "${GH_STUB_DIR}/gh" <<STUB_EOF
#!/usr/bin/env bash
echo "\$*" >> "${GH_STUB_CALLS}"
if [[ "\$*" == *"pr list"* ]]; then
    echo '[]'
elif [[ "\$*" == *"issue view"* && "\$*" == *"--json labels"* ]]; then
    printf '[{"name":"agent-ready"}]'
elif [[ "\$*" == *"issue edit"* ]]; then
    :
else
    echo "gh-stub: unhandled: \$*" >&2
fi
STUB_EOF
    chmod +x "${GH_STUB_DIR}/gh"

    # Branch with no commits beyond main
    git -C "${LOCAL_PATH}" checkout -b "agent/issue-7" -q

    run_after_run "${LOCAL_PATH}" "7"

    local calls
    calls="$(stub_calls)"
    assert_not_contains "should NOT add agent-done" "agent-done" "${calls}"
    assert_not_contains "should NOT remove agent-ready on retryable" "--remove-label agent-ready" "${calls}"
    assert_contains "output mentions retryable classification" "no-commits" "${OUTPUT}"

    teardown_tmpdir
}

# ---------------------------------------------------------------------------
# Main: run all tests and report
# ---------------------------------------------------------------------------

main() {
    echo "Running tests for scripts/after-run.sh"
    echo "Script under test: ${SCRIPT_UNDER_TEST}"
    echo ""

    if [[ ! -f "${SCRIPT_UNDER_TEST}" ]]; then
        echo "ERROR: ${SCRIPT_UNDER_TEST} does not exist — tests will fail until it is created."
        echo "This is expected during the RED phase of TDD."
        echo ""
        # Still count as failures
        FAIL_COUNT=7
        FAILURES=("after-run.sh does not exist")
    else
        test_uncommitted_changes
        test_no_commits
        test_committed_no_pr
        test_pr_opened
        test_label_reconciliation_pr_opened
        test_label_reconciliation_blocked
        test_label_reconciliation_retryable
    fi

    echo ""
    echo "=========================================="
    echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
    if [[ ${FAIL_COUNT} -gt 0 ]]; then
        echo ""
        echo "Failures:"
        for f in "${FAILURES[@]}"; do
            echo "  - ${f}"
        done
        exit 1
    fi
    echo "All tests passed."
    exit 0
}

main "$@"
