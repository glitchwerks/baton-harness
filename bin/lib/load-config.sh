# shellcheck shell=bash
# Read literal configuration through the installed CodeReeve parser.
# Source this library; host.env and config.env are never shell programs.
# Optional arguments are supplied by callers sourcing this library directly.
# shellcheck disable=SC2120
_codereeve_load_config() {
    # Bootstrap must reject conflicting spellings before choosing code to run.
    if [[ ${CODEREEVE_VENV+x} && ${BH_VENV+x} && "${CODEREEVE_VENV-}" != "${BH_VENV-}" ]]; then
        echo "codereeve: conflicting environment variables: CODEREEVE_VENV and BH_VENV" >&2
        return 1
    fi
    local _codereeve_root _codereeve_venv _codereeve_python
    _codereeve_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || return 1
    _codereeve_venv="${CODEREEVE_VENV-${BH_VENV-${_codereeve_root}/.venv}}"
    if [[ -x "${_codereeve_venv}/Scripts/python.exe" ]]; then
        _codereeve_python="${_codereeve_venv}/Scripts/python.exe"
    elif [[ -x "${_codereeve_venv}/bin/python" ]]; then
        _codereeve_python="${_codereeve_venv}/bin/python"
    else
        echo "codereeve: configuration bridge interpreter unavailable" >&2
        return 1
    fi
    local _codereeve_records _codereeve_key _codereeve_value
    _codereeve_records="$(umask 077; mktemp)" || return 1
    if ! chmod 600 "${_codereeve_records}"; then
        rm -f -- "${_codereeve_records}"
        return 1
    fi
    if ! "${_codereeve_python}" -m codereeve.config_bridge --format nul "$@" > "${_codereeve_records}"; then
        rm -f -- "${_codereeve_records}"
        return 1
    fi
    # Keep the open descriptor, then unlink while the operator environment
    # is still intact. No external commands may run after config exports.
    local _codereeve_fd
    if ! exec {_codereeve_fd}< "${_codereeve_records}"; then
        rm -f -- "${_codereeve_records}"
        return 1
    fi
    if ! rm -f -- "${_codereeve_records}"; then
        exec {_codereeve_fd}<&-
        return 1
    fi
    local -a _codereeve_keys=() _codereeve_values=()
    local _codereeve_valid=1 _codereeve_index
    while true; do
        _codereeve_key=""
        if ! IFS= read -r -d '' _codereeve_key <&"${_codereeve_fd}"; then
            [[ -z "${_codereeve_key}" ]] || _codereeve_valid=0
            break
        fi
        if ! IFS= read -r -d '' _codereeve_value <&"${_codereeve_fd}"; then
            _codereeve_valid=0
            break
        fi
        if [[ ! "${_codereeve_key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] ||
           [[ "${_codereeve_key}" == _codereeve_* && "${_codereeve_key}" != _codereeve_state_directory ]]; then
            _codereeve_valid=0
            break
        fi
        _codereeve_keys+=("${_codereeve_key}")
        _codereeve_values+=("${_codereeve_value}")
    done
    exec {_codereeve_fd}<&-
    if [[ "${_codereeve_valid}" != 1 ]]; then
        echo "codereeve: invalid configuration bridge protocol" >&2
        return 1
    fi
    # Preflight readonly/special shell variables in an isolated subshell so
    # an unexportable record cannot leave half the caller's config applied.
    if ! (
        for _codereeve_index in "${!_codereeve_keys[@]}"; do
            export "${_codereeve_keys[_codereeve_index]}=${_codereeve_values[_codereeve_index]}" || exit 1
        done
    ) 2>/dev/null; then
        echo "codereeve: configuration contains an unexportable variable" >&2
        return 1
    fi
    for _codereeve_index in "${!_codereeve_keys[@]}"; do
        export "${_codereeve_keys[_codereeve_index]}=${_codereeve_values[_codereeve_index]}" || return 1
    done
    return 0
}

_bh_resolve_config_with_reuse_prompt() {
    local _bh_config_file="$1"
    local _bh_prompt_and_write_fn="$2"

    if [[ ! -f "${_bh_config_file}" ]]; then
        "${_bh_prompt_and_write_fn}"
        return $?
    fi

    if [[ -t 0 && -t 1 && "${CODEREEVE_SETUP_NO_PROMPT:-0}" != "1" ]]; then
        local _bh_answer
        if ! read -r -p "codereeve: existing config found at ${_bh_config_file}; type 'overwrite' to replace it, or press Enter to reuse it: " _bh_answer; then
            echo "codereeve: error: could not read overwrite-or-reuse choice" >&2
            return 1
        fi
        case "${_bh_answer}" in
            [Oo][Vv][Ee][Rr][Ww][Rr][Ii][Tt][Ee])
                "${_bh_prompt_and_write_fn}"
                return $?
                ;;
        esac
        return 0
    else
        echo "codereeve: error: existing config ${_bh_config_file} requires an interactive overwrite-or-reuse choice" >&2
        return 1
    fi
}


# Entrypoint arguments belong to the caller, not to the config bridge.
# shellcheck disable=SC2119
_codereeve_load_config
