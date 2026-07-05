#!/usr/bin/env bash
# bin/install-daemon-service.sh — One-shot systemd unit installer for bh-daemon (#208)
#
# Installs /etc/systemd/system/bh-daemon.service and /etc/bh-daemon/secrets.env,
# then enables and starts the bh-daemon service. Interaction model: auto-detect
# everything possible, print a summary, ask one [y/N] confirm; prompt only for
# values that cannot be resolved automatically.
#
# Usage:
#   bin/install-daemon-service.sh [--no-start] [--print-unit]
#                                  [--harness-dir PATH] [--project-root PATH]
#                                  [--user NAME] [--help|-h]
#
# Auto-detected:
#   HARNESS_DIR      Repo root of this script (git rev-parse --show-toplevel,
#                     falls back to dirname/..). Overridable via --harness-dir.
#   RUN_USER         ${SUDO_USER:-$(whoami)}. Overridable via --user.
#   BWS_BIN_DIR      RUN_USER's home (via getent, falls back to /home/$RUN_USER)
#                     + "/.local/bin" — matches where bin/setup-env.sh installs
#                     bws. Written into the unit's Environment=PATH= so the
#                     daemon can find bws under systemd's minimal PATH (#213).
#   BH_PROJECT_ROOT  Resolved via bin/lib/load-config.sh (host.env / config.env).
#                     Overridable via --project-root; prompted or fail-closed
#                     if still unresolved.
#
# Secret handling:
#   BWS_ACCESS_TOKEN is read from the environment if exported, else prompted
#   silently (read -r -s). Never echoed, logged, or exposed via `set -x`.
#   Written to /etc/bh-daemon/secrets.env as a bare KEY=value line (mode 600).
#
# Non-interactive mode (BH_SETUP_NO_PROMPT=1, or non-tty):
#   Skips the confirm prompt and any interactive prompt. Fails closed with a
#   clear message if a required value (BH_PROJECT_ROOT, BWS_ACCESS_TOKEN)
#   cannot be resolved without prompting.
#
# Safety:
#   Refuses to run if ANTHROPIC_API_KEY is set in the calling environment —
#   the daemon hard-aborts at startup if it sees that key (deployment model
#   mandates OAuth/subscription auth; see docs/smoke-test-daemon.md).
#
# Idempotent: an existing unit file or secrets.env is backed up
# (<name>.bak.<timestamp>) before being overwritten.

set -euo pipefail

# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: bin/install-daemon-service.sh [OPTIONS]

Installs and starts the bh-daemon systemd service.

Options:
  --no-start              Write the unit + secrets.env and daemon-reload only;
                           do not enable/start the service.
  --print-unit             Render the unit + secrets.env to stdout instead of
                           writing to /etc; skips all privileged writes and
                           systemctl calls. For dry-run inspection.
  --harness-dir PATH      Override auto-detected harness repo root.
  --project-root PATH     Override auto-detected BH_PROJECT_ROOT.
  --user NAME             Override auto-detected systemd User= (default:
                           ${SUDO_USER:-$(whoami)}).
  --help, -h              Show this help and exit.

Environment:
  BWS_ACCESS_TOKEN        Bitwarden Secrets CLI machine-account token.
                          Read from the environment if exported; otherwise
                          prompted for silently. Required — the script fails
                          closed in non-interactive mode if unresolved.
  BH_SETUP_NO_PROMPT=1    Skip the confirm prompt and any interactive prompt
                          (same convention as bin/setup-env.sh). Non-tty
                          stdin/stdout is treated the same way.

Writes:
  /etc/bh-daemon/secrets.env           mode 600, EnvironmentFile= source
  /etc/systemd/system/bh-daemon.service

After activation, prints a reminder that bin/provision-ruleset.sh must be
run once (not run by this script) to capture .bh/ruleset-baseline.json.
EOF
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

NO_START=0
PRINT_UNIT=0
HARNESS_DIR_OVERRIDE=""
PROJECT_ROOT_OVERRIDE=""
USER_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --no-start)
            NO_START=1
            shift
            ;;
        --print-unit)
            PRINT_UNIT=1
            shift
            ;;
        --harness-dir)
            HARNESS_DIR_OVERRIDE="${2-}"
            shift 2
            ;;
        --harness-dir=*)
            HARNESS_DIR_OVERRIDE="${1#*=}"
            shift
            ;;
        --project-root)
            PROJECT_ROOT_OVERRIDE="${2-}"
            shift 2
            ;;
        --project-root=*)
            PROJECT_ROOT_OVERRIDE="${1#*=}"
            shift
            ;;
        --user)
            USER_OVERRIDE="${2-}"
            shift 2
            ;;
        --user=*)
            USER_OVERRIDE="${1#*=}"
            shift
            ;;
        *)
            echo "baton-harness: error: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Interactive helper (same convention as bin/setup-env.sh / bin/init-sandbox.sh)
# ---------------------------------------------------------------------------

_bh_interactive() {
    [[ -t 0 && -t 1 && "${BH_SETUP_NO_PROMPT:-0}" != "1" ]]
}

# ---------------------------------------------------------------------------
# Guard: refuse to run at all if ANTHROPIC_API_KEY is set in the caller's
# shell. The daemon hard-aborts at startup if it sees this key (OAuth/
# subscription deployment model — docs/smoke-test-daemon.md "Credentials
# and auth on the server"). Checked first so the guard fires before any
# other resolution/prompt work.
# ---------------------------------------------------------------------------

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "baton-harness: error: ANTHROPIC_API_KEY is set in the calling environment." >&2
    echo "  bh-daemon hard-aborts at startup if it sees this key (OAuth/subscription" >&2
    echo "  auth is required — see docs/smoke-test-daemon.md). Unset it before" >&2
    echo "  re-running this installer:" >&2
    echo "    unset ANTHROPIC_API_KEY" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Auto-detect: HARNESS_DIR
# ---------------------------------------------------------------------------

if [[ -n "${HARNESS_DIR_OVERRIDE}" ]]; then
    HARNESS_DIR="${HARNESS_DIR_OVERRIDE}"
else
    _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if HARNESS_DIR="$(git -C "${_script_dir}" rev-parse --show-toplevel 2>/dev/null)"; then
        :
    else
        HARNESS_DIR="$(cd "${_script_dir}/.." && pwd)"
    fi
    unset _script_dir
fi

# Probe both venv layouts (POSIX bin/ for the systemd/Linux target, Scripts/
# for local dev-time verification on Windows) — mirrors bin/setup-env.sh.
BH_DAEMON_POSIX="${HARNESS_DIR}/.venv/bin/bh-daemon"
BH_DAEMON_WIN="${HARNESS_DIR}/.venv/Scripts/bh-daemon"
if [[ -f "${BH_DAEMON_POSIX}" ]]; then
    BH_DAEMON_BIN="${BH_DAEMON_POSIX}"
elif [[ -f "${BH_DAEMON_WIN}" ]]; then
    BH_DAEMON_BIN="${BH_DAEMON_WIN}"
else
    echo "baton-harness: error: bh-daemon not found under ${HARNESS_DIR}/.venv" >&2
    echo "  Expected one of:" >&2
    echo "    ${BH_DAEMON_POSIX}" >&2
    echo "    ${BH_DAEMON_WIN}" >&2
    echo "  Run bin/setup-env.sh first, or pass --harness-dir to point at the" >&2
    echo "  correct harness checkout." >&2
    exit 1
fi

WORKFLOW_FILE="${HARNESS_DIR}/config/WORKFLOW.md"
if [[ ! -f "${WORKFLOW_FILE}" ]]; then
    echo "baton-harness: error: workflow config not found: ${WORKFLOW_FILE}" >&2
    echo "  Pass --harness-dir to point at a harness checkout with config/WORKFLOW.md." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Auto-detect: RUN_USER
# ---------------------------------------------------------------------------

if [[ -n "${USER_OVERRIDE}" ]]; then
    RUN_USER="${USER_OVERRIDE}"
else
    RUN_USER="${SUDO_USER:-$(whoami)}"
fi

# ---------------------------------------------------------------------------
# Auto-detect: bws install dir for RUN_USER (#213)
#
# bin/setup-env.sh installs bws to "${HOME}/.local/bin" (no XDG override for
# that path). Under systemd the unit's User= process gets a minimal PATH that
# does not include it, so the daemon dies with "No such file or directory:
# 'bws'". Resolve RUN_USER's home via getent (works regardless of who invokes
# this installer, e.g. under sudo) and mirror setup-env.sh's ".local/bin"
# suffix; fall back to /home/${RUN_USER} if getent is unavailable/unknown.
# ---------------------------------------------------------------------------

RUN_USER_HOME="$(getent passwd "${RUN_USER}" 2>/dev/null | cut -d: -f6)" || RUN_USER_HOME=""
if [[ -z "${RUN_USER_HOME}" ]]; then
    RUN_USER_HOME="/home/${RUN_USER}"
fi
BWS_BIN_DIR="${RUN_USER_HOME}/.local/bin"

# ---------------------------------------------------------------------------
# Auto-detect: BH_PROJECT_ROOT (via bin/lib/load-config.sh — host.env, then
# .bh/config.env). An operator-set env var already wins inside that loader;
# setting it here before sourcing has the same effect as an override.
# ---------------------------------------------------------------------------

if [[ -n "${PROJECT_ROOT_OVERRIDE}" ]]; then
    export BH_PROJECT_ROOT="${PROJECT_ROOT_OVERRIDE}"
fi

_BH_LOAD_CONFIG="$(dirname "${BASH_SOURCE[0]}")/lib/load-config.sh"
if [[ -f "${_BH_LOAD_CONFIG}" ]]; then
    # shellcheck disable=SC1091
    source "${_BH_LOAD_CONFIG}"
fi
unset _BH_LOAD_CONFIG

if [[ -z "${BH_PROJECT_ROOT:-}" ]]; then
    if _bh_interactive; then
        read -r -p "baton-harness: BH_PROJECT_ROOT (absolute path to local sandbox clone): " BH_PROJECT_ROOT
        if [[ -z "${BH_PROJECT_ROOT}" ]]; then
            echo "baton-harness: error: BH_PROJECT_ROOT is required and was not provided." >&2
            exit 1
        fi
    else
        echo "baton-harness: error: BH_PROJECT_ROOT could not be resolved (no host.env," >&2
        echo "  no --project-root, non-interactive session)." >&2
        echo "  Set it via bin/setup-env.sh, --project-root PATH, or export BH_PROJECT_ROOT." >&2
        exit 1
    fi
fi

# .bh/config.env is read by bh-daemon itself at startup — warn (not fail) if
# absent, since this installer does not require it to write the unit file.
if [[ ! -f "${BH_PROJECT_ROOT}/.bh/config.env" ]]; then
    echo "baton-harness: warning: ${BH_PROJECT_ROOT}/.bh/config.env not found." >&2
    echo "  bh-daemon reads this at startup (BH_REPO_OWNER, BH_REPO_NAME, etc)." >&2
    echo "  Run bin/init-sandbox.sh, or write it by hand, before the service starts." >&2
fi

# ---------------------------------------------------------------------------
# Validate: systemd unit values must not contain whitespace. A space in any
# of these breaks the unquoted interpolation in _render_unit() below. Fail
# closed rather than adding fragile systemd-quoting rules (simplicity-first;
# CodeRabbit finding on #209) — reject the input and tell the operator to
# use a path without spaces.
# ---------------------------------------------------------------------------

_bh_reject_if_whitespace() {
    local name="$1" value="$2"
    if [[ "${value}" =~ [[:space:]] ]]; then
        echo "baton-harness: error: ${name} contains whitespace: '${value}'" >&2
        echo "  systemd unit values (User=, Environment=, ExecStart=) are" >&2
        echo "  interpolated unquoted, so paths with spaces produce a malformed" >&2
        echo "  unit file. Re-run with a ${name} that has no spaces (e.g. move" >&2
        echo "  the checkout, or pass an override without whitespace)." >&2
        exit 1
    fi
}

_bh_reject_if_whitespace "HARNESS_DIR" "${HARNESS_DIR}"
_bh_reject_if_whitespace "BH_DAEMON_BIN" "${BH_DAEMON_BIN}"
_bh_reject_if_whitespace "WORKFLOW_FILE" "${WORKFLOW_FILE}"
_bh_reject_if_whitespace "RUN_USER" "${RUN_USER}"
_bh_reject_if_whitespace "BH_PROJECT_ROOT" "${BH_PROJECT_ROOT}"
_bh_reject_if_whitespace "BWS_BIN_DIR" "${BWS_BIN_DIR}"

# ---------------------------------------------------------------------------
# Resolve BWS_ACCESS_TOKEN (the single bootstrap secret written to
# /etc/bh-daemon/secrets.env). Never echoed, logged, or exposed via set -x.
# ---------------------------------------------------------------------------

if [[ -n "${BWS_ACCESS_TOKEN:-}" ]]; then
    _bh_token="${BWS_ACCESS_TOKEN}"
elif _bh_interactive; then
    read -r -s -p "baton-harness: BWS_ACCESS_TOKEN (Bitwarden Secrets CLI machine-account token): " _bh_token
    echo ""
    if [[ -z "${_bh_token}" ]]; then
        echo "baton-harness: error: BWS_ACCESS_TOKEN is required and was not provided." >&2
        exit 1
    fi
else
    echo "baton-harness: error: BWS_ACCESS_TOKEN not set and session is non-interactive." >&2
    echo "  Export BWS_ACCESS_TOKEN before running this installer, or run it" >&2
    echo "  interactively without BH_SETUP_NO_PROMPT=1." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Render unit + secrets content
# ---------------------------------------------------------------------------

_render_secrets_preview() {
    # Redacted — used ONLY by the --print-unit dry-run path below. The real
    # token is rendered directly inside write_secrets_file(), never through
    # a shared helper, so this function must never see or print ${_bh_token}.
    printf 'BWS_ACCESS_TOKEN=%s\n' "<redacted>"
}

_render_unit() {
    cat <<EOF
[Unit]
Description=baton-harness daemon
After=network.target

[Service]
Type=simple
User=${RUN_USER}
Environment=BH_PROJECT_ROOT=${BH_PROJECT_ROOT}
Environment=PATH=${BWS_BIN_DIR}:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=/etc/bh-daemon/secrets.env
ExecStart=${BH_DAEMON_BIN} --workflow ${WORKFLOW_FILE}
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo "baton-harness: install-daemon-service — resolved configuration:"
echo "  HARNESS_DIR        = ${HARNESS_DIR}"
echo "  bh-daemon binary   = ${BH_DAEMON_BIN}"
echo "  WORKFLOW_FILE      = ${WORKFLOW_FILE}"
echo "  RUN_USER           = ${RUN_USER}"
echo "  BWS_BIN_DIR        = ${BWS_BIN_DIR} (unit PATH= prefix, so bws is found)"
echo "  BH_PROJECT_ROOT    = ${BH_PROJECT_ROOT}"
echo "  BWS_ACCESS_TOKEN   = <resolved, not shown>"
echo "  --no-start         = ${NO_START}"
echo ""
echo "  Will write:"
echo "    /etc/bh-daemon/secrets.env"
echo "    /etc/systemd/system/bh-daemon.service"
echo ""

# ---------------------------------------------------------------------------
# --print-unit: dry-run — render to stdout, no privileged writes, no systemctl
# ---------------------------------------------------------------------------

if [[ "${PRINT_UNIT}" == 1 ]]; then
    echo "baton-harness: --print-unit given, rendering to stdout (no writes performed)."
    echo ""
    echo "--- /etc/bh-daemon/secrets.env ---"
    _render_secrets_preview
    echo "--- /etc/systemd/system/bh-daemon.service ---"
    _render_unit
    exit 0
fi

# ---------------------------------------------------------------------------
# Single confirm (skipped in non-interactive mode — already past the two
# fail-closed gates above, so BH_PROJECT_ROOT and the token are resolved)
# ---------------------------------------------------------------------------

if _bh_interactive; then
    read -r -p "baton-harness: proceed with install? [y/N] " _bh_confirm || _bh_confirm="n"
    case "${_bh_confirm}" in
        [Yy]|[Yy][Ee][Ss]) ;;
        *)
            echo "baton-harness: install cancelled."
            exit 0
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# Privileged writes (backup-before-overwrite for idempotency)
# ---------------------------------------------------------------------------

_backup_if_exists() {
    local target="$1"
    if sudo test -f "${target}"; then
        local ts
        ts="$(date +%Y%m%d%H%M%S)"
        sudo cp "${target}" "${target}.bak.${ts}"
        echo "baton-harness:   backed up existing ${target} -> ${target}.bak.${ts}"
    fi
}

write_secrets_file() {
    sudo mkdir -p /etc/bh-daemon
    _backup_if_exists /etc/bh-daemon/secrets.env

    # Render the real token into a local 0600 tmp file, then install(1) it
    # into place at 0600 in one syscall — never a plain "tee then chmod",
    # which leaves the token world-readable at default umask between the
    # write and the chmod. The tmp file is created 600 (umask 077 in the
    # subshell, belt-and-suspenders chmod) and always removed (EXIT trap
    # covers early failure; explicit rm covers the success path).
    local _bh_secrets_tmp
    _bh_secrets_tmp="$(mktemp)"
    trap 'rm -f "${_bh_secrets_tmp}"' EXIT
    chmod 600 "${_bh_secrets_tmp}"
    (umask 077; printf 'BWS_ACCESS_TOKEN=%s\n' "${_bh_token}" > "${_bh_secrets_tmp}")
    sudo install -m 600 "${_bh_secrets_tmp}" /etc/bh-daemon/secrets.env
    rm -f "${_bh_secrets_tmp}"
    trap - EXIT

    echo "baton-harness:   wrote /etc/bh-daemon/secrets.env (mode 600)"
}

write_unit_file() {
    _backup_if_exists /etc/systemd/system/bh-daemon.service
    _render_unit | sudo tee /etc/systemd/system/bh-daemon.service >/dev/null
    echo "baton-harness:   wrote /etc/systemd/system/bh-daemon.service"
}

activate_service() {
    sudo systemctl daemon-reload
    echo "baton-harness:   systemctl daemon-reload done"
    if [[ "${NO_START}" == 1 ]]; then
        echo "baton-harness:   --no-start given, skipping enable/start"
        return 0
    fi
    sudo systemctl enable --now bh-daemon
    echo "baton-harness:   bh-daemon enabled and started"
    echo ""
    sudo systemctl status bh-daemon --no-pager || true
    echo ""
    sudo journalctl -u bh-daemon -n 20 --no-pager || true
}

echo "baton-harness: writing files ..."
write_secrets_file
write_unit_file
activate_service

# ---------------------------------------------------------------------------
# Baseline reminder (remind-only — does NOT run provisioning)
# ---------------------------------------------------------------------------

echo ""
echo "baton-harness: REMINDER — run bin/provision-ruleset.sh once against this"
echo "  target repo to capture .bh/ruleset-baseline.json. Without it, the #206"
echo "  fail-closed preflight gate parks every issue as NOT_PROVISIONED."
echo ""
echo "baton-harness: install complete."
