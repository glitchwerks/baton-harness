#!/usr/bin/env bash
# Resolve an operator-only bootstrap pair before package Python exists.
# Values are literal; diagnostics expose variable names, never their values.

_codereeve_bootstrap_alias() {
    local canonical="$1" legacy="$2"
    if [[ -v "$canonical" && -v "$legacy" && "${!canonical}" != "${!legacy}" ]]; then
        printf 'conflicting environment variables: %s and %s\n' "$canonical" "$legacy" >&2
        return 1
    fi
    if [[ -v "$legacy" && ! -v "$canonical" ]]; then
        printf -v "$canonical" '%s' "${!legacy}"
        export "${canonical?}"
    fi
}
