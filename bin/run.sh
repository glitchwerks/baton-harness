#!/usr/bin/env bash
# bin/run.sh — Baton harness launcher
#
# Encapsulates the point-at-path Baton invocation (harness-design.md §2, §4.1)
# so it isn't retyped or misremembered.
#
# Resolves the harness root from the script's own location and exports it as
# BATON_HARNESS_DIR so hook scripts can resolve scripts/ without hardcoding
# a path (harness-design.md §8 design decision: env var over hardcoded path).
#
# Also derives the venv root from baton's own location and exports BH_VENV.
# Hook lines in WORKFLOW.md use this to self-activate the venv before calling
# bh-*, making them work under Baton's login-shell runner (which re-derives
# PATH from the user's profile and does not inherit an activated venv).
#
# Usage:
#   bin/run.sh <project-repo-path>
#
# Arguments:
#   project-repo-path  Absolute or relative path to the target project repo
#                      (the directory baton runs inside)
#
# Example:
#   bin/run.sh /home/chris/projects/my-api
#
# Environment exported:
#   BATON_HARNESS_DIR  Absolute path to this harness repo root. Available to
#                      all Baton hook scripts so they can resolve scripts/.
#   BH_VENV            Absolute path to the venv that contains baton and the
#                      bh-* console scripts.

set -euo pipefail

# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: bin/run.sh <project-repo-path>

Arguments:
  project-repo-path  Path to the target project repo (baton runs inside it)

Example:
  bin/run.sh /home/chris/projects/my-api

Environment exported to hooks:
  BATON_HARNESS_DIR  Absolute path to this harness repo root
  BH_VENV            Absolute path to the venv containing baton and bh-*
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

# ---------------------------------------------------------------------------
# Locate baton and derive the venv root
#
# baton and the bh-* console scripts all live in the same venv bin/.  By
# deriving the venv root from baton's own location we export BH_VENV without
# hard-coding any path.  Hook lines in WORKFLOW.md activate the venv before
# calling bh-* so they resolve correctly under Baton's login-shell runner
# (which re-derives PATH from the user's profile and does not inherit the
# caller's activated venv).
# ---------------------------------------------------------------------------

BATON_BIN="$(command -v baton)" || {
    echo "error: baton not found on PATH — install it first" >&2
    exit 1
}
BH_VENV="$(cd "$(dirname "${BATON_BIN}")/.." && pwd)"
export BH_VENV

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

if [[ $# -ne 1 ]]; then
    echo "error: expected 1 argument, got $#" >&2
    echo >&2
    usage >&2
    exit 1
fi

PROJECT_REPO_PATH="$1"

# ---------------------------------------------------------------------------
# Resolve harness root from the script's own location (works regardless of cwd)
# ---------------------------------------------------------------------------

# BASH_SOURCE[0] is the path to this script file.
# Resolve it to an absolute path, then step up one directory (bin/ -> root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BATON_HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export BATON_HARNESS_DIR

# ---------------------------------------------------------------------------
# Validate config file exists
# ---------------------------------------------------------------------------

WORKFLOW_FILE="${BATON_HARNESS_DIR}/config/WORKFLOW.md"

if [[ ! -f "${WORKFLOW_FILE}" ]]; then
    echo "error: workflow config not found: ${WORKFLOW_FILE}" >&2
    echo "       Create config/WORKFLOW.md in the harness repo." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Validate target project repo path exists
# ---------------------------------------------------------------------------

if [[ ! -d "${PROJECT_REPO_PATH}" ]]; then
    echo "error: project repo path not found: ${PROJECT_REPO_PATH}" >&2
    exit 1
fi

# Resolve to absolute path before cd so errors are unambiguous
PROJECT_REPO_ABS="$(cd "${PROJECT_REPO_PATH}" && pwd)"

# ---------------------------------------------------------------------------
# Label preflight — verify all three harness state labels exist in the target
# repo before starting Baton.  after_run reconciliation uses gh issue edit
# --add-label / --remove-label for these labels; if any are missing from the
# repo, gh will error and reconciliation breaks (pilot: observed missing
# 'agent-done' causing an unbounded dispatch loop via issue #21).
#
# Fail-clear: we never auto-create labels.  Mutating the target repo silently
# is outside this harness's scope and could hide misconfiguration.  The
# operator must create missing labels explicitly.
# ---------------------------------------------------------------------------

_REQUIRED_LABELS=(
    "agent-ready"
    "agent-done"
    "blocked"
)

echo "baton-harness: checking required labels in ${PROJECT_REPO_ABS}..."
_REPO_SLUG="$(git -C "${PROJECT_REPO_ABS}" remote get-url origin 2>/dev/null \
    | sed -E 's#.*github\.com[:/]([^/]+)/([^/]+?)(\.git)?$#\1/\2#')"
if [[ -z "${_REPO_SLUG}" ]]; then
    echo "error: could not determine GitHub repo slug for ${PROJECT_REPO_ABS}" >&2
    exit 1
fi

_missing_labels=()
_existing_labels="$(gh label list -R "${_REPO_SLUG}" --limit 200 --json name --jq '.[].name')"

for _label in "${_REQUIRED_LABELS[@]}"; do
    if ! echo "${_existing_labels}" | grep -qxF "${_label}"; then
        _missing_labels+=("${_label}")
    fi
done

if [[ ${#_missing_labels[@]} -gt 0 ]]; then
    echo "error: the following required labels are missing from the target repo:" >&2
    echo "       ${PROJECT_REPO_ABS}" >&2
    echo >&2
    for _label in "${_missing_labels[@]}"; do
        echo "  missing: ${_label}" >&2
        echo "  fix:     gh label create \"${_label}\" -R \"${_REPO_SLUG}\" --color 0075ca" >&2
        echo >&2
    done
    echo "Create the missing label(s) above, then re-run bin/run.sh." >&2
    exit 1
fi

echo "baton-harness: all required labels present (agent-ready, agent-done, blocked)"

# ---------------------------------------------------------------------------
# Launch Baton
# ---------------------------------------------------------------------------

echo "baton-harness: harness=${BATON_HARNESS_DIR}"
echo "baton-harness: workflow=${WORKFLOW_FILE}"
echo "baton-harness: repo=${PROJECT_REPO_ABS}"
echo "baton-harness: starting baton..."

cd "${PROJECT_REPO_ABS}"
exec baton start -w "${WORKFLOW_FILE}"
