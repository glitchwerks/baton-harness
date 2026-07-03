# bin/lib/load-config.sh — shared env-config loader for bin/ entrypoints (#197)
#
# SOURCE THIS FILE — do not execute it standalone (it has no shebang and
# defines nothing runnable on its own).
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/load-config.sh"
#
# Resolves the harness's standard two-file config chain, in order:
#
#   1. ${XDG_CONFIG_HOME:-${HOME}/.config}/baton-harness/host.env
#        -> provides BH_PROJECT_ROOT (written by bin/setup-env.sh)
#   2. ${BH_PROJECT_ROOT}/.bh/config.env
#        -> provides BH_REPO_OWNER, BH_REPO_NAME, BH_GITHUB_APP_ID,
#           BH_GITHUB_APP_INSTALLATION_ID, BWS_*_SECRET_ID, etc.
#           (written by bin/init-sandbox.sh; requires BH_PROJECT_ROOT to
#           already be known from step 1, or from an operator export
#           made before this file was sourced)
#
# Operator-env-wins precedence (docs/smoke-test-daemon.md, "Operator
# override": "explicit env values win over .bh/config.env, host.env,
# and vault-fetch, in that order"): any variable already set/exported
# in the calling shell BEFORE this file is sourced is never overwritten
# by either config file below. Only variables that were unset beforehand
# pick up a value from host.env or config.env.
#
# Each source is guarded by a file-exists check — a missing file is not
# fatal here. Callers apply their own required-var gates after sourcing
# this file, so absence of either config file surfaces as a normal
# "missing required env var" error from the caller, not from here.

# ---------------------------------------------------------------------------
# _bh_source_env_preserving_overrides <file>
#
# Sources <file> (a no-op if it does not exist) while preserving the
# pre-existing value of every variable the file assigns.
#
# How it works: the set of assignment targets in <file> is found with a
# plain `grep` for lines of the form `[export ]NAME=` (comments and
# blank lines never match, since the match is anchored at line start).
# For each target already set in the environment, its current value is
# snapshotted *before* sourcing. The file is then sourced normally —
# full bash grammar (export, quoting, comments, command substitution,
# etc.) behaves exactly as a plain `source` would, so this changes
# nothing about how host.env/config.env are parsed. Afterwards, every
# snapshotted name is re-exported to its pre-source value, undoing any
# clobber the source may have performed.
#
# Restoration uses a plain `export NAME=value` (never `declare`/`local`
# on the target name) so it correctly reaches the caller's global
# environment even though this runs inside a function — an unqualified
# `export` inside a bash function modifies the nearest existing scope
# (global, here) rather than creating a function-local shadow the way
# `declare`/`local` would.
# ---------------------------------------------------------------------------
_bh_source_env_preserving_overrides() {
    local _bh_file="$1"
    [[ -f "${_bh_file}" ]] || return 0

    local _bh_keys
    _bh_keys="$(grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "${_bh_file}" 2>/dev/null \
                | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//')" || true

    local _bh_key
    local -a _bh_prior_names=()
    local -a _bh_prior_values=()
    while IFS= read -r _bh_key; do
        [[ -z "${_bh_key}" ]] && continue
        if [[ -n "${!_bh_key+x}" ]]; then
            _bh_prior_names+=("${_bh_key}")
            _bh_prior_values+=("${!_bh_key}")
        fi
    done <<< "${_bh_keys}"

    # shellcheck disable=SC1090
    source "${_bh_file}"

    local _bh_i
    for (( _bh_i = 0; _bh_i < ${#_bh_prior_names[@]}; _bh_i++ )); do
        export "${_bh_prior_names[_bh_i]}=${_bh_prior_values[_bh_i]}"
    done

    return 0
}

# ---------------------------------------------------------------------------
# 1. Per-host config (written by bin/setup-env.sh) -> BH_PROJECT_ROOT
# ---------------------------------------------------------------------------
_BH_HOST_ENV="${XDG_CONFIG_HOME:-${HOME}/.config}/baton-harness/host.env"
_bh_source_env_preserving_overrides "${_BH_HOST_ENV}"
unset _BH_HOST_ENV

# ---------------------------------------------------------------------------
# 2. Sandbox config (written by bin/init-sandbox.sh). Only attempted once
#    BH_PROJECT_ROOT is known (from step 1, or an operator export made
#    before this file was sourced) — config.env lives under that root.
# ---------------------------------------------------------------------------
if [[ -n "${BH_PROJECT_ROOT:-}" ]]; then
    _bh_source_env_preserving_overrides "${BH_PROJECT_ROOT}/.bh/config.env"
fi

unset -f _bh_source_env_preserving_overrides
