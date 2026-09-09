#!/usr/bin/env bash
# bin/run-daemon.sh — Always-on daemon launcher
#
# Replaces bin/run.sh (deleted in P3).  Launches the codereeve daemon entry point
# which polls for agent-ready issues and runs dependency-ordered work units.
#
# Usage:
#   bin/run-daemon.sh [--once] [--workflow <path>] [--poll-interval <secs>]
#
# Arguments passed through to codereeve daemon:
#   --once            Run one tick then exit (useful for smoke tests).
#   --workflow PATH   Path to WORKFLOW.md.  Defaults to config/WORKFLOW.md
#                     in the CodeReeve repository root.
#   --poll-interval N Override the outer-loop poll interval in seconds.
#
# Required environment variable:
#   CODEREEVE_PROJECT_ROOT    Absolute path to the local clone of the managed repo.
#
# Sandbox repo identity and GitHub App settings are read by codereeve daemon from
# ${CODEREEVE_PROJECT_ROOT}/.codereeve/config.env at startup.
#
# Exported environment:
#   CODEREEVE_ROOT  Absolute path to the CodeReeve repository root.
#   CODEREEVE_VENV            Absolute path to the venv that contains codereeve daemon.

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

Required environment variable:
  CODEREEVE_PROJECT_ROOT    Absolute path to the local clone of the managed repo

Sandbox config:
  Launches the CodeReeve daemon, which reads
  ${CODEREEVE_PROJECT_ROOT}/.codereeve/config.env at startup for
  CODEREEVE_REPO_OWNER, CODEREEVE_REPO_NAME, CODEREEVE_GITHUB_APP_ID, and related BWS_* IDs.

Exported to hooks:
  CODEREEVE_ROOT  Absolute path to the CodeReeve repository root
  CODEREEVE_VENV            Absolute path to the venv containing codereeve daemon
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

# ---------------------------------------------------------------------------
# Locate codereeve daemon and derive the venv root
# ---------------------------------------------------------------------------

_codereeve_daemon_bin="$(command -v codereeve)" || {
    echo "error: codereeve daemon not found on PATH — install CodeReeve first" >&2
    echo "       pip install -e . (or uv pip install -e .)" >&2
    exit 1
}
CODEREEVE_VENV="$(cd "$(dirname "${_codereeve_daemon_bin}")/.." && pwd)"
export CODEREEVE_VENV

# ---------------------------------------------------------------------------
# Resolve CodeReeve root from the script's own location
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEREEVE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export CODEREEVE_ROOT

# ---------------------------------------------------------------------------
# Source shared env-config loader (host.env -> CODEREEVE_PROJECT_ROOT;
# .codereeve/config.env -> CODEREEVE_REPO_OWNER/CODEREEVE_REPO_NAME/etc; operator env wins)
# ---------------------------------------------------------------------------

_codereeve_load_config="$(dirname "${BASH_SOURCE[0]}")/lib/load-config.sh"
if [[ -f "${_codereeve_load_config}" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "${_codereeve_load_config}"
fi
unset _codereeve_load_config

# ---------------------------------------------------------------------------
# Validate required environment variables
# ---------------------------------------------------------------------------

_missing_env=()
# shellcheck disable=SC2043  # deliberate extensible checklist; currently one entry
for _var in CODEREEVE_PROJECT_ROOT; do
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
    echo "Set it via one of:" >&2
    echo "  - Run bin/setup-env.sh (writes CODEREEVE_PROJECT_ROOT to ~/.config/codereeve/host.env)" >&2
    echo "  - Or export CODEREEVE_PROJECT_ROOT in your shell as a last-resort override" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Parse --workflow override (without consuming/altering "$@" for codereeve daemon)
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
    WORKFLOW_FILE="${CODEREEVE_ROOT}/config/WORKFLOW.md"
    if [[ ! -f "${WORKFLOW_FILE}" ]]; then
        echo "error: workflow config not found: ${WORKFLOW_FILE}" >&2
        echo "       Create config/WORKFLOW.md in the CodeReeve repo." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Extract repo slug from .codereeve/config.env for shell-side preflights
# ---------------------------------------------------------------------------

if [[ -z "${CODEREEVE_REPO_OWNER:-}" || -z "${CODEREEVE_REPO_NAME:-}" ]]; then
    echo "error: CODEREEVE_REPO_OWNER or CODEREEVE_REPO_NAME missing from resolved configuration" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Label preflight — verify required CodeReeve labels exist in the target repo
# ---------------------------------------------------------------------------

_REQUIRED_LABELS=(
    "agent-ready"
    "agent-done"
    "blocked"
    "agent-in-progress"
    "agent-merged"
)

_REPO_SLUG="${CODEREEVE_REPO_OWNER}/${CODEREEVE_REPO_NAME}"
echo "codereeve: checking required labels in ${_REPO_SLUG}..."

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

echo "codereeve: all required labels present"

# ---------------------------------------------------------------------------
# Gitignore preflight — verify .symphony/ is gitignored in the target repo
# ---------------------------------------------------------------------------

echo "codereeve: checking .symphony/ is gitignored in ${CODEREEVE_PROJECT_ROOT}..."

if [[ ! -f "${CODEREEVE_PROJECT_ROOT}/.gitignore" ]] || ! grep -qxF '.symphony/' "${CODEREEVE_PROJECT_ROOT}/.gitignore"; then
    echo "error: this repo is not ready for CodeReeve work — '.symphony/' is not gitignored in ${CODEREEVE_PROJECT_ROOT}" >&2
    echo "  The daemon writes orchestrator state to .symphony/; it must be gitignored or gh pr create warns and the state file pollutes the tree." >&2
    echo "  fix: add a line '.symphony/' to ${CODEREEVE_PROJECT_ROOT}/.gitignore and commit it (bin/init-sandbox.sh does this automatically for sandboxes)." >&2
    exit 1
fi

echo "codereeve: .symphony/ is gitignored"

# ---------------------------------------------------------------------------
# Launch the daemon
# ---------------------------------------------------------------------------

echo "codereeve: root=${CODEREEVE_ROOT}"
echo "codereeve: workflow=${WORKFLOW_FILE}"
echo "codereeve: repo=${CODEREEVE_REPO_OWNER}/${CODEREEVE_REPO_NAME} at ${CODEREEVE_PROJECT_ROOT}"
echo "codereeve: starting CodeReeve daemon..."

# Change into the managed repo root so that any gh calls that rely on cwd
# for repo resolution (e.g. vendored GitHubTracker) hit the right repo.
# Belt-and-suspenders with the cli.py os.chdir; also makes the intent
# obvious to operators reading this script.
cd "${CODEREEVE_PROJECT_ROOT}"

exec codereeve daemon "$@"
