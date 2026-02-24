# Conch: LLM-assisted shell for bash
# Source this file from .bashrc, then use conch-setup to bind the trigger key.

: "${CONCH_ASK_CMD:=conch-ask}"

_conch_trigger_bash() {
  local query cmd
  # Move to new line and prompt for natural-language request
  echo ""
  read -r -e -p "ask: " query || return
  [[ -z "$query" ]] && return
  export PWD="$PWD"
  export CONCH_OS_SHELL="$(uname -s) / $SHELL"
  if (( CONCH_HISTORY_COUNT > 0 )) 2>/dev/null; then
    export CONCH_HISTORY="$(history 2>/dev/null | tail -n ${CONCH_HISTORY_COUNT:-5})"
  fi
  cmd="$("$CONCH_ASK_CMD" "$query" 2>/dev/null)"
  if [[ -n "$cmd" ]]; then
    READLINE_LINE="$cmd"
    READLINE_POINT=${#cmd}
  else
    echo "conch: no command returned (check CONCH_ASK_CMD and API config)" >&2
  fi
}

# Bind Ctrl+Space to run _conch_trigger_bash (which can set READLINE_LINE)
conch-setup() {
  bind -x '"^ ": _conch_trigger_bash'
}
