#!/usr/bin/env bash
# bin/setup-env.sh — Harness-side environment setup
#
# Creates the Python venv and installs the baton-harness package so that
# bh-daemon is available.  Safe to re-run (idempotent).
#
# Usage:
#   bin/setup-env.sh [--help|-h]
#
# Prerequisites:
#   uv on PATH  (https://astral.sh/uv)

set -euo pipefail

# Pinned Bitwarden Secrets CLI version — bump here to upgrade.
BWS_INSTALL_VERSION="2.1.0"

# Pinned GitHub CLI version — bump here to upgrade.
GH_INSTALL_VERSION="2.62.0"

# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: bin/setup-env.sh [--help|-h]

Sets up the Python virtual environment and installs the baton-harness
package (including bh-daemon entry point) using uv.

Steps performed:
  1. Checks that uv is on PATH
  2. Checks that bws (Bitwarden Secrets CLI) is on PATH; offers to install
     v2.1.0 to ~/.local/bin when running in an interactive terminal
  3. Checks that gh (GitHub CLI) is on PATH; offers to install v2.62.0
     to ~/.local/bin when running in an interactive terminal
  4. Checks that claude (Claude Code CLI) is on PATH; offers to install
     via the official native installer when running in an interactive terminal
  5. Creates .venv (skipped if already present — idempotent)
  6. Installs the package with dev extras: uv pip install -e ".[dev]"
  7. Verifies bh-daemon is accessible inside the venv
  8. Prints the activation hint
  9. Checks whether BWS_ACCESS_TOKEN is present for bh-daemon runtime and
     prints a non-fatal notice if it is missing

Safe to re-run: venv creation is skipped when .venv already exists.
No environment variables are required for this dev-setup script to run.
BWS_ACCESS_TOKEN is required later by bh-daemon at runtime, but not by this
script; setup-env.sh only checks whether it is present and never persists it.

bws/gh/claude auto-install behaviour (all three follow the same rules):
  - Interactive terminal (default): prompts before downloading.
  - BH_SETUP_NO_PROMPT=1 or non-TTY (e.g. CI): skips prompt and exits 1
    with a link to the manual install page.  No silent network calls.
  - Manual install pages:
      bws:    https://bitwarden.com/help/secrets-manager-cli/
      gh:     https://github.com/cli/cli#installation
      claude: https://docs.claude.com/en/docs/claude-code/setup

Note: after installing gh or claude, you must authenticate them separately:
  - gh:     gh auth login
  - claude: run claude once interactively
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
    exit 0
fi

# ---------------------------------------------------------------------------
# Resolve the harness repo root from the script's own location
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BATON_HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Preflight: uv must be on PATH
# ---------------------------------------------------------------------------

if ! command -v uv &>/dev/null; then
    echo "baton-harness: error: uv not found on PATH" >&2
    echo "  Install uv with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "  Then restart your shell and re-run this script." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Preflight: bws (Bitwarden Secrets CLI) must be on PATH
# ---------------------------------------------------------------------------

_bws_manual_url="https://bitwarden.com/help/secrets-manager-cli/"

_install_bws() {
    # Preflight: required tools
    local _missing_tools=()
    command -v curl   &>/dev/null || _missing_tools+=("curl")
    command -v unzip  &>/dev/null || _missing_tools+=("unzip")
    local _os
    _os="$(uname -s)"
    if [[ "${_os}" == "Linux" ]]; then
        command -v sha256sum &>/dev/null || _missing_tools+=("sha256sum")
    else
        command -v shasum &>/dev/null || _missing_tools+=("shasum")
    fi
    if [[ "${#_missing_tools[@]}" -gt 0 ]]; then
        echo "baton-harness: error: bws install requires: ${_missing_tools[*]}" >&2
        echo "  Install the missing tool(s) and re-run this script." >&2
        exit 1
    fi

    # OS detection
    local _arch
    _arch="$(uname -m)"
    local _asset_stem
    case "${_os}" in
        Linux)
            case "${_arch}" in
                x86_64)
                    _asset_stem="bws-x86_64-unknown-linux-gnu" ;;
                aarch64|arm64)
                    _asset_stem="bws-aarch64-unknown-linux-gnu" ;;
                *)
                    echo "baton-harness: error: unsupported Linux architecture: ${_arch}" >&2
                    echo "  Install bws manually: ${_bws_manual_url}" >&2
                    exit 1 ;;
            esac
            ;;
        Darwin)
            _asset_stem="bws-macos-universal" ;;
        *)
            echo "baton-harness: error: auto-install supported only on Linux/macOS" >&2
            echo "  Install bws manually: ${_bws_manual_url}" >&2
            exit 1 ;;
    esac

    local _ver="${BWS_INSTALL_VERSION}"
    local _asset_name="${_asset_stem}-${_ver}.zip"
    local _base_url="https://github.com/bitwarden/sdk-sm/releases/download/bws-v${_ver}"
    local _zip_url="${_base_url}/${_asset_name}"
    local _checksum_url="${_base_url}/bws-sha256-checksums-${_ver}.txt"

    # Create tempdir; trap ensures cleanup on success and failure.
    local _tmpdir
    _tmpdir="$(mktemp -d)"
    BWS_TMPDIR="${_tmpdir}"
    trap 'rm -rf "${BWS_TMPDIR:-/dev/null}"' EXIT

    local _zip_path="${_tmpdir}/${_asset_name}"
    local _checksum_path="${_tmpdir}/bws-sha256-checksums-${_ver}.txt"

    echo "baton-harness: downloading bws v${_ver} ..."
    curl -fSL --proto '=https' --tlsv1.2 -o "${_zip_path}" "${_zip_url}"
    curl -fSL --proto '=https' --tlsv1.2 -o "${_checksum_path}" "${_checksum_url}"

    echo "baton-harness: verifying checksum ..."
    # Extract the matching line; guard grep exit so we can give a clear error.
    local _checksum_line
    _checksum_line="$(grep -F "${_asset_name}" "${_checksum_path}" || true)"
    if [[ -z "${_checksum_line}" ]]; then
        echo "baton-harness: error: checksum entry for ${_asset_name} not found in checksums file" >&2
        exit 1
    fi
    local _checksum_verify_line="${_checksum_line%"${_asset_name}"}${_zip_path}"
    if [[ "${_os}" == "Linux" ]]; then
        printf '%s' "${_checksum_verify_line}" | sha256sum -c - >/dev/null
    else
        printf '%s' "${_checksum_verify_line}" | shasum -a 256 -c - >/dev/null
    fi

    echo "baton-harness: installing bws to ~/.local/bin ..."
    unzip -q "${_zip_path}" -d "${_tmpdir}"
    mkdir -p "${HOME}/.local/bin"
    mv "${_tmpdir}/bws" "${HOME}/.local/bin/bws"
    chmod +x "${HOME}/.local/bin/bws"

    # Clear bash's command-name cache before PATH check.
    hash -r 2>/dev/null || true
    if ! command -v bws &>/dev/null; then
        echo "baton-harness: ~/.local/bin/bws installed, but ~/.local/bin is not on your PATH." >&2
        echo "  Add to your shell rc:" >&2
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
        echo "  Then re-run bin/setup-env.sh." >&2
        exit 1
    fi

    # Final sanity check.
    local _bws_ver
    _bws_ver="$(bws --version 2>&1)" || {
        echo "baton-harness: error: bws installed but 'bws --version' failed" >&2
        exit 1
    }
    echo "baton-harness: bws installed successfully (${_bws_ver})"
}

if command -v bws &>/dev/null; then
    echo "baton-harness: bws already on PATH ($(bws --version 2>&1))"
elif [[ -t 0 && -t 1 && "${BH_SETUP_NO_PROMPT:-0}" != "1" ]]; then
    echo ""
    read -r -p "baton-harness: bws not found. Install Bitwarden Secrets CLI v${BWS_INSTALL_VERSION} to ~/.local/bin? [Y/n] " _bws_reply || _bws_reply="n"
    case "${_bws_reply}" in
        [Yy]|"")
            _install_bws ;;
        *)
            echo "baton-harness: bws install declined." >&2
            echo "  Install bws manually: ${_bws_manual_url}" >&2
            exit 1 ;;
    esac
else
    echo "baton-harness: error: bws not found on PATH" >&2
    echo "  Install bws manually: ${_bws_manual_url}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Preflight: gh (GitHub CLI) must be on PATH
# ---------------------------------------------------------------------------

_gh_manual_url="https://github.com/cli/cli#installation"

_install_gh() {
    # Preflight: required tools
    local _missing_tools=()
    command -v curl      &>/dev/null || _missing_tools+=("curl")
    command -v tar       &>/dev/null || _missing_tools+=("tar")
    local _os
    _os="$(uname -s)"
    if [[ "${_os}" == "Linux" ]]; then
        command -v sha256sum &>/dev/null || _missing_tools+=("sha256sum")
    else
        command -v shasum &>/dev/null || _missing_tools+=("shasum")
    fi
    if [[ "${#_missing_tools[@]}" -gt 0 ]]; then
        echo "baton-harness: error: gh install requires: ${_missing_tools[*]}" >&2
        echo "  Install the missing tool(s) and re-run this script." >&2
        exit 1
    fi

    # OS + arch detection
    local _arch
    _arch="$(uname -m)"
    local _asset_stem
    case "${_os}" in
        Linux)
            case "${_arch}" in
                x86_64)
                    _asset_stem="linux_amd64" ;;
                aarch64|arm64)
                    _asset_stem="linux_arm64" ;;
                *)
                    echo "baton-harness: error: unsupported Linux architecture: ${_arch}" >&2
                    echo "  Install gh manually: ${_gh_manual_url}" >&2
                    exit 1 ;;
            esac
            ;;
        Darwin)
            case "${_arch}" in
                arm64)
                    _asset_stem="macOS_arm64" ;;
                *)
                    _asset_stem="macOS_amd64" ;;
            esac
            ;;
        *)
            echo "baton-harness: error: gh auto-install supported only on Linux/macOS" >&2
            echo "  Install gh manually: ${_gh_manual_url}" >&2
            exit 1 ;;
    esac

    local _ver="${GH_INSTALL_VERSION}"
    local _asset_name="gh_${_ver}_${_asset_stem}.tar.gz"
    local _base_url="https://github.com/cli/cli/releases/download/v${_ver}"
    local _tar_url="${_base_url}/${_asset_name}"
    local _checksum_url="${_base_url}/gh_${_ver}_checksums.txt"

    # Create tempdir; trap ensures cleanup on success and failure.
    local _tmpdir
    _tmpdir="$(mktemp -d)"
    GH_TMPDIR="${_tmpdir}"
    trap 'rm -rf "${GH_TMPDIR:-/dev/null}"' EXIT

    local _tar_path="${_tmpdir}/${_asset_name}"
    local _checksum_path="${_tmpdir}/gh_${_ver}_checksums.txt"

    echo "baton-harness: downloading gh v${_ver} ..."
    curl -fSL --proto '=https' --tlsv1.2 -o "${_tar_path}" "${_tar_url}"
    curl -fSL --proto '=https' --tlsv1.2 -o "${_checksum_path}" "${_checksum_url}"

    echo "baton-harness: verifying checksum ..."
    local _checksum_line
    _checksum_line="$(grep -F "${_asset_name}" "${_checksum_path}" || true)"
    if [[ -z "${_checksum_line}" ]]; then
        echo "baton-harness: error: checksum entry for ${_asset_name} not found in checksums file" >&2
        exit 1
    fi
    local _checksum_verify_line="${_checksum_line%"${_asset_name}"}${_tar_path}"
    if [[ "${_os}" == "Linux" ]]; then
        printf '%s' "${_checksum_verify_line}" | sha256sum -c - >/dev/null
    else
        printf '%s' "${_checksum_verify_line}" | shasum -a 256 -c - >/dev/null
    fi

    echo "baton-harness: installing gh to ~/.local/bin ..."
    tar -xzf "${_tar_path}" -C "${_tmpdir}"
    # The tarball extracts to gh_<ver>_<plat>/bin/gh — locate the binary.
    local _gh_binary
    _gh_binary="${_tmpdir}/gh_${_ver}_${_asset_stem}/bin/gh"
    if [[ ! -f "${_gh_binary}" ]]; then
        echo "baton-harness: error: expected binary not found at ${_gh_binary}" >&2
        exit 1
    fi
    mkdir -p "${HOME}/.local/bin"
    mv "${_gh_binary}" "${HOME}/.local/bin/gh"
    chmod +x "${HOME}/.local/bin/gh"

    # Clear bash's command-name cache before PATH check.
    hash -r 2>/dev/null || true
    if ! command -v gh &>/dev/null; then
        echo "baton-harness: ~/.local/bin/gh installed, but ~/.local/bin is not on your PATH." >&2
        echo "  Add to your shell rc:" >&2
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
        echo "  Then re-run bin/setup-env.sh." >&2
        exit 1
    fi

    # Final sanity check.
    local _gh_ver
    _gh_ver="$(gh --version 2>&1 | head -1)" || {
        echo "baton-harness: error: gh installed but 'gh --version' failed" >&2
        exit 1
    }
    echo "baton-harness: gh installed successfully (${_gh_ver})"
}

if command -v gh &>/dev/null; then
    echo "baton-harness: gh already on PATH ($(gh --version 2>&1 | head -1))"
elif [[ -t 0 && -t 1 && "${BH_SETUP_NO_PROMPT:-0}" != "1" ]]; then
    echo ""
    read -r -p "baton-harness: gh not found. Install GitHub CLI v${GH_INSTALL_VERSION} to ~/.local/bin? [Y/n] " _gh_reply || _gh_reply="n"
    case "${_gh_reply}" in
        [Yy]|"")
            _install_gh ;;
        *)
            echo "baton-harness: gh install declined." >&2
            echo "  Install gh manually: ${_gh_manual_url}" >&2
            exit 1 ;;
    esac
else
    echo "baton-harness: error: gh not found on PATH" >&2
    echo "  Install gh manually: ${_gh_manual_url}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Preflight: claude (Claude Code CLI) must be on PATH
# ---------------------------------------------------------------------------

_claude_manual_url="https://docs.claude.com/en/docs/claude-code/setup"

_install_claude() {
    # Preflight: required tools
    if ! command -v curl &>/dev/null; then
        echo "baton-harness: error: claude install requires: curl" >&2
        echo "  Install curl and re-run this script." >&2
        exit 1
    fi

    echo "baton-harness: installing claude via official installer ..."
    curl -fsSL https://claude.ai/install.sh | bash

    # Clear bash's command-name cache before PATH check.
    hash -r 2>/dev/null || true
    if ! command -v claude &>/dev/null; then
        echo "baton-harness: claude installer ran, but claude is not on your PATH." >&2
        echo "  The installer typically places claude in ~/.local/bin or ~/.claude/local." >&2
        echo "  Add the appropriate directory to your PATH:" >&2
        echo "    export PATH=\"\$HOME/.local/bin:\$HOME/.claude/local:\$PATH\"" >&2
        echo "  Then re-run bin/setup-env.sh." >&2
        exit 1
    fi

    # Final sanity check.
    local _claude_ver
    _claude_ver="$(claude --version 2>&1)" || {
        echo "baton-harness: error: claude installed but 'claude --version' failed" >&2
        exit 1
    }
    echo "baton-harness: claude installed successfully (${_claude_ver})"
}

if command -v claude &>/dev/null; then
    echo "baton-harness: claude already on PATH ($(claude --version 2>&1))"
elif [[ -t 0 && -t 1 && "${BH_SETUP_NO_PROMPT:-0}" != "1" ]]; then
    echo ""
    read -r -p "baton-harness: claude not found. Install Claude Code CLI via official installer? [Y/n] " _claude_reply || _claude_reply="n"
    case "${_claude_reply}" in
        [Yy]|"")
            _install_claude ;;
        *)
            echo "baton-harness: claude install declined." >&2
            echo "  Install claude manually: ${_claude_manual_url}" >&2
            exit 1 ;;
    esac
else
    echo "baton-harness: error: claude not found on PATH" >&2
    echo "  Install claude manually: ${_claude_manual_url}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Create venv (idempotent)
# ---------------------------------------------------------------------------

VENV_DIR="${BATON_HARNESS_DIR}/.venv"

if [[ -d "${VENV_DIR}" ]]; then
    echo "baton-harness: venv already present, skipping creation"
else
    echo "baton-harness: creating venv at ${VENV_DIR} ..."
    uv venv "${VENV_DIR}"
fi

# ---------------------------------------------------------------------------
# Install the package (always run — uv is idempotent here)
# ---------------------------------------------------------------------------

echo "baton-harness: installing package (uv pip install -e \".[dev]\") ..."
uv pip install --python "${VENV_DIR}" -e "${BATON_HARNESS_DIR}[dev]"

# ---------------------------------------------------------------------------
# Verify bh-daemon is accessible inside the venv
# ---------------------------------------------------------------------------

# Probe both Windows (Scripts/) and POSIX (bin/) venv layouts.
BH_DAEMON_WIN="${VENV_DIR}/Scripts/bh-daemon"
BH_DAEMON_POSIX="${VENV_DIR}/bin/bh-daemon"

if [[ -f "${BH_DAEMON_WIN}" ]]; then
    BH_DAEMON_FOUND="${BH_DAEMON_WIN}"
elif [[ -f "${BH_DAEMON_POSIX}" ]]; then
    BH_DAEMON_FOUND="${BH_DAEMON_POSIX}"
else
    echo "baton-harness: error: bh-daemon not found after install" >&2
    echo "  Expected one of:" >&2
    echo "    ${BH_DAEMON_WIN}" >&2
    echo "    ${BH_DAEMON_POSIX}" >&2
    echo "  Check that pyproject.toml declares:" >&2
    echo "    [project.scripts]" >&2
    echo "    bh-daemon = \"baton_harness.chain.cli:main\"" >&2
    exit 1
fi

echo "baton-harness: bh-daemon found at ${BH_DAEMON_FOUND}"

# ---------------------------------------------------------------------------
# Print activation hint
# ---------------------------------------------------------------------------

echo ""
echo "baton-harness: setup complete."
echo ""
echo "  Activate the venv before running bh-daemon manually:"
echo ""
echo "    source .venv/bin/activate        # macOS / Linux"
echo "    source .venv/Scripts/activate    # Windows Git Bash"
printf '    .venv\\Scripts\\Activate.ps1      # Windows PowerShell\n'
printf '    .venv\\Scripts\\activate.bat      # Windows cmd\n'
echo ""
echo "  Or run bh-daemon directly via bin/run-daemon.sh (no activation needed)."
echo ""

# ---------------------------------------------------------------------------
# Prompt for per-host config and persist
# ---------------------------------------------------------------------------

HOST_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/baton-harness"
HOST_ENV="${HOST_CONFIG_DIR}/host.env"

# Skip prompting in non-interactive contexts (CI, --no-prompt).
if [[ -t 0 && -t 1 && "${BH_SETUP_NO_PROMPT:-0}" != "1" ]]; then
    echo ""
    if [[ -f "${HOST_ENV}" ]]; then
        echo "baton-harness: per-host config already present at ${HOST_ENV}"
        echo "  (delete it and re-run bin/setup-env.sh to reset)"
    else
        echo "baton-harness: setting up per-host config at ${HOST_ENV}"
        read -r -p "  BH_PROJECT_ROOT (absolute path to local sandbox clone): " _bh_project_root
        if [[ -z "${_bh_project_root}" ]]; then
            echo "  skipped — re-run bin/setup-env.sh to set it, or export BH_PROJECT_ROOT manually"
        else
            mkdir -p "${HOST_CONFIG_DIR}"
            chmod 700 "${HOST_CONFIG_DIR}"
            cat > "${HOST_ENV}" <<EOF
# baton-harness per-host config — written by bin/setup-env.sh
# Sourced automatically by bin/run-daemon.sh at startup.
export BH_PROJECT_ROOT="${_bh_project_root}"
EOF
            chmod 600 "${HOST_ENV}"
            echo "  wrote ${HOST_ENV} (mode 600)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Runtime preflight notice: BWS_ACCESS_TOKEN
# ---------------------------------------------------------------------------

if [[ -n "${BWS_ACCESS_TOKEN:-}" ]]; then
    echo "baton-harness: BWS_ACCESS_TOKEN already set in environment"
else
    echo "baton-harness: warning: BWS_ACCESS_TOKEN not set — required by bh-daemon at runtime to authenticate to bws. For manual bin/run-daemon.sh runs, export it in your shell; for the systemd service, drop it at /etc/bh-daemon/secrets.env (mode 600, via EnvironmentFile). Do not store it in ~/.config/baton-harness/host.env. See README Prerequisites (runtime). Not needed for this dev-setup step." >&2
fi
