#!/usr/bin/env bash
# bin/provision-ruleset.sh — Idempotent ruleset applier (slice 3b).
#
# Brings the live GitHub Repository Rulesets into agreement with the
# checked-in JSON configs at config/ruleset.main.json and
# config/ruleset.feature.json.
#
# Two-step ID lookup (per GitHub Rulesets REST API):
#   1. GET /repos/<owner>/<repo>/rulesets  -> list of {id,name}; filter by name
#   2. GET /repos/<owner>/<repo>/rulesets/<id>  -> single ruleset detail
# (Name-string lookup at endpoint 2 returns 404 silently — must not be used.)
#
# Required environment variables:
#   BH_REPO_OWNER                    GitHub repository owner.
#   BH_REPO_NAME                     GitHub repository name.
#   BH_GITHUB_APP_ID                 Numeric App ID for ruleset bypass
#                                    (NOT the same as installation id).
#   BH_GITHUB_APP_INSTALLATION_ID    Required for app_auth.py at runtime.
#                                    Validated for presence only here.
#
# Optional environment variables:
#   BH_ADMIN_ROLE_ID                 Numeric RepositoryRole id for admin
#                                    bypass on main. Default: 5 (community-
#                                    cited; not officially documented).
#
# Exit codes:
#   0  success — rulesets match or were brought into agreement
#   1  drift could not be fixed
#   2  missing env / invalid config / preflight App-ID mismatch

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bin/provision-ruleset.sh [--help|-h]

Idempotently provisions the harness-main-no-merge and
harness-feature-daemon-only rulesets in the target sandbox repo.
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Python resolver — mirrors after_create.py:L99-L106.
# ---------------------------------------------------------------------------
_PYTHON="${HARNESS_DIR}/.venv/Scripts/python.exe"
[[ ! -x "${_PYTHON}" ]] && _PYTHON="${HARNESS_DIR}/.venv/bin/python"
[[ ! -x "${_PYTHON}" ]] && _PYTHON="python3"

# ---------------------------------------------------------------------------
# Env validation.
# ---------------------------------------------------------------------------
_missing=()
for v in BH_REPO_OWNER BH_REPO_NAME BH_GITHUB_APP_ID BH_GITHUB_APP_INSTALLATION_ID; do
    if [[ -z "${!v:-}" ]]; then
        _missing+=("${v}")
    fi
done
if [[ ${#_missing[@]} -gt 0 ]]; then
    echo "provision-ruleset: missing env vars: ${_missing[*]}" >&2
    exit 2
fi

REPO_SLUG="${BH_REPO_OWNER}/${BH_REPO_NAME}"
ADMIN_ROLE_ID="${BH_ADMIN_ROLE_ID:-5}"

# ---------------------------------------------------------------------------
# Isolate the subprocess Python environment.
# When this script is invoked from within a uv-managed venv (e.g. via pytest),
# PYTHONHOME may be set to the venv's CPython directory.  If a subprocess then
# calls the system python3 (which the fake_gh shim does for JSON serialisation),
# that interpreter inherits the wrong PYTHONHOME and fails to initialise its
# site module.  Unsetting PYTHONHOME here lets every child process resolve its
# own Python home independently, which is the correct behaviour for a shell
# script that is not itself a Python program.
# ---------------------------------------------------------------------------
unset PYTHONHOME PYTHONPATH 2>/dev/null || true

# ---------------------------------------------------------------------------
# Preflight: cross-check BH_GITHUB_APP_ID against GET /app (B3).
# Parse the JSON response with Python so this works with both real gh
# (which honours --jq) and the fake shim (which ignores --jq flags).
# ---------------------------------------------------------------------------
_app_response="$(gh api app)"
_live_app_id="$(
    printf '%s' "${_app_response}" \
        | "${_PYTHON}" -c \
            'import json,sys; print(json.loads(sys.stdin.read())["id"])' \
        2>/dev/null || true
)"
if [[ "${_live_app_id}" != "${BH_GITHUB_APP_ID}" ]]; then
    echo "provision-ruleset: PREFLIGHT FAILURE — BH_GITHUB_APP_ID=${BH_GITHUB_APP_ID}" >&2
    echo "  but GET /app .id returned ${_live_app_id}." >&2
    echo "  BH_GITHUB_APP_ID must be the App ID from https://github.com/settings/apps/<slug>," >&2
    echo "  NOT the Installation ID. Aborting before writing ruleset." >&2
    exit 2
fi
echo "provision-ruleset: preflight OK — App ID ${BH_GITHUB_APP_ID} confirmed via GET /app"
echo "provision-ruleset: using admin RepositoryRole actor_id=${ADMIN_ROLE_ID}"

# ---------------------------------------------------------------------------
# Render each config (substitute placeholders into valid JSON integers).
# The placeholder values appear as JSON strings: "\"__BH_GITHUB_APP_ID__\""
# and must become bare JSON integers after substitution so json.loads
# parses them as int, matching the GitHub API response format.
# ---------------------------------------------------------------------------
_render_config() {
    local src="$1"
    sed \
        -e "s|\"__BH_GITHUB_APP_ID__\"|${BH_GITHUB_APP_ID}|g" \
        -e "s|\"__BH_ADMIN_ROLE_ID__\"|${ADMIN_ROLE_ID}|g" \
        "${src}"
}

# ---------------------------------------------------------------------------
# List + filter helper: discover numeric id for a ruleset name.
# Returns the first matching id via Python json.load, or empty string.
# ---------------------------------------------------------------------------
_lookup_id() {
    local target_name="$1"
    local list_json
    # --paginate follows Link: rel="next" headers to fetch all pages.
    # --slurp wraps the per-page JSON arrays into an outer array:
    # [[page1-entry, ...], [page2-entry, ...], ...].
    # The Python snippet below flattens one level before searching.
    list_json="$(gh api --paginate --slurp "repos/${REPO_SLUG}/rulesets")"
    "${_PYTHON}" -c "
import json, sys
pages = json.loads(sys.argv[1])
# --slurp produces a list-of-lists; flatten one level.
entries = [e for page in pages for e in page]
for entry in entries:
    if entry.get('name') == sys.argv[2]:
        print(entry['id'])
        sys.exit(0)
" "${list_json}" "${target_name}" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# _COMPARE_KEYS: structural keys to compare between desired and live state.
# Excludes:
#   - _comment     (Task 1 uses this for JSON pseudo-comments; no GitHub field)
#   - id, source, source_type, _links, node_id, *_at, current_user_can_bypass
#     (server-managed fields present in GET responses but absent in PUT/POST)
# ---------------------------------------------------------------------------
_COMPARE_KEYS='["name","target","enforcement","bypass_actors","conditions","rules"]'

# ---------------------------------------------------------------------------
# Apply one ruleset: list+filter, GET-by-id, compare, PUT/POST.
# ---------------------------------------------------------------------------
_apply_ruleset() {
    local name="$1"
    local desired_path="$2"
    local desired
    desired="$(_render_config "${desired_path}")"

    local existing_id
    existing_id="$(_lookup_id "${name}")"

    if [[ -z "${existing_id}" ]]; then
        echo "provision-ruleset: ${name} absent — POST-ing"
        printf '%s' "${desired}" | gh api \
            --method POST \
            "repos/${REPO_SLUG}/rulesets" \
            --input -
        return 0
    fi

    local current_body
    current_body="$(gh api "repos/${REPO_SLUG}/rulesets/${existing_id}")"

    # Compare structural keys only; exit 0 means no drift.
    if "${_PYTHON}" -c "
import json, sys
desired = json.loads(sys.argv[1])
current = json.loads(sys.argv[2])
keys = json.loads(sys.argv[3])
sys.exit(0 if all(desired.get(k) == current.get(k) for k in keys) else 1)
" "${desired}" "${current_body}" "${_COMPARE_KEYS}" 2>/dev/null; then
        echo "provision-ruleset: ${name} matches (id=${existing_id}) — no-op"
        return 0
    fi

    echo "provision-ruleset: ${name} drift detected (id=${existing_id}) — PUT-ing"
    printf '%s' "${desired}" | gh api \
        --method PUT \
        "repos/${REPO_SLUG}/rulesets/${existing_id}" \
        --input -
}

_apply_ruleset "harness-main-no-merge" "${HARNESS_DIR}/config/ruleset.main.json"
_apply_ruleset "harness-feature-daemon-only" "${HARNESS_DIR}/config/ruleset.feature.json"

echo "provision-ruleset: complete"
