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

# ── Make scripts executable ──────────────────────────────────────────────────

chmod +x "$CONCH_DIR/bin/conch-ask" "$CONCH_DIR/bin/conch-chat" "$CONCH_DIR/bin/conch-run-with-timeout" 2>/dev/null
ok "Scripts are executable"

# ── API key ──────────────────────────────────────────────────────────────────

ENV_FILE="$CONCH_DIR/.env"
EXISTING_KEY=""
if [[ -f "$ENV_FILE" ]]; then
    EXISTING_KEY="$(grep 'OPENAI_API_KEY=' "$ENV_FILE" 2>/dev/null | sed 's/.*OPENAI_API_KEY="\{0,1\}\([^"]*\)"\{0,1\}/\1/' | head -1 || true)"
fi
if [[ -z "$EXISTING_KEY" ]]; then
    EXISTING_KEY="${OPENAI_API_KEY:-}"
fi

if [[ -n "$EXISTING_KEY" ]]; then
    MASKED="${EXISTING_KEY:0:8}...${EXISTING_KEY: -4}"
    ok "OpenAI API key found: ${MASKED}"
    API_KEY="$EXISTING_KEY"
else
    printf "\n"
    info "Enter your OpenAI API key (or press Enter to skip):"
    printf "  ${DIM}Get one at https://platform.openai.com/api-keys${RST}\n"
    printf "  Key: "
    read -r API_KEY
    if [[ -z "$API_KEY" ]]; then
        warn "No API key set. Set OPENAI_API_KEY later or edit ${ENV_FILE}"
    fi
fi

if [[ -n "$API_KEY" ]]; then
    cat > "$ENV_FILE" <<ENVEOF
# Conch API key (do not commit this file)
export OPENAI_API_KEY="${API_KEY}"
ENVEOF
    chmod 600 "$ENV_FILE"
    ok "API key saved to ${ENV_FILE} (chmod 600)"
fi

# ── Config ───────────────────────────────────────────────────────────────────

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/conch"
CONFIG_FILE="$CONFIG_DIR/config"
if [[ ! -f "$CONFIG_FILE" ]]; then
    mkdir -p "$CONFIG_DIR"
    cp "$CONCH_DIR/config.example" "$CONFIG_FILE" 2>/dev/null || true
    ok "Config created: ${CONFIG_FILE}"
else
    ok "Config exists: ${CONFIG_FILE}"
fi

# ── Composio MCP tools (optional) ────────────────────────────────────────

MCP_FILE="$CONFIG_DIR/mcp.json"
if [[ -f "$MCP_FILE" ]] && grep -q 'composio' "$MCP_FILE" 2>/dev/null; then
    ok "Composio already configured in ${MCP_FILE}"
else
    printf "\n"
    info "Composio adds 100+ tools to chat: web search, news, code execution, and more."
    info "Enter your Composio API key (or press Enter to skip):"
    printf "  ${DIM}Get one at https://composio.dev${RST}\n"
    printf "  Key: "
    read -r COMPOSIO_KEY
    if [[ -n "$COMPOSIO_KEY" ]]; then
        info "Creating Composio MCP server..."
        COMPOSIO_RESP="$(curl -s -X POST "https://backend.composio.dev/api/v3/mcp/servers" \
            -H "x-api-key: ${COMPOSIO_KEY}" \
            -H "Content-Type: application/json" \
            -d '{"name": "conch-tools", "auth_config_ids": [], "no_auth_apps": ["serpapi", "composio_search", "codeinterpreter", "firecrawl", "tavily"], "managed_auth_via_composio": true}' 2>/dev/null || true)"

        COMPOSIO_ID="$(echo "$COMPOSIO_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
        if [[ -n "$COMPOSIO_ID" ]]; then
            COMPOSIO_URL="https://backend.composio.dev/v3/mcp/${COMPOSIO_ID}/mcp?user_id=conch"
            mkdir -p "$CONFIG_DIR"
            if [[ -f "$MCP_FILE" ]]; then
                python3 -c "
import json
with open('$MCP_FILE') as f:
    cfg = json.load(f)
cfg.setdefault('mcpServers', {})['composio'] = {'type': 'http', 'url': '$COMPOSIO_URL'}
with open('$MCP_FILE', 'w') as f:
    json.dump(cfg, f, indent=2)
"
            else
                cat > "$MCP_FILE" <<MCPEOF
{
  "mcpServers": {
    "composio": {
      "type": "http",
      "url": "${COMPOSIO_URL}"
    }
  }
}
MCPEOF
            fi
            chmod 600 "$MCP_FILE"
            TOOL_COUNT="$(echo "$COMPOSIO_RESP" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("allowed_tools",[])))' 2>/dev/null || echo "100+")"
            ok "Composio configured: ${TOOL_COUNT} tools available"
        else
            COMPOSIO_ERR="$(echo "$COMPOSIO_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error","unknown error"))' 2>/dev/null || echo "could not create server")"
            warn "Composio setup failed: ${COMPOSIO_ERR}"
            echo "  You can configure it manually later. See README."
        fi
    else
        warn "Skipped Composio. You can add it later — see README."
    fi
fi

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
    add_to_rc "$HOME/.zshrc" "$CONCH_BLOCK"
elif [[ "$USER_SHELL" == "bash" ]]; then
    add_to_rc "$HOME/.bashrc" "$BASH_BLOCK"
else
    add_to_rc "$HOME/.zshrc" "$CONCH_BLOCK"
    warn "Added to .zshrc (your shell is $USER_SHELL; adjust if needed)"
fi

# ── Test ─────────────────────────────────────────────────────────────────────

printf "\n"
info "Testing conch-ask..."
export PATH="${CONCH_DIR}/bin:$PATH"
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"

if TEST_CMD="$(python3 "$CONCH_DIR/bin/conch-ask" "list files" 2>&1)"; then
    ok "conch-ask works: ${GREEN}${TEST_CMD}${RST}"
else
    warn "conch-ask returned an error: ${TEST_CMD}"
    echo "  This might be an API key issue. You can fix it later."
fi

# ── Done ─────────────────────────────────────────────────────────────────────

printf "\n${BOLD}${GREEN}  ✓ Conch installed!${RST}\n\n"
printf "  ${BOLD}Open a new terminal${RST}, then:\n\n"
printf "    ${CYAN}Ctrl+G${RST}           → ask for a shell command\n"
printf "    ${CYAN}ask${RST} list files    → inline request\n"
printf "    ${CYAN}chat${RST}             → multi-turn conversation\n"
printf "    ${CYAN}Ctrl+X Ctrl+G${RST}    → start chat via shortcut\n\n"
printf "  ${DIM}Config:  ${CONFIG_FILE}${RST}\n"
printf "  ${DIM}API key: ${ENV_FILE}${RST}\n"
printf "  ${DIM}MCP:     ${MCP_FILE}${RST}\n\n"
