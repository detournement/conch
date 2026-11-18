#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Conch installer
# Adds LLM-assisted shell commands (ask, chat) to your terminal.
# ─────────────────────────────────────────────────────────────────────────────

CYAN='\033[1;36m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
DIM='\033[2m'
BOLD='\033[1m'
RST='\033[0m'

CONCH_DIR="$(cd "$(dirname "$0")" && pwd)"

info()  { printf "${CYAN}▸${RST} %s\n" "$*"; }
ok()    { printf "${GREEN}✓${RST} %s\n" "$*"; }
warn()  { printf "${YELLOW}!${RST} %s\n" "$*"; }
err()   { printf "${RED}✗${RST} %s\n" "$*"; }

# ── Checks ───────────────────────────────────────────────────────────────────

printf "\n${BOLD}${CYAN}  🐚 Conch installer${RST}\n"
printf "  ${DIM}LLM-assisted shell — ask, chat, get commands${RST}\n\n"

# Python 3
if ! command -v python3 &>/dev/null; then
    err "Python 3 is required but not found."
    echo "  Install with: brew install python3"
    exit 1
fi
PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ok "Python ${PYTHON_VERSION} found"

# Shell
USER_SHELL="$(basename "${SHELL:-/bin/zsh}")"
if [[ "$USER_SHELL" != "zsh" && "$USER_SHELL" != "bash" ]]; then
    warn "Shell is '$USER_SHELL' — Conch works best with zsh or bash."
fi
ok "Shell: ${USER_SHELL}"

# ── Install Python package ───────────────────────────────────────────────────

info "Installing conch in the checkout's virtual environment..."
VENV="$CONCH_DIR/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
fi
PYTHON="$VENV/bin/python"
if command -v uv &>/dev/null; then
    uv pip install --python "$PYTHON" -e "$CONCH_DIR"
else
    if ! "$PYTHON" -m pip --version &>/dev/null; then
        "$PYTHON" -m ensurepip --upgrade
    fi
    "$PYTHON" -m pip install --upgrade --no-input "pip>=21.3"
    "$PYTHON" -m pip install -e "$CONCH_DIR"
fi
ok "Installed conch and terminal dependencies in ${VENV}"

# ── Make scripts executable ──────────────────────────────────────────────────

chmod +x "$CONCH_DIR/bin/conch" "$CONCH_DIR/bin/conch-ask" "$CONCH_DIR/bin/conch-chat" "$CONCH_DIR/bin/conch-run-with-timeout" 2>/dev/null || true
ok "Scripts are executable"

# ── Provider setup ────────────────────────────────────────────────────────────

# Keep existing repository-local integration credentials untouched. Model
# sign-in and new API keys use Conch's shared first-run wizard/key manager.
ENV_FILE="$CONCH_DIR/.env"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/conch"
CONFIG_FILE="$CONFIG_DIR/config"
mkdir -p "$CONFIG_DIR"

printf "\n"
if [[ -f "$CONFIG_FILE" ]]; then
    ok "Existing provider configuration preserved: ${CONFIG_FILE}"
else
    info "Setup will offer Claude/ChatGPT account sign-in, local models, and optional API keys."
fi

# Optional integrations should not interrupt provider setup on first launch.
MCP_FILE="$CONFIG_DIR/mcp.json"
info "Optional MCP tools can be configured later in ${MCP_FILE}"

# ── .gitignore ───────────────────────────────────────────────────────────────

if [[ ! -f "$CONCH_DIR/.gitignore" ]] || ! grep -q '\.env' "$CONCH_DIR/.gitignore" 2>/dev/null; then
    echo ".env" >> "$CONCH_DIR/.gitignore"
fi

# ── Shell integration ────────────────────────────────────────────────────────

CONCH_BLOCK="
# Conch: LLM-assisted shell (Ctrl+G = ask, Ctrl+X Ctrl+G = chat)
export CONCH_DIR=\"${CONCH_DIR}\"
export PATH=\"\${CONCH_DIR}/bin:\$PATH\"
[[ -f \"\${CONCH_DIR}/.env\" ]] && source \"\${CONCH_DIR}/.env\"
source \"\${CONCH_DIR}/shell/conch.zsh\"
conch-setup"

BASH_BLOCK="
# Conch: LLM-assisted shell
export CONCH_DIR=\"${CONCH_DIR}\"
export PATH=\"\${CONCH_DIR}/bin:\$PATH\"
[[ -f \"\${CONCH_DIR}/.env\" ]] && source \"\${CONCH_DIR}/.env\"
source \"\${CONCH_DIR}/shell/conch.bash\"
conch-setup"

add_to_rc() {
    local rc_file="$1" block="$2"
    if [[ -f "$rc_file" ]] && grep -q 'conch-setup' "$rc_file" 2>/dev/null; then
        ok "Already in ${rc_file}"
        return
    fi
    printf "\n%s\n" "$block" >> "$rc_file"
    ok "Added to ${rc_file}"
}

printf "\n"
info "Adding Conch to shell config..."

if [[ "$USER_SHELL" == "zsh" ]]; then
    add_to_rc "${ZDOTDIR:-$HOME}/.zshrc" "$CONCH_BLOCK"
elif [[ "$USER_SHELL" == "bash" ]]; then
    add_to_rc "$HOME/.bashrc" "$BASH_BLOCK"
else
    add_to_rc "$HOME/.zshrc" "$CONCH_BLOCK"
    warn "Added to .zshrc (your shell is $USER_SHELL; adjust if needed)"
fi

# Reconcile old aliases, PATH entries and sourced integration files instead of
# assuming the first Conch block still selects this checkout.
"$PYTHON" -m conch.launchers --launcher "$CONCH_DIR/bin/conch"

# ── Test ─────────────────────────────────────────────────────────────────────

printf "\n"
info "Testing conch..."
export PATH="${CONCH_DIR}/bin:$PATH"
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"

if "$PYTHON" -c "import conch, prompt_toolkit; print(f'conch v{conch.__version__}')"; then
    ok "Package and terminal editor load correctly"
else
    err "Package import failed — installation is incomplete"
    exit 1
fi

info "Checking the configured model and account access..."
if ( : < /dev/tty ) 2>/dev/null; then
    if ! "$PYTHON" -m conch.onboarding < /dev/tty > /dev/tty 2>&1; then
        warn "Conch is installed, but setup is incomplete. Run conch setup in a terminal."
        exit 1
    fi
elif ! "$PYTHON" -m conch.onboarding --check; then
    warn "Conch is installed but unverified. Run conch setup in a terminal before chatting."
    exit 1
fi

# ── Done ─────────────────────────────────────────────────────────────────────

printf "\n${BOLD}${GREEN}  ✓ Conch installed!${RST}\n\n"
printf "  ${BOLD}Open a new terminal${RST}, then:\n\n"
printf "    ${CYAN}conch${RST}              → multi-turn chat with tools\n"
printf "    ${CYAN}ask${RST} list files     → inline command generation\n"
printf "    ${CYAN}Ctrl+G${RST}             → ask for a shell command\n"
printf "    ${CYAN}Ctrl+X Ctrl+G${RST}      → start chat via shortcut\n\n"
printf "  ${DIM}Config:  ${CONFIG_FILE}${RST}\n"
printf "  ${DIM}API/integration keys: ${CONFIG_DIR}/env (managed by conch keys)${RST}\n"
printf "  ${DIM}MCP:     ${MCP_FILE}${RST}\n"
printf "  ${DIM}Docs:    README.md → Install and first run; /help inside chat${RST}\n\n"
printf "  ${BOLD}Provider setup:${RST} conch setup verifies access and repairs sign-in.\n"
printf "  Seats use your own subscription through an official CLI (install it first):\n"
printf "    ${CYAN}conch auth login claude-code${RST}  → Claude subscription\n"
printf "    ${CYAN}conch auth login codex${RST}        → ChatGPT subscription\n"
printf "    ${CYAN}conch auth --help${RST}             → CLI installation and setup steps\n"
printf "  API keys are optional, separately billed alternatives; existing settings are kept.\n"
printf "  Local models and optional subagents: docs/LOCAL-SUBAGENTS.md\n\n"
