#!/usr/bin/env bash
# bin/run-daemon.sh — Always-on daemon launcher
#
# Replaces bin/run.sh (deleted in P3).  Launches the bh-daemon entry point
# which polls for agent-ready issues and runs dependency-ordered work units.
#
# Usage:
#   bin/run-daemon.sh [--once] [--workflow <path>] [--poll-interval <secs>]
#
# Arguments passed through to bh-daemon:
#   --once            Run one tick then exit (useful for smoke tests).
#   --workflow PATH   Path to WORKFLOW.md.  Defaults to config/WORKFLOW.md
#                     in the harness root.
#   --poll-interval N Override the outer-loop poll interval in seconds.
#
# Required environment variables:
#   BH_REPO_OWNER      GitHub repository owner (org or user login).
#   BH_REPO_NAME       GitHub repository name (without owner prefix).
#   BH_PROJECT_ROOT    Absolute path to the local clone of the managed repo.
#
# Exported environment:
#   BATON_HARNESS_DIR  Absolute path to this harness repo root.
#   BH_VENV            Absolute path to the venv that contains bh-daemon.

set -euo pipefail

# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: bin/run-daemon.sh [--once] [--workflow PATH] [--poll-interval SECS]

Arguments:
  --once              Run one tick then exit (smoke test mode)
  --workflow PATH     Path to WORKFLOW.md (default: config/WORKFLOW.md)
  --poll-interval N   Override outer-loop poll interval in seconds

Required environment variables:
  BH_REPO_OWNER      GitHub repository owner (org or user login)
  BH_REPO_NAME       GitHub repository name
  BH_PROJECT_ROOT    Absolute path to the local clone of the managed repo

Exported to hooks:
  BATON_HARNESS_DIR  Absolute path to this harness repo root
  BH_VENV            Absolute path to the venv containing bh-daemon
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

# ---------------------------------------------------------------------------
# Locate bh-daemon and derive the venv root
# ---------------------------------------------------------------------------

BH_DAEMON_BIN="$(command -v bh-daemon)" || {
    echo "error: bh-daemon not found on PATH — install the harness first" >&2
    echo "       pip install -e . (or uv pip install -e .)" >&2
    exit 1
}
BH_VENV="$(cd "$(dirname "${BH_DAEMON_BIN}")/.." && pwd)"
export BH_VENV

# ---------------------------------------------------------------------------
# Resolve harness root from the script's own location
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BATON_HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export BATON_HARNESS_DIR

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
    echo "error: the following required environment variables are not set:" >&2
    for _var in "${_missing_env[@]}"; do
        echo "  missing: ${_var}" >&2
    done
    echo >&2
    echo "Set them before running bin/run-daemon.sh:" >&2
    echo "  export BH_REPO_OWNER=<owner>" >&2
    echo "  export BH_REPO_NAME=<repo>" >&2
    echo "  export BH_PROJECT_ROOT=/path/to/local/clone" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Parse --workflow override (without consuming/altering "$@" for bh-daemon)
# ---------------------------------------------------------------------------

_WORKFLOW_OVERRIDE=""
_args_remaining=("$@")
_i=0
while [[ ${_i} -lt ${#_args_remaining[@]} ]]; do
    _arg="${_args_remaining[${_i}]}"
    case "${_arg}" in
        --workflow=*)
            _WORKFLOW_OVERRIDE="${_arg#--workflow=}"
            ;;
        --workflow)
            _i=$(( _i + 1 ))
            if [[ ${_i} -lt ${#_args_remaining[@]} ]]; then
                _WORKFLOW_OVERRIDE="${_args_remaining[${_i}]}"
            fi
            ;;
    esac
    _i=$(( _i + 1 ))
done
unset _args_remaining _i _arg

# ---------------------------------------------------------------------------
# Validate config file exists
# ---------------------------------------------------------------------------

if [[ -n "${_WORKFLOW_OVERRIDE}" ]]; then
    WORKFLOW_FILE="${_WORKFLOW_OVERRIDE}"
    if [[ ! -f "${WORKFLOW_FILE}" ]]; then
        echo "error: workflow config not found (from --workflow override): ${WORKFLOW_FILE}" >&2
        exit 1
    fi
else
    WORKFLOW_FILE="${BATON_HARNESS_DIR}/config/WORKFLOW.md"
    if [[ ! -f "${WORKFLOW_FILE}" ]]; then
        echo "error: workflow config not found: ${WORKFLOW_FILE}" >&2
        echo "       Create config/WORKFLOW.md in the harness repo." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Label preflight — verify required harness labels exist in the target repo
# ---------------------------------------------------------------------------

_REQUIRED_LABELS=(
    "agent-ready"
    "agent-done"
    "blocked"
    "agent-in-progress"
    "agent-merged"
)

_REPO_SLUG="${BH_REPO_OWNER}/${BH_REPO_NAME}"
echo "baton-harness: checking required labels in ${_REPO_SLUG}..."

_missing_labels=()
_existing_labels="$(gh label list -R "${_REPO_SLUG}" --limit 200 --json name --jq '.[].name')"

for _label in "${_REQUIRED_LABELS[@]}"; do
    if ! echo "${_existing_labels}" | grep -qxF "${_label}"; then
        _missing_labels+=("${_label}")
    fi
done

if [[ ${#_missing_labels[@]} -gt 0 ]]; then
    echo "error: the following required labels are missing from the target repo:" >&2
    echo "       ${_REPO_SLUG}" >&2
    echo >&2
    for _label in "${_missing_labels[@]}"; do
        echo "  missing: ${_label}" >&2
        echo "  fix:     gh label create \"${_label}\" -R \"${_REPO_SLUG}\" --color 0075ca" >&2
        echo >&2
    done
    echo "Create the missing label(s) above, then re-run bin/run-daemon.sh." >&2
    exit 1
fi

echo "baton-harness: all required labels present"

# ---------------------------------------------------------------------------
# Launch the daemon
# ---------------------------------------------------------------------------

echo "baton-harness: harness=${BATON_HARNESS_DIR}"
echo "baton-harness: workflow=${WORKFLOW_FILE}"
echo "baton-harness: repo=${BH_REPO_OWNER}/${BH_REPO_NAME} at ${BH_PROJECT_ROOT}"
echo "baton-harness: starting bh-daemon..."

# Change into the managed repo root so that any gh calls that rely on cwd
# for repo resolution (e.g. vendored GitHubTracker) hit the right repo.
# Belt-and-suspenders with the cli.py os.chdir; also makes the intent
# obvious to operators reading this script.
cd "${BH_PROJECT_ROOT}"

exec bh-daemon "$@"
