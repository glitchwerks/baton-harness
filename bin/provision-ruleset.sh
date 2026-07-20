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
#   BH_APP_AUTH_JWT_CMD              Command that prints the App JWT.
#   BH_APP_AUTH_TOKEN_CMD            Command that prints the installation
#                                    token.
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
# Source shared env-config loader (host.env -> BH_PROJECT_ROOT;
# .bh/config.env -> BH_REPO_OWNER/BH_REPO_NAME/BH_GITHUB_APP_ID/etc;
# operator env wins). Lets the four required vars below resolve from
# .bh/config.env instead of requiring manual export every time.
# ---------------------------------------------------------------------------
_BH_LOAD_CONFIG="${SCRIPT_DIR}/lib/load-config.sh"
if [[ -f "${_BH_LOAD_CONFIG}" ]]; then
    # shellcheck disable=SC1091
    source "${_BH_LOAD_CONFIG}"
fi
unset _BH_LOAD_CONFIG

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

# Acquire the App JWT before making any GitHub API call. The command override
# is a test/operator seam; production falls back to the app_auth module.
if [[ -n "${BH_APP_AUTH_JWT_CMD:-}" ]]; then
    # Trusted operator/test-only override; never wire to untrusted input.
    if ! _APP_JWT="$(eval "${BH_APP_AUTH_JWT_CMD}")"; then
        echo "provision-ruleset: could not obtain the App JWT." >&2
        exit 2
    fi
else
    if ! _APP_JWT="$("${_PYTHON}" -m baton_harness.chain.app_auth jwt)"; then
        echo "provision-ruleset: could not obtain the App JWT." >&2
        exit 2
    fi
fi
if [[ -z "${_APP_JWT}" ]]; then
    echo "provision-ruleset: could not obtain the App JWT (empty output)." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Preflight: cross-check BH_GITHUB_APP_ID against GET /app (B3).
# Parse the JSON response with Python so this works with both real gh
# (which honours --jq) and the fake shim (which ignores --jq flags).
# ---------------------------------------------------------------------------
_warn_skip_appid_check() {
    echo "provision-ruleset: WARNING — gh is not App-authenticated or GET /app returned no App ID; skipping the App ID cross-check via GET /app. Ensure the ambient token has administration: write for the subsequent ruleset writes." >&2
}

_app_stderr_file="$(mktemp)"
if ! _app_response="$(GH_TOKEN="${_APP_JWT}" gh api app 2>"${_app_stderr_file}")"; then
    _app_stderr="$(cat "${_app_stderr_file}" 2>/dev/null || true)"
    rm -f "${_app_stderr_file}"
    echo "provision-ruleset: PREFLIGHT FAILURE — GET /app failed; cannot confirm BH_GITHUB_APP_ID. A freshly-minted App JWT is always used for this call, so any failure means that credential is unusable." >&2
    if [[ -n "${_app_stderr}" ]]; then
        echo "  ${_app_stderr}" >&2
    fi
    exit 1
else
    rm -f "${_app_stderr_file}"
    _live_app_id="$(
        printf '%s' "${_app_response}" \
            | "${_PYTHON}" -c \
                'import json,sys; print(json.loads(sys.stdin.read())["id"])' \
            2>/dev/null || true
    )"
    if [[ -z "${_live_app_id}" ]]; then
        _warn_skip_appid_check
    elif [[ "${_live_app_id}" != "${BH_GITHUB_APP_ID}" ]]; then
        echo "provision-ruleset: PREFLIGHT FAILURE — BH_GITHUB_APP_ID=${BH_GITHUB_APP_ID}" >&2
        echo "  but GET /app .id returned ${_live_app_id}." >&2
        echo "  BH_GITHUB_APP_ID must be the App ID from https://github.com/settings/apps/<slug>," >&2
        echo "  NOT the Installation ID. Aborting before writing ruleset." >&2
        exit 2
    else
        echo "provision-ruleset: preflight OK — App ID ${BH_GITHUB_APP_ID} confirmed via GET /app"
    fi
fi

# Acquire the installation token only after the App-ID preflight. All
# repository- and organization-scoped calls below use this credential.
if [[ -n "${BH_APP_AUTH_TOKEN_CMD:-}" ]]; then
    # Trusted operator/test-only override; never wire to untrusted input.
    if ! _INSTALL_TOKEN="$(eval "${BH_APP_AUTH_TOKEN_CMD}")"; then
        echo "provision-ruleset: could not obtain the installation token." >&2
        exit 2
    fi
else
    if ! _INSTALL_TOKEN="$("${_PYTHON}" -m baton_harness.chain.app_auth token)"; then
        echo "provision-ruleset: could not obtain the installation token." >&2
        exit 2
    fi
fi
if [[ -z "${_INSTALL_TOKEN}" ]]; then
    echo "provision-ruleset: could not obtain the installation token (empty output)." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Preflight: validate the admin-role assumption before writing rulesets.
# 1. The repo must currently report at least one admin collaborator.
# 2. Default actor_id=5 is accepted only with that GitHub-backed admin signal.
# 3. Non-default overrides must be validated against the org custom roles API.
# ---------------------------------------------------------------------------
_admin_collaborators_json="$(
    GH_TOKEN="${_INSTALL_TOKEN}" gh api "repos/${REPO_SLUG}/collaborators?permission=admin"
)"
_admin_count="$(
    printf '%s' "${_admin_collaborators_json}" \
        | "${_PYTHON}" -c '
import json, sys
data = json.loads(sys.stdin.read())
print(sum(
    1 for entry in data
    if entry.get("role_name") == "admin"
    or bool((entry.get("permissions") or {}).get("admin"))
))
' 2>/dev/null || true
)"
if [[ -z "${_admin_count}" || "${_admin_count}" == "0" ]]; then
    echo "provision-ruleset: PREFLIGHT FAILURE — repo reports no admin collaborators." >&2
    echo "  GET /repos/${REPO_SLUG}/collaborators?permission=admin returned no admin identities." >&2
    echo "  Refusing to write rulesets until the admin bypass assumption is validated." >&2
    exit 2
fi

if [[ "${ADMIN_ROLE_ID}" != "5" ]]; then
    _custom_roles_err="$(mktemp)"
    if _custom_roles_json="$(
        GH_TOKEN="${_INSTALL_TOKEN}" gh api "orgs/${BH_REPO_OWNER}/custom-repository-roles" 2>"${_custom_roles_err}"
    )"; then
        _custom_role_match="$(
            printf '%s' "${_custom_roles_json}" \
                | "${_PYTHON}" -c '
import json, sys
target = int(sys.argv[1])
for entry in json.loads(sys.stdin.read()):
    if entry.get("id") == target and entry.get("base_role") == "admin":
        print("match")
        break
' "${ADMIN_ROLE_ID}" 2>/dev/null || true
        )"
        rm -f "${_custom_roles_err}"
        if [[ "${_custom_role_match}" != "match" ]]; then
            echo "provision-ruleset: PREFLIGHT FAILURE — BH_ADMIN_ROLE_ID=${ADMIN_ROLE_ID}" >&2
            echo "  GitHub custom repository roles did not report an admin-based role with that id." >&2
            echo "  Override the id to a validated admin role or omit BH_ADMIN_ROLE_ID to use the default 5." >&2
            exit 2
        fi
        echo "provision-ruleset: preflight OK — custom admin RepositoryRole actor_id=${ADMIN_ROLE_ID} validated via org custom roles API"
    else
        _custom_roles_stderr="$(cat "${_custom_roles_err}")"
        rm -f "${_custom_roles_err}"
        echo "provision-ruleset: PREFLIGHT FAILURE — BH_ADMIN_ROLE_ID=${ADMIN_ROLE_ID} is a non-default override." >&2
        echo "  GitHub did not expose a custom-role validation API for ${BH_REPO_OWNER}." >&2
        echo "  stderr: ${_custom_roles_stderr}" >&2
        echo "  Refusing to write rulesets because the override cannot be validated." >&2
        exit 2
    fi
else
    echo "provision-ruleset: preflight OK — repo reports ${_admin_count} admin collaborator(s); using default RepositoryRole actor_id=5"
fi

echo "provision-ruleset: using admin RepositoryRole actor_id=${ADMIN_ROLE_ID}"

# ---------------------------------------------------------------------------
# Strip all "_comment" pseudo-comment keys from a JSON document, at any
# depth. config/ruleset.*.json carries these as in-repo documentation, but
# the real GitHub Rulesets API never emits them in GET responses and 422s
# on a POST/PUT that includes one (issue #202, Bug 2). Reads JSON on
# stdin, writes the stripped JSON on stdout.
# ---------------------------------------------------------------------------
_strip_comments() {
    "${_PYTHON}" -c '
import json, sys


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k != "_comment"}
    if isinstance(obj, list):
        return [_strip(item) for item in obj]
    return obj


print(json.dumps(_strip(json.loads(sys.stdin.read()))))
'
}

# ---------------------------------------------------------------------------
# Render each config (substitute placeholders into valid JSON integers,
# then strip "_comment" keys). The placeholder values appear as JSON
# strings: "\"__BH_GITHUB_APP_ID__\"" and must become bare JSON integers
# after substitution so json.loads parses them as int, matching the
# GitHub API response format.
#
# The "_comment" strip happens here — once — so the SAME comment-free
# ``desired`` string feeds both the drift comparison and the POST/PUT
# write payload (see _apply_ruleset below). Stripping only at write time
# would leave "_comment" in the value used for comparison, which would
# never match a comment-free live GET body and cause a perpetual PUT
# (issue #202, Bug 2).
# ---------------------------------------------------------------------------
_render_config() {
    local src="$1"
    sed \
        -e "s|\"__BH_GITHUB_APP_ID__\"|${BH_GITHUB_APP_ID}|g" \
        -e "s|\"__BH_ADMIN_ROLE_ID__\"|${ADMIN_ROLE_ID}|g" \
        "${src}" | _strip_comments
}

# ---------------------------------------------------------------------------
# List + filter helper: discover numeric id for a ruleset name.
# Returns the first matching id via Python json.load, or empty string.
#
# On a genuine LIST-call failure (network error, auth error, etc.) this
# returns non-zero and prints nothing on stdout, so the caller can tell
# "lookup failed" apart from "ruleset legitimately absent" and must NOT
# fall through to a spurious create (issue #202, Bug 1 robustness note).
#
# The same applies when the LIST call itself succeeds (HTTP 200) but
# returns a body that is not valid JSON: the Python parse step's
# non-zero exit propagates out of this function (no error masking),
# so the caller still sees "lookup failed" rather than misreading a
# parse failure as "ruleset absent" (issue #202/#203 follow-up).
# ---------------------------------------------------------------------------
_lookup_id() {
    local target_name="$1"
    local list_json
    local list_err
    list_err="$(mktemp)"
    # --paginate follows Link: rel="next" headers to fetch all pages. Real
    # `gh api --paginate` on an array endpoint concatenates every page into
    # ONE FLAT array — NOT a list-of-lists. (--slurp is a jq flag, not a
    # `gh api` flag; real gh rejects it outright, so it must not be passed
    # here — issue #202, Bug 1.)
    if ! list_json="$(GH_TOKEN="${_INSTALL_TOKEN}" gh api --paginate "repos/${REPO_SLUG}/rulesets" 2>"${list_err}")"; then
        echo "provision-ruleset: FAILURE — GET /repos/${REPO_SLUG}/rulesets (LIST) failed; refusing to treat this as absent:" >&2
        cat "${list_err}" >&2
        rm -f "${list_err}"
        return 1
    fi
    rm -f "${list_err}"
    "${_PYTHON}" -c "
import json, sys
entries = json.loads(sys.argv[1])
for entry in entries:
    if entry.get('name') == sys.argv[2]:
        print(entry['id'])
        sys.exit(0)
" "${list_json}" "${target_name}"
}

# ---------------------------------------------------------------------------
# _COMPARE_KEYS: structural keys to compare between desired and live state.
# Excludes:
#   - _comment     (Task 1 uses this for JSON pseudo-comments; no GitHub field)
#   - id, source, source_type, _links, node_id, *_at, current_user_can_bypass
#     (server-managed fields present in GET responses but absent in PUT/POST)
# ---------------------------------------------------------------------------
_COMPARE_KEYS="$(cat "${HARNESS_DIR}/config/ruleset.compare-keys.json")"

# ---------------------------------------------------------------------------
# Apply one ruleset: list+filter, GET-by-id, compare, PUT/POST.
# ---------------------------------------------------------------------------
_apply_ruleset() {
    local name="$1"
    local desired_path="$2"
    local desired
    desired="$(_render_config "${desired_path}")"

    local existing_id
    if ! existing_id="$(_lookup_id "${name}")"; then
        echo "provision-ruleset: FAILURE — could not determine current state for '${name}'; aborting rather than risk a spurious create." >&2
        exit 1
    fi

    if [[ -z "${existing_id}" ]]; then
        echo "provision-ruleset: ${name} absent — POST-ing"
        printf '%s' "${desired}" | GH_TOKEN="${_INSTALL_TOKEN}" gh api \
            --method POST \
            "repos/${REPO_SLUG}/rulesets" \
            --input -
        return 0
    fi

    local current_body
    current_body="$(GH_TOKEN="${_INSTALL_TOKEN}" gh api "repos/${REPO_SLUG}/rulesets/${existing_id}")"

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
    printf '%s' "${desired}" | GH_TOKEN="${_INSTALL_TOKEN}" gh api \
        --method PUT \
        "repos/${REPO_SLUG}/rulesets/${existing_id}" \
        --input -
}

_apply_ruleset "harness-main-no-merge" "${HARNESS_DIR}/config/ruleset.main.json"
_apply_ruleset "harness-feature-daemon-only" "${HARNESS_DIR}/config/ruleset.feature.json"

# ---------------------------------------------------------------------------
# Baseline capture (#206): pin ruleset_id + updated_at per ruleset so the
# App-token-safe per-launch preflight (chain.ruleset_status.
# check_ruleset_signals) can detect drift without needing bypass_actors
# read access. Runs as the FINAL step, after any POST/PUT above, so a
# legitimate ruleset edit made by this run is captured in the fresh pin
# rather than immediately flagged as drift on the next preflight.
#
# Non-fatal by design: a missing BH_PROJECT_ROOT or an unresolvable
# ruleset id only warns and skips the capture — provisioning itself has
# already succeeded (or no-op'd) above, and the operator can re-run this
# script once BH_PROJECT_ROOT is set to pin the baseline.
# ---------------------------------------------------------------------------
_capture_baseline() {
    if [[ -z "${BH_PROJECT_ROOT:-}" ]]; then
        echo "provision-ruleset: WARNING — BH_PROJECT_ROOT not set; skipping ruleset baseline capture." >&2
        return 0
    fi

    local baseline_dir="${BH_PROJECT_ROOT}/.bh"
    local baseline_path="${baseline_dir}/ruleset-baseline.json"
    mkdir -p "${baseline_dir}"

    local main_id feat_id
    if ! main_id="$(_lookup_id "harness-main-no-merge")"; then
        echo "provision-ruleset: WARNING — could not resolve harness-main-no-merge id for baseline capture; skipping." >&2
        return 0
    fi
    if ! feat_id="$(_lookup_id "harness-feature-daemon-only")"; then
        echo "provision-ruleset: WARNING — could not resolve harness-feature-daemon-only id for baseline capture; skipping." >&2
        return 0
    fi
    if [[ -z "${main_id}" || -z "${feat_id}" ]]; then
        echo "provision-ruleset: WARNING — one or both rulesets not found after provisioning; skipping baseline capture." >&2
        return 0
    fi

    local main_body feat_body
    if ! main_body="$(GH_TOKEN="${_INSTALL_TOKEN}" gh api "repos/${REPO_SLUG}/rulesets/${main_id}")"; then
        echo "provision-ruleset: WARNING — could not fetch ruleset harness-main-no-merge body for baseline capture; skipping." >&2
        return 0
    fi
    if ! feat_body="$(GH_TOKEN="${_INSTALL_TOKEN}" gh api "repos/${REPO_SLUG}/rulesets/${feat_id}")"; then
        echo "provision-ruleset: WARNING — could not fetch ruleset harness-feature-daemon-only body for baseline capture; skipping." >&2
        return 0
    fi

    if ! "${_PYTHON}" -c '
import json, sys

owner_repo, main_id, main_body, feat_id, feat_body, baseline_path = sys.argv[1:7]


def _entry(ruleset_id, body):
    parsed = json.loads(body)
    return {"ruleset_id": int(ruleset_id), "updated_at": parsed["updated_at"]}


try:
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    baseline = {}

baseline[owner_repo] = {
    "harness-main-no-merge": _entry(main_id, main_body),
    "harness-feature-daemon-only": _entry(feat_id, feat_body),
}

with open(baseline_path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(baseline, f, indent=2)
    f.write("\n")
' "${REPO_SLUG}" "${main_id}" "${main_body}" "${feat_id}" "${feat_body}" "${baseline_path}"; then
        echo "provision-ruleset: WARNING — failed to write ruleset baseline (parse/write error); skipping." >&2
        return 0
    fi

    echo "provision-ruleset: baseline pinned at ${baseline_path} (main id=${main_id}, feature id=${feat_id})"
}

_capture_baseline

echo "provision-ruleset: complete"
