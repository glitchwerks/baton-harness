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
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

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
# Launch Baton
# ---------------------------------------------------------------------------

echo "baton-harness: harness=${BATON_HARNESS_DIR}"
echo "baton-harness: workflow=${WORKFLOW_FILE}"
echo "baton-harness: repo=${PROJECT_REPO_ABS}"
echo "baton-harness: starting baton..."

cd "${PROJECT_REPO_ABS}"
exec baton start -w "${WORKFLOW_FILE}"
