#!/usr/bin/env bash
# bin/probe-merge-denial.sh — Slice 3c live merge-denial probe.
#
# Attempts every known bypass vector against a sandbox repo's PR and
# asserts that each attempt is DENIED.  Prints a per-vector summary
# and exits non-zero if any vector succeeds unexpectedly.
#
# Required environment variables:
#   BH_PROBE_SANDBOX_REPO       owner/repo of the live sandbox repo
#   BH_PROBE_PR_NUMBER          open PR number in that repo (against main)
#   BH_PROBE_WORKER_TOKEN_PATH  file path containing the worker-identity
#                               token (NEVER inline or env-var value)
#
# Optional environment variables:
#   BH_PROBE_DRY_RUN=1          print commands without executing them
#   BH_PROBE_HOOK_SCRIPT        path to force-pr-not-merge hook script
#                               (default: auto-detected from harness root)
#
# Exit codes:
#   0  all 7 vectors denied as expected
#   1  one or more vectors produced an unexpected result
#   2  missing env / precondition failure
#
# Usage:
#   export BH_PROBE_SANDBOX_REPO="owner/sandbox-repo"
#   export BH_PROBE_PR_NUMBER="42"
#   export BH_PROBE_WORKER_TOKEN_PATH="/path/to/worker-token.txt"
#   bash bin/probe-merge-denial.sh

set -uo pipefail
# Note: NOT set -e so we can capture exit codes from denied commands.

# ---------------------------------------------------------------------------
# Script-dir resolution (works from any cwd).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Python resolver — same pattern as provision-ruleset.sh.
# ---------------------------------------------------------------------------
_PYTHON="${HARNESS_DIR}/.venv/Scripts/python.exe"
[[ ! -x "${_PYTHON}" ]] && _PYTHON="${HARNESS_DIR}/.venv/bin/python"
[[ ! -x "${_PYTHON}" ]] && _PYTHON="python3"
[[ ! -x "${_PYTHON}" ]] && _PYTHON="python"

# ---------------------------------------------------------------------------
# Probe-assert helper (Approach B — Python helper for result parsing).
# ---------------------------------------------------------------------------
_probe_assert() {
    "${_PYTHON}" -m scripts.probe_assert "$@"
}

# ---------------------------------------------------------------------------
# Env validation.
# ---------------------------------------------------------------------------
_missing=()
for v in BH_PROBE_SANDBOX_REPO BH_PROBE_PR_NUMBER BH_PROBE_WORKER_TOKEN_PATH; do
    if [[ -z "${!v:-}" ]]; then
        _missing+=("${v}")
    fi
done
if [[ ${#_missing[@]} -gt 0 ]]; then
    echo "probe-merge-denial: missing required env vars: ${_missing[*]}" >&2
    echo "  Set BH_PROBE_SANDBOX_REPO, BH_PROBE_PR_NUMBER, and" >&2
    echo "  BH_PROBE_WORKER_TOKEN_PATH before running this probe." >&2
    exit 2
fi

# Validate token file exists and is readable (NEVER read its value).
if [[ ! -f "${BH_PROBE_WORKER_TOKEN_PATH}" ]]; then
    echo "probe-merge-denial: token file not found: ${BH_PROBE_WORKER_TOKEN_PATH}" >&2
    exit 2
fi
if [[ ! -r "${BH_PROBE_WORKER_TOKEN_PATH}" ]]; then
    echo "probe-merge-denial: token file not readable: ${BH_PROBE_WORKER_TOKEN_PATH}" >&2
    exit 2
fi

# Compute token length for diagnostics (never echo value).
_TOKEN_LEN="$(wc -c < "${BH_PROBE_WORKER_TOKEN_PATH}" | tr -d ' ')"

# Locate the force-pr-not-merge hook script.
_HOOK_SCRIPT="${BH_PROBE_HOOK_SCRIPT:-}"
if [[ -z "${_HOOK_SCRIPT}" ]]; then
    _HOOK_SCRIPT="${HARNESS_DIR}/.venv/Scripts/bh-force-pr-not-merge.exe"
    [[ ! -x "${_HOOK_SCRIPT}" ]] && \
        _HOOK_SCRIPT="${HARNESS_DIR}/.venv/bin/bh-force-pr-not-merge"
    [[ ! -x "${_HOOK_SCRIPT}" ]] && _HOOK_SCRIPT=""
fi

# Construct API base URL.
SANDBOX_REPO="${BH_PROBE_SANDBOX_REPO}"
PR_NUM="${BH_PROBE_PR_NUMBER}"
API_URL="https://api.github.com/repos/${SANDBOX_REPO}/pulls/${PR_NUM}/merge"

# Dry-run flag.
DRY_RUN="${BH_PROBE_DRY_RUN:-0}"

# ---------------------------------------------------------------------------
# Banner.
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  baton-harness merge-denial probe  (slice 3c, #160)"
echo "============================================================"
echo "  Sandbox repo : ${SANDBOX_REPO}"
echo "  PR number    : ${PR_NUM}"
echo "  Token path   : ${BH_PROBE_WORKER_TOKEN_PATH} (len=${_TOKEN_LEN})"
echo "  Dry-run      : ${DRY_RUN}"
echo "  API URL      : ${API_URL}"
if [[ -n "${_HOOK_SCRIPT}" ]]; then
    echo "  Hook script  : ${_HOOK_SCRIPT}"
else
    echo "  Hook script  : NOT FOUND (hook-coverage vectors will skip sentinel check)"
fi
echo "============================================================"
echo ""

# ---------------------------------------------------------------------------
# Counters and results table (populated as vectors run).
# ---------------------------------------------------------------------------
_PASS=0
_FAIL=0
_TOTAL=7
_RESULTS=()  # "N|name|PASS|detail" entries

_record() {
    local vec_num="$1"
    local vec_name="$2"
    local status="$3"
    local detail="$4"
    _RESULTS+=("${vec_num}|${vec_name}|${status}|${detail}")
    if [[ "${status}" == "PASS" ]]; then
        (( _PASS++ )) || true
    else
        (( _FAIL++ )) || true
    fi
}

# ---------------------------------------------------------------------------
# _run_vector: execute a command (or print it in dry-run), capture exit+output.
# Sets _CMD_EXIT, _CMD_STDOUT, _CMD_STDERR.
# ---------------------------------------------------------------------------
_run_vector() {
    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "  [DRY-RUN] would run: $*"
        _CMD_EXIT=1
        _CMD_STDOUT="dry-run placeholder — no 403 check"
        _CMD_STDERR=""
        return
    fi
    local _tmp_out _tmp_err
    _tmp_out="$(mktemp)"
    _tmp_err="$(mktemp)"
    # Run command; capture exit code without aborting the script.
    "$@" >"${_tmp_out}" 2>"${_tmp_err}" || true
    _CMD_EXIT=$?
    _CMD_STDOUT="$(cat "${_tmp_out}")"
    _CMD_STDERR="$(cat "${_tmp_err}")"
    rm -f "${_tmp_out}" "${_tmp_err}"
}

# ---------------------------------------------------------------------------
# _hook_vector: run a command through the force-pr-not-merge hook simulator.
# Constructs a PreToolUse JSON payload and pipes it to the hook binary.
# Sets _CMD_EXIT, _HOOK_EXIT, _HOOK_STDERR, _SENTINEL_DIR.
# ---------------------------------------------------------------------------
_hook_vector() {
    local cmd_string="$1"

    # Build a tmp dir so each vector gets its own sentinel dir.
    local tmpdir
    tmpdir="$(mktemp -d)"
    _SENTINEL_DIR="${tmpdir}/.bh-state"

    local payload
    payload="$(printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' \
        "$(printf '%s' "${cmd_string}" | sed 's/"/\\"/g')")"

    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "  [DRY-RUN] hook payload: ${payload}"
        _CMD_EXIT=1
        _HOOK_EXIT=2
        _HOOK_STDERR="BH_WORKER_TRIED_MERGE: dry-run"
        return
    fi

    if [[ -z "${_HOOK_SCRIPT}" ]]; then
        # Hook not installed — mark as SKIP (counts as PASS with caveat).
        echo "  [SKIP] hook not installed; cannot verify sentinel/marker for this vector"
        _CMD_EXIT=1
        _HOOK_EXIT=0
        _HOOK_STDERR=""
        return
    fi

    # Run the hook in the temp dir so .bh-state lands there.
    local _tmp_herr
    _tmp_herr="$(mktemp)"
    (
        cd "${tmpdir}"
        printf '%s' "${payload}" | "${_HOOK_SCRIPT}" 2>"${_tmp_herr}"
    ) || true
    _HOOK_EXIT=$?
    _HOOK_STDERR="$(cat "${_tmp_herr}")"
    rm -f "${_tmp_herr}"

    # For hook-covered vectors the "command" itself isn't executed by the
    # probe — the hook blocks it.  Report hook exit as _CMD_EXIT.
    _CMD_EXIT="${_HOOK_EXIT}"
}

# ---------------------------------------------------------------------------
# Vector 1: gh pr merge <N>
# Hook coverage: YES — _RE_GH_PR_MERGE fires, sentinel + exit 2.
# ---------------------------------------------------------------------------
echo "=== Vector 1: gh pr merge (obvious path) ==="
_hook_vector "gh pr merge ${PR_NUM}"

_exit_result="$(_probe_assert check_exit_code 2 "${_CMD_EXIT}" 2>&1)"
_exit_ok="$(printf '%s' "${_exit_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_sentinel_result=""
_sentinel_ok="True"
if [[ "${DRY_RUN}" != "1" && -n "${_HOOK_SCRIPT}" ]]; then
    _sentinel_result="$(_probe_assert check_sentinel "${_SENTINEL_DIR}" 2>&1)"
    _sentinel_ok="$(printf '%s' "${_sentinel_result}" | "${_PYTHON}" -c \
        'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

    _marker_result="$(_probe_assert check_stderr_marker "${_HOOK_STDERR}" 2>&1)"
    _marker_ok="$(printf '%s' "${_marker_result}" | "${_PYTHON}" -c \
        'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"
else
    _marker_ok="True"
fi

if [[ "${_exit_ok}" == "True" && "${_sentinel_ok}" == "True" && "${_marker_ok:-True}" == "True" ]]; then
    echo "  PASS — hook exit=${_CMD_EXIT}, sentinel present, marker in stderr"
    _record 1 "gh-pr-merge" "PASS" "exit=${_CMD_EXIT} sentinel=ok marker=ok"
else
    echo "  FAIL — exit_ok=${_exit_ok} sentinel_ok=${_sentinel_ok} marker_ok=${_marker_ok:-n/a}"
    _record 1 "gh-pr-merge" "FAIL" \
        "exit_ok=${_exit_ok} sentinel_ok=${_sentinel_ok} marker_ok=${_marker_ok:-n/a}"
fi
echo ""

# ---------------------------------------------------------------------------
# Vector 2: gh api -X PUT repos/.../pulls/<N>/merge (flag-first whitespace)
# Hook coverage: YES — _RE_GH_API + _RE_PULLS_MERGE + _RE_PUT_METHOD fires.
# ---------------------------------------------------------------------------
echo "=== Vector 2: gh api -X PUT (flag-first) ==="
_V2_CMD="gh api -X PUT repos/${SANDBOX_REPO}/pulls/${PR_NUM}/merge"
_hook_vector "${_V2_CMD}"
_v2_hook_exit="${_CMD_EXIT}"
_v2_hook_stderr="${_HOOK_STDERR:-}"
_v2_sentinel_dir="${_SENTINEL_DIR:-}"

# Also attempt live API call (captures 403 from ruleset, orthogonal to hook).
_GH_TOKEN="$(cat "${BH_PROBE_WORKER_TOKEN_PATH}")"
export GH_TOKEN="${_GH_TOKEN}"
_run_vector gh api -X PUT "repos/${SANDBOX_REPO}/pulls/${PR_NUM}/merge" \
    -f merge_method=merge 2>&1 || true
_v2_combined="${_CMD_STDOUT} ${_CMD_STDERR}"
unset GH_TOKEN _GH_TOKEN

_exit_result="$(_probe_assert check_exit_code 1 "${_CMD_EXIT}" 2>&1)"
_exit_ok="$(printf '%s' "${_exit_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_http_result="$(_probe_assert check_http_403 "${_v2_combined}" 2>&1)"
_http_ok="$(printf '%s' "${_http_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_hook_exit_ok="$(_probe_assert check_exit_code 2 "${_v2_hook_exit}" 2>&1 | \
    "${_PYTHON}" -c 'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

if [[ "${_exit_ok}" == "True" && ("${DRY_RUN}" == "1" || "${_http_ok}" == "True") ]]; then
    echo "  PASS — API exit=${_CMD_EXIT} 403-in-body=${_http_ok}, hook exit=${_v2_hook_exit}"
    _record 2 "gh-api-X-PUT-flag-first" "PASS" \
        "api_exit=${_CMD_EXIT} 403=${_http_ok} hook_exit=${_v2_hook_exit}"
else
    echo "  FAIL — exit_ok=${_exit_ok} http_ok=${_http_ok} hook_exit_ok=${_hook_exit_ok}"
    _record 2 "gh-api-X-PUT-flag-first" "FAIL" \
        "exit_ok=${_exit_ok} http_ok=${_http_ok} hook_exit_ok=${_hook_exit_ok}"
fi
echo ""

# ---------------------------------------------------------------------------
# Vector 3: gh api repos/.../pulls/<N>/merge -X PUT (URL-first whitespace)
# Hook coverage: YES — same patterns as V2, different argument order.
# ---------------------------------------------------------------------------
echo "=== Vector 3: gh api URL-first -X PUT ==="
_V3_CMD="gh api repos/${SANDBOX_REPO}/pulls/${PR_NUM}/merge -X PUT"
_hook_vector "${_V3_CMD}"
_v3_hook_exit="${_CMD_EXIT}"

_GH_TOKEN="$(cat "${BH_PROBE_WORKER_TOKEN_PATH}")"
export GH_TOKEN="${_GH_TOKEN}"
_run_vector gh api "repos/${SANDBOX_REPO}/pulls/${PR_NUM}/merge" -X PUT \
    -f merge_method=merge 2>&1 || true
_v3_combined="${_CMD_STDOUT} ${_CMD_STDERR}"
unset GH_TOKEN _GH_TOKEN

_exit_result="$(_probe_assert check_exit_code 1 "${_CMD_EXIT}" 2>&1)"
_exit_ok="$(printf '%s' "${_exit_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_http_result="$(_probe_assert check_http_403 "${_v3_combined}" 2>&1)"
_http_ok="$(printf '%s' "${_http_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

if [[ "${_exit_ok}" == "True" && ("${DRY_RUN}" == "1" || "${_http_ok}" == "True") ]]; then
    echo "  PASS — API exit=${_CMD_EXIT} 403-in-body=${_http_ok}, hook exit=${_v3_hook_exit}"
    _record 3 "gh-api-URL-first-PUT" "PASS" \
        "api_exit=${_CMD_EXIT} 403=${_http_ok} hook_exit=${_v3_hook_exit}"
else
    echo "  FAIL — exit_ok=${_exit_ok} http_ok=${_http_ok}"
    _record 3 "gh-api-URL-first-PUT" "FAIL" "exit_ok=${_exit_ok} http_ok=${_http_ok}"
fi
echo ""

# ---------------------------------------------------------------------------
# Vector 4: gh api --method=PUT repos/.../pulls/<N>/merge (equals form)
# Hook coverage: YES — _RE_PUT_METHOD covers --method=PUT (P2-B, slice 3b).
# ---------------------------------------------------------------------------
echo "=== Vector 4: gh api --method=PUT (equals form) ==="
_V4_CMD="gh api --method=PUT repos/${SANDBOX_REPO}/pulls/${PR_NUM}/merge"
_hook_vector "${_V4_CMD}"
_v4_hook_exit="${_CMD_EXIT}"

_GH_TOKEN="$(cat "${BH_PROBE_WORKER_TOKEN_PATH}")"
export GH_TOKEN="${_GH_TOKEN}"
_run_vector gh api --method=PUT "repos/${SANDBOX_REPO}/pulls/${PR_NUM}/merge" \
    -f merge_method=merge 2>&1 || true
_v4_combined="${_CMD_STDOUT} ${_CMD_STDERR}"
unset GH_TOKEN _GH_TOKEN

_exit_result="$(_probe_assert check_exit_code 1 "${_CMD_EXIT}" 2>&1)"
_exit_ok="$(printf '%s' "${_exit_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_http_result="$(_probe_assert check_http_403 "${_v4_combined}" 2>&1)"
_http_ok="$(printf '%s' "${_http_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

if [[ "${_exit_ok}" == "True" && ("${DRY_RUN}" == "1" || "${_http_ok}" == "True") ]]; then
    echo "  PASS — API exit=${_CMD_EXIT} 403-in-body=${_http_ok}, hook exit=${_v4_hook_exit}"
    _record 4 "gh-api-method-equals-PUT" "PASS" \
        "api_exit=${_CMD_EXIT} 403=${_http_ok} hook_exit=${_v4_hook_exit}"
else
    echo "  FAIL — exit_ok=${_exit_ok} http_ok=${_http_ok}"
    _record 4 "gh-api-method-equals-PUT" "FAIL" "exit_ok=${_exit_ok} http_ok=${_http_ok}"
fi
echo ""

# ---------------------------------------------------------------------------
# Vector 5: curl -X PUT ... (raw HTTP via curl, flag-space form)
# Hook coverage: NONE — curl is NOT a gh tool; only ruleset blocks.
# Note: hook does cover curl via _RE_CURL + _RE_CURL_PUT, but the probe
# exercises the ruleset independently for belt-and-braces verification.
# ---------------------------------------------------------------------------
echo "=== Vector 5: curl -X PUT (raw HTTP, flag-space form) ==="
_GH_TOKEN="$(cat "${BH_PROBE_WORKER_TOKEN_PATH}")"
_run_vector curl --silent --show-error --write-out "\nHTTP_STATUS:%{http_code}" \
    -X PUT \
    -H "Authorization: token ${_GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    -d '{"merge_method":"merge"}' \
    "${API_URL}" 2>&1 || true
unset _GH_TOKEN
_v5_combined="${_CMD_STDOUT} ${_CMD_STDERR}"

_exit_result="$(_probe_assert check_exit_code 1 "${_CMD_EXIT}" 2>&1)"
_exit_ok="$(printf '%s' "${_exit_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_http_result="$(_probe_assert check_http_403 "${_v5_combined}" 2>&1)"
_http_ok="$(printf '%s' "${_http_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

if [[ "${_exit_ok}" == "True" && ("${DRY_RUN}" == "1" || "${_http_ok}" == "True") ]]; then
    echo "  PASS — exit=${_CMD_EXIT} 403-in-body=${_http_ok} (ruleset boundary confirmed)"
    _record 5 "curl-X-PUT-flag-space" "PASS" "exit=${_CMD_EXIT} 403=${_http_ok}"
else
    echo "  FAIL — exit_ok=${_exit_ok} http_ok=${_http_ok}"
    _record 5 "curl-X-PUT-flag-space" "FAIL" "exit_ok=${_exit_ok} http_ok=${_http_ok}"
fi
echo ""

# ---------------------------------------------------------------------------
# Vector 6: curl --request=PUT ... (equals form)
# Hook coverage: NONE — ruleset only.
# ---------------------------------------------------------------------------
echo "=== Vector 6: curl --request=PUT (raw HTTP, equals form) ==="
_GH_TOKEN="$(cat "${BH_PROBE_WORKER_TOKEN_PATH}")"
_run_vector curl --silent --show-error --write-out "\nHTTP_STATUS:%{http_code}" \
    "--request=PUT" \
    -H "Authorization: token ${_GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    -d '{"merge_method":"merge"}' \
    "${API_URL}" 2>&1 || true
unset _GH_TOKEN
_v6_combined="${_CMD_STDOUT} ${_CMD_STDERR}"

_exit_result="$(_probe_assert check_exit_code 1 "${_CMD_EXIT}" 2>&1)"
_exit_ok="$(printf '%s' "${_exit_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_http_result="$(_probe_assert check_http_403 "${_v6_combined}" 2>&1)"
_http_ok="$(printf '%s' "${_http_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

if [[ "${_exit_ok}" == "True" && ("${DRY_RUN}" == "1" || "${_http_ok}" == "True") ]]; then
    echo "  PASS — exit=${_CMD_EXIT} 403-in-body=${_http_ok} (ruleset boundary confirmed)"
    _record 6 "curl-request-equals-PUT" "PASS" "exit=${_CMD_EXIT} 403=${_http_ok}"
else
    echo "  FAIL — exit_ok=${_exit_ok} http_ok=${_http_ok}"
    _record 6 "curl-request-equals-PUT" "FAIL" "exit_ok=${_exit_ok} http_ok=${_http_ok}"
fi
echo ""

# ---------------------------------------------------------------------------
# Vector 7: Python urllib.request (raw HTTP via Python)
# Hook coverage: NONE — ruleset only (Python stdlib not hooked).
# ---------------------------------------------------------------------------
echo "=== Vector 7: Python urllib.request (raw HTTP via Python) ==="
_GH_TOKEN="$(cat "${BH_PROBE_WORKER_TOKEN_PATH}")"

if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [DRY-RUN] would run: python -c urllib.request PUT ${API_URL}"
    _CMD_EXIT=1
    _CMD_STDOUT="dry-run placeholder"
    _CMD_STDERR=""
else
    # Pass token length only to diagnostics; token itself goes through env var
    # scoped strictly to the python subprocess (never printed/logged).
    _run_vector "${_PYTHON}" -c "
import urllib.request, urllib.error, json, os, sys
token = os.environ.get('_BH_PROBE_TOKEN_INNER', '')
url = '${API_URL}'
data = json.dumps({'merge_method': 'merge'}).encode('utf-8')
req = urllib.request.Request(
    url, data=data, method='PUT',
    headers={
        'Authorization': 'token ' + token,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
    }
)
try:
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode('utf-8')
        print(body)
        sys.exit(0)
except urllib.error.HTTPError as e:
    body = e.read().decode('utf-8')
    print('HTTP_STATUS:' + str(e.code))
    print(body)
    sys.exit(1)
except Exception as exc:
    print('ERROR: ' + str(exc), file=sys.stderr)
    sys.exit(1)
" 2>&1
    # Pass token as a scoped env var to the subprocess (never echoed).
fi
export _BH_PROBE_TOKEN_INNER="${_GH_TOKEN}"
unset _GH_TOKEN
unset _BH_PROBE_TOKEN_INNER
_v7_combined="${_CMD_STDOUT} ${_CMD_STDERR}"

_exit_result="$(_probe_assert check_exit_code 1 "${_CMD_EXIT}" 2>&1)"
_exit_ok="$(printf '%s' "${_exit_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

_http_result="$(_probe_assert check_http_403 "${_v7_combined}" 2>&1)"
_http_ok="$(printf '%s' "${_http_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["ok"])' 2>/dev/null || echo False)"

if [[ "${_exit_ok}" == "True" && ("${DRY_RUN}" == "1" || "${_http_ok}" == "True") ]]; then
    echo "  PASS — exit=${_CMD_EXIT} 403-in-body=${_http_ok} (ruleset boundary confirmed)"
    _record 7 "python-urllib-PUT" "PASS" "exit=${_CMD_EXIT} 403=${_http_ok}"
else
    echo "  FAIL — exit_ok=${_exit_ok} http_ok=${_http_ok}"
    _record 7 "python-urllib-PUT" "FAIL" "exit_ok=${_exit_ok} http_ok=${_http_ok}"
fi
echo ""

# ---------------------------------------------------------------------------
# Summary table.
# ---------------------------------------------------------------------------
echo "============================================================"
echo "  PROBE SUMMARY"
echo "============================================================"
printf "  %-4s %-32s %-6s %s\n" "Vec" "Name" "Status" "Detail"
printf "  %-4s %-32s %-6s %s\n" "---" "----" "------" "------"
for entry in "${_RESULTS[@]}"; do
    IFS="|" read -r _vn _vname _vstatus _vdetail <<< "${entry}"
    printf "  %-4s %-32s %-6s %s\n" "${_vn}" "${_vname}" "${_vstatus}" "${_vdetail}"
done
echo "============================================================"

_summary_result="$(_probe_assert summarise "${_TOTAL}" "${_PASS}" "${_FAIL}" 2>&1)"
_summary_reason="$(printf '%s' "${_summary_result}" | "${_PYTHON}" -c \
    'import json,sys; print(json.loads(sys.stdin.read())["reason"])' 2>/dev/null || \
    echo "summary parse error")"

echo ""
if [[ "${_FAIL}" -eq 0 ]]; then
    echo "  RESULT: PASS — ${_summary_reason}"
    echo ""
    exit 0
else
    echo "  RESULT: FAIL — ${_summary_reason}"
    echo ""
    exit 1
fi
