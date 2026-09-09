#!/usr/bin/env bash
# Delegate service installation to the independently installed CodeReeve wheel.

# Disable inherited xtrace before any environment value can reach trace output.
set +x
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bin/lib/bootstrap-alias.sh
source "${SCRIPT_DIR}/lib/bootstrap-alias.sh"
_codereeve_bootstrap_alias CODEREEVE_ROOT BATON_HARNESS_DIR

original_arguments=("$@")
harness_override=""
environment_override=""
index=0
while (( index < ${#original_arguments[@]} )); do
    argument="${original_arguments[index]}"
    case "${argument}" in
        --harness-dir|--environment)
            if (( index + 1 >= ${#original_arguments[@]} )); then
                printf 'codereeve service installer: %s requires a path\n' "${argument}" >&2
                exit 2
            fi
            if [[ "${argument}" == "--harness-dir" ]]; then
                harness_override="${original_arguments[index + 1]}"
            else
                environment_override="${original_arguments[index + 1]}"
            fi
            ((index += 2))
            ;;
        --harness-dir=*)
            harness_override="${argument#*=}"
            ((index += 1))
            ;;
        --environment=*)
            environment_override="${argument#*=}"
            ((index += 1))
            ;;
        *)
            ((index += 1))
            ;;
    esac
done

if [[ -n "${harness_override}" ]]; then
    harness_dir="${harness_override}"
elif [[ -n "${CODEREEVE_ROOT:-}" ]]; then
    harness_dir="${CODEREEVE_ROOT}"
elif harness_dir="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null)"; then
    :
else
    harness_dir="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

if [[ -n "${environment_override}" ]]; then
    candidate_environment="${environment_override}"
else
    candidate_environment="${harness_dir}/.venv-codereeve"
fi
candidate_python="${candidate_environment}/bin/python"
if [[ ! -x "${candidate_python}" ]]; then
    printf 'codereeve service installer: candidate Python is unavailable\n' >&2
    exit 1
fi

forwarded=("${original_arguments[@]}")
if [[ -z "${harness_override}" ]]; then
    forwarded+=(--harness-dir "${harness_dir}")
fi
if [[ -z "${environment_override}" ]]; then
    forwarded+=(--environment "${candidate_environment}")
fi
exec "${candidate_python}" -I -m codereeve.service_cutover.cli "${forwarded[@]}"
