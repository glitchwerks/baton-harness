# shellcheck shell=bash
# Read literal configuration through the installed CodeReeve parser.
# Source this library; host.env and config.env are never shell programs.
_codereeve_load_config() {
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
    while IFS= read -r -d '' _codereeve_key; do
        if ! IFS= read -r -d '' _codereeve_value; then
            rm -f -- "${_codereeve_records}"
            return 1
        fi
        if ! export "${_codereeve_key}=${_codereeve_value}"; then
            rm -f -- "${_codereeve_records}"
            return 1
        fi
    done < "${_codereeve_records}"
    rm -f -- "${_codereeve_records}"
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
        if ! read -r -p "baton-harness: existing config found at ${_bh_config_file}; type 'overwrite' to replace it, or press Enter to reuse it: " _bh_answer; then
            echo "baton-harness: error: could not read overwrite-or-reuse choice" >&2
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
        echo "baton-harness: error: existing config ${_bh_config_file} requires an interactive overwrite-or-reuse choice" >&2
        return 1
    fi
}


_codereeve_load_config
