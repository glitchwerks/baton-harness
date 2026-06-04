#!/usr/bin/env bash
# scripts/after-run.sh — outcome router (harness-design.md §4.2, §5)
#
# Classifies what a Baton agent run produced and reconciles GitHub labels to a
# single human-facing state.
#
# Runs inside the TARGET PROJECT REPO's worktree (cwd = worktree checkout).
# git and gh commands run from cwd → target the correct project repo + branch.
# Labels mutated are runtime labels on the *project's* issues, not this harness.
#
# Usage:
#   after-run.sh [issue-number]
#   (issue-number defaults to basename of cwd — spike F2 pattern)
#
# Exit codes:
#   0 — classification and label reconciliation completed (even if retryable)
#   1 — fatal error (git/gh call failed unexpectedly)
#
# References: harness-design.md §5, spike-findings F3/F5/F8/F10, issue #3.

set -euo pipefail

# ---------------------------------------------------------------------------
# Label constants (§5 vocabulary — change here, nowhere else)
# ---------------------------------------------------------------------------

LABEL_READY="agent-ready"
LABEL_DONE="agent-done"
LABEL_BLOCKED="blocked"

# ---------------------------------------------------------------------------
# Issue number (F2: derive from worktree dirname; accept override as $1)
# ---------------------------------------------------------------------------

ISSUE="${1:-$(basename "$PWD")}"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

log() {
    echo "[after-run #${ISSUE}] $*"
}

err() {
    echo "[after-run #${ISSUE}] ERROR: $*" >&2
}

# ---------------------------------------------------------------------------
# Git-state classification helpers (F5)
#
# States (exactly one selected):
#   uncommitted-changes  — working tree has staged/modified but uncommitted changes
#   no-commits           — branch has no commits beyond origin/main
#   committed-no-pr      — commits exist on branch but no open PR
#   pr-opened            — an open/draft PR exists for this branch
# ---------------------------------------------------------------------------

# has_uncommitted_changes: returns 0 if there are staged or modified-tracked files.
has_uncommitted_changes() {
    # git status --porcelain outputs one line per changed file.
    # Untracked files (prefix '??') are not committed but also not a partial commit;
    # we focus on staged (index) and modified-tracked (worktree) changes.
    # Using --untracked-files=no to exclude bare untracked files — they can't be
    # "committed" without being staged first and don't indicate a partial commit.
    local porcelain
    porcelain="$(git status --porcelain --untracked-files=no)"
    [[ -n "${porcelain}" ]]
}

# has_commits_ahead: returns 0 if the current branch has commits not on origin/main.
# Uses git cherry to compare against origin/main.
has_commits_ahead() {
    local cherry_out
    # git cherry lists commits unique to HEAD vs upstream.
    # '+' prefix = commit in HEAD not in origin/main.
    # '-' prefix = patch equivalent already in origin/main (skip).
    cherry_out="$(git cherry origin/main HEAD 2>/dev/null)" || return 1
    echo "${cherry_out}" | grep -q '^+'
}

# query_open_pr: writes PR number to stdout if an open/draft PR exists for
# the current branch, empty string otherwise.
# Uses gh pr list --head <branch> --state open and accepts draft PRs too.
query_open_pr() {
    local branch
    branch="$(git branch --show-current)"
    # gh pr list --json number returns a JSON array; non-empty array → PR found.
    local pr_json
    pr_json="$(gh pr list \
        --head "${branch}" \
        --state open \
        --json number \
        2>/dev/null)" || { err "gh pr list failed for branch '${branch}'"; return 1; }
    # Output the PR number if found (first element), empty string if not.
    if echo "${pr_json}" | grep -q '"number"'; then
        echo "${pr_json}" | grep -o '"number":[0-9]*' | head -1 | grep -o '[0-9]*'
    else
        echo ""
    fi
}

# classify_git_state: sets GIT_STATE to one of the four F5 terminal states.
GIT_STATE=""
classify_git_state() {
    log "classifying git state..."

    # 1. Check for uncommitted changes first — they indicate an incomplete run.
    if has_uncommitted_changes; then
        GIT_STATE="uncommitted-changes"
        log "git state: ${GIT_STATE} (staged or modified-tracked files present)"
        return 0
    fi

    # 2. Check for commits ahead of origin/main.
    if ! has_commits_ahead; then
        GIT_STATE="no-commits"
        log "git state: ${GIT_STATE} (branch has no commits beyond origin/main)"
        return 0
    fi

    # 3. Branch has commits — check for an open PR.
    log "branch has commits ahead of origin/main; querying GitHub for open PR..."
    local pr_num
    pr_num="$(query_open_pr)"

    if [[ -n "${pr_num}" ]]; then
        GIT_STATE="pr-opened"
        log "git state: ${GIT_STATE} (PR #${pr_num} is open for this branch)"
        # NOTE (F10): agent-done means a PR exists, NOT that CI is green.
        # The human verifies CI at review (pilot). CI-status gating of agent-done
        # is a later-phase concern — do NOT query CI here.
        log "CAVEAT (F10): PR exists but CI status is NOT checked — human verifies CI at review (pilot phase)"
    else
        GIT_STATE="committed-no-pr"
        log "git state: ${GIT_STATE} (commits exist on branch but no open PR found)"
    fi
}

# ---------------------------------------------------------------------------
# Label reconciliation (harness-design.md §5, F3, H1)
#
# Enforces the single-state invariant: exactly one of agent-ready, agent-done,
# blocked is present on the issue at the end of this hook.
#
# Block detection: query current labels on the issue; if 'blocked' is present,
# the block path applies regardless of git state.
#
# IMPORTANT: do NOT swallow label-edit errors with 2>/dev/null || true.
# Silent swallowing is the documented H1 root cause (spike-findings.md H1).
# ---------------------------------------------------------------------------

# get_issue_labels: writes current labels as a JSON array to stdout.
get_issue_labels() {
    gh issue view "${ISSUE}" --json labels --jq '.labels[].name' 2>/dev/null || {
        err "gh issue view failed for issue #${ISSUE}"
        return 1
    }
}

# issue_has_label <label>: returns 0 if the given label is on the issue.
issue_has_label() {
    local label="$1"
    local labels_out
    labels_out="$(get_issue_labels)"
    echo "${labels_out}" | grep -qF "${label}"
}

# add_label <label>: adds a label to the issue. Errors are NOT swallowed (H1 fix).
add_label() {
    local label="$1"
    log "adding label '${label}' to issue #${ISSUE}"
    gh issue edit "${ISSUE}" --add-label "${label}"
    log "label '${label}' added"
}

# remove_label <label>: removes a label from the issue. Errors are NOT swallowed (H1 fix).
remove_label() {
    local label="$1"
    log "removing label '${label}' from issue #${ISSUE}"
    gh issue edit "${ISSUE}" --remove-label "${label}"
    log "label '${label}' removed"
}

# reconcile_labels: applies the §5 state-machine transition based on GIT_STATE
# and the current label state of the issue.
reconcile_labels() {
    log "reconciling labels for issue #${ISSUE} (git state: ${GIT_STATE})"

    # --- Block detection (highest priority) ---
    # Check if the issue currently carries the 'blocked' label.
    # The agent applies 'blocked' mid-run via the WORKFLOW.md prompt (built in #5).
    # When blocked: ensure single state — 'blocked' present, 'agent-ready' removed.
    if issue_has_label "${LABEL_BLOCKED}"; then
        log "issue #${ISSUE} has '${LABEL_BLOCKED}' label — applying block reconciliation"
        remove_label "${LABEL_READY}"
        # NOTE (#4): future issue #4 handles the H1/continuation-retry interaction —
        # making a block terminal (stopping Baton's continuation retries from
        # re-adding labels) belongs in #4 after the block-cost test in #6.
        log "label state: ${LABEL_BLOCKED} present, ${LABEL_READY} removed — single blocked state enforced"
        return 0
    fi

    # --- State-based transition ---
    case "${GIT_STATE}" in
        pr-opened)
            # Successful outcome: transition to agent-done.
            # Remove agent-ready; add agent-done.
            # F10 caveat already logged in classify_git_state.
            log "transitioning issue #${ISSUE}: ${LABEL_READY} → ${LABEL_DONE}"
            add_label "${LABEL_DONE}"
            remove_label "${LABEL_READY}"
            log "label state: ${LABEL_DONE} applied, ${LABEL_READY} removed"
            ;;

        no-commits | uncommitted-changes | committed-no-pr)
            # Retryable failure — leave agent-ready in place for Baton's own retry.
            #
            # Rationale for treating committed-no-pr as retryable (not terminal):
            # The agent committed code but the `gh pr create` step failed or was
            # skipped. This is recoverable: a retry run can open the PR. Marking
            # it terminal would discard real work. A human can also open the PR
            # manually if Baton's retry budget is exhausted.
            #
            # Rationale for treating uncommitted-changes as retryable:
            # Uncommitted changes may indicate the agent ran out of turns mid-task.
            # A retry can continue from the dirty worktree. Marking it terminal
            # would abandon potentially useful partial work.
            log "retryable state '${GIT_STATE}' — leaving '${LABEL_READY}' in place for Baton retry"
            log "label state: ${LABEL_READY} unchanged (retryable path)"
            ;;

        *)
            err "unexpected GIT_STATE '${GIT_STATE}' — no label change made"
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    log "starting (cwd='${PWD}', issue=#${ISSUE})"

    classify_git_state
    reconcile_labels

    log "done"
}

main "$@"
