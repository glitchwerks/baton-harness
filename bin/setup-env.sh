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
  2. Creates .venv (skipped if already present — idempotent)
  3. Installs the package with dev extras: uv pip install -e ".[dev]"
  4. Verifies bh-daemon is accessible inside the venv
  5. Prints the activation hint

Safe to re-run: venv creation is skipped when .venv already exists.
No environment variables are required.
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
echo "    source .venv/bin/activate      # Git Bash / macOS / Linux"
printf '    .venv\\Scripts\\activate         # Windows cmd / PowerShell\n'
echo ""
echo "  Or run bh-daemon directly via bin/run-daemon.sh (no activation needed)."
echo ""
