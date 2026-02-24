# Conch: LLM-assisted shell for zsh
# Source this file from .zshrc and call conch-setup.

: "${CONCH_DIR:=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)}"
: "${CONCH_ASK_CMD:=conch-ask}"

# Single widget: prompt for request, call LLM, put result in buffer.
# No keymap manipulation — Enter/Escape/Tab all work normally after.
conch-trigger() {
  local request cmd errfile errmsg runner _conch_bin

  # Tell zle we're going to use the terminal directly
  zle -I

  # Prompt for the request on a new line
  printf '\n'
  read -r "request?ask: " || { zle reset-prompt; return; }
  [[ -z "$request" ]] && { zle reset-prompt; return; }

  printf 'Thinking…\n'

  # Find the timeout runner (same dir as conch-ask)
  _conch_bin="$(dirname "$(command -v "$CONCH_ASK_CMD" 2>/dev/null)" 2>/dev/null)"
  runner="${_conch_bin}/conch-run-with-timeout"
  [[ -f "$runner" ]] || runner="$(command -v conch-run-with-timeout 2>/dev/null)"
  [[ -f "$runner" ]] || runner="${CONCH_DIR}/bin/conch-run-with-timeout"

  errfile="${TMPDIR:-/tmp}/conch-$$.err"
  export CONCH_OS_SHELL="$(uname -s) / $SHELL"

  cmd="$(python3 "$runner" 15 "$request" 2>"$errfile")"

  errmsg=""
  [[ -s "$errfile" ]] && errmsg="$(head -n 3 "$errfile")"
  rm -f "$errfile"

  if [[ -n "$cmd" ]]; then
    BUFFER="$cmd"
    CURSOR=${#BUFFER}
    printf 'Command ready — press Enter to run, or edit first.\n'
  else
    BUFFER=""
    [[ -n "$errmsg" ]] && printf 'conch: %s\n' "$errmsg" || printf 'No command returned.\n'
  fi
  zle reset-prompt
}

zle -N conch-trigger

# Animated spinner for the shell (runs in background, killed when done)
_conch_spinner() {
  local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
  local i=0
  while true; do
    printf '\r\033[36m%s\033[0m \033[2mThinking\033[0m  ' "${frames[$((i % ${#frames[@]} + 1))]}"
    i=$((i + 1))
    sleep 0.08
  done
}

ask() {
  local request="$*"
  if [[ -z "$request" ]]; then
    printf '\033[1;33mask:\033[0m '
    read -r request || return
  fi
  [[ -z "$request" ]] && return
  local _conch_bin runner errfile cmd errmsg spinner_pid
  _conch_bin="$(dirname "$(command -v "$CONCH_ASK_CMD" 2>/dev/null)" 2>/dev/null)"
  runner="${_conch_bin}/conch-run-with-timeout"
  [[ -f "$runner" ]] || runner="$(command -v conch-run-with-timeout 2>/dev/null)"
  [[ -f "$runner" ]] || runner="${CONCH_DIR}/bin/conch-run-with-timeout"
  errfile="${TMPDIR:-/tmp}/conch-$$.err"
  export CONCH_OS_SHELL="$(uname -s) / $SHELL"
  # Start animated spinner (suppress job control messages)
  {
    _conch_spinner &
    spinner_pid=$!
  } 2>/dev/null
  cmd="$(python3 "$runner" 15 "$request" 2>"$errfile")"
  { kill $spinner_pid; wait $spinner_pid; } 2>/dev/null
  printf '\r%40s\r' ''
  errmsg=""
  [[ -s "$errfile" ]] && errmsg="$(head -n 3 "$errfile")"
  rm -f "$errfile"
  if [[ -n "$cmd" ]]; then
    printf '\033[1;36m→\033[0m \033[1;32m%s\033[0m\n' "$cmd"
    print -z "$cmd"
  else
    [[ -n "$errmsg" ]] && printf '\033[31mconch: %s\033[0m\n' "$errmsg" || printf '\033[31mNo command returned.\033[0m\n'
  fi
}

# "chat" — multi-turn conversation with the LLM (not shell commands)
# Type "chat" to start, or "chat what is a docker volume?" for a one-shot answer.
chat() {
  local _conch_bin chat_cmd
  _conch_bin="$(dirname "$(command -v "$CONCH_ASK_CMD" 2>/dev/null)" 2>/dev/null)"
  chat_cmd="${_conch_bin}/conch-chat"
  [[ -x "$chat_cmd" ]] || chat_cmd="$(command -v conch-chat 2>/dev/null)"
  [[ -x "$chat_cmd" ]] || chat_cmd="${CONCH_DIR}/bin/conch-chat"
  if [[ $# -gt 0 ]]; then
    python3 "$chat_cmd" "$@"
  else
    python3 "$chat_cmd"
  fi
}

# --- Completions: zsh general, Docker, AWS CLI, git -------------------------
conch-completion-setup() {
  # Ensure Homebrew completions are in fpath before compinit
  local brew_prefix
  brew_prefix="$(brew --prefix 2>/dev/null)" || brew_prefix=""
  if [[ -n "$brew_prefix" && -d "$brew_prefix/share/zsh/site-functions" ]]; then
    fpath=("$brew_prefix/share/zsh/site-functions" $fpath)
  fi

  # Initialize zsh completion system (skip if already loaded)
  if (( ! ${+_comps} )); then
    autoload -Uz compinit
    compinit -u
  fi

  # Docker completion
  if (( ! ${+_comps[docker]} )); then
    if command -v docker &>/dev/null; then
      # Modern Docker CLI has built-in completion output
      if docker completion zsh &>/dev/null; then
        source <(docker completion zsh)
      else
        # Docker Desktop on macOS ships a completion file
        local docker_comp
        for docker_comp in \
          /Applications/Docker.app/Contents/Resources/etc/docker.zsh-completion \
          /usr/local/share/zsh/site-functions/_docker \
          "$brew_prefix/share/zsh/site-functions/_docker"; do
          if [[ -r "$docker_comp" ]]; then
            source "$docker_comp"
            break
          fi
        done
      fi
    fi
  fi

  # Docker Compose completion
  if (( ! ${+_comps[docker-compose]} )); then
    local dc_comp
    for dc_comp in \
      /Applications/Docker.app/Contents/Resources/etc/docker-compose.zsh-completion \
      "$brew_prefix/share/zsh/site-functions/_docker-compose"; do
      if [[ -r "$dc_comp" ]]; then
        source "$dc_comp"
        break
      fi
    done
  fi

  # Kubernetes: kubectl, helm, kustomize, argocd, istioctl, k9s
  _conch_completion_from_cmd() {
    local cmd="$1" subcmd="$2"
    (( ${+_comps[$cmd]} )) && return
    command -v "$cmd" &>/dev/null || return
    source <("$cmd" $subcmd 2>/dev/null) 2>/dev/null
  }
  _conch_completion_from_cmd kubectl "completion zsh"
  _conch_completion_from_cmd helm "completion zsh"
  _conch_completion_from_cmd kustomize "completion zsh"
  _conch_completion_from_cmd argocd "completion zsh"
  _conch_completion_from_cmd istioctl "completion zsh"
  _conch_completion_from_cmd k9s "completion zsh"
  _conch_completion_from_cmd flux "completion zsh"

  # Terraform
  if (( ! ${+_comps[terraform]} )) && command -v terraform &>/dev/null; then
    autoload -U +X bashcompinit && bashcompinit
    complete -o nospace -C "$(command -v terraform)" terraform
  fi

  # AWS CLI v2 completer
  if (( ! ${+_conch_aws_loaded} )); then
    if command -v aws_completer &>/dev/null; then
      autoload -U +X bashcompinit 2>/dev/null && bashcompinit 2>/dev/null
      complete -C "$(command -v aws_completer)" aws
      _conch_aws_loaded=1
    else
      local aws_completer
      for aws_completer in \
        /usr/local/bin/aws_zsh_completer.sh \
        /opt/homebrew/bin/aws_zsh_completer.sh \
        "$(dirname "$(command -v aws 2>/dev/null)" 2>/dev/null)/aws_zsh_completer.sh" \
        "$brew_prefix/share/zsh/site-functions/aws_zsh_completer.sh" \
        "$HOME/.local/bin/aws_zsh_completer.sh"; do
        if [[ -r "$aws_completer" ]]; then
          source "$aws_completer"
          _conch_aws_loaded=1
          break
        fi
      done
    fi
  fi

  # npm: use npm's built-in bash completion via bashcompinit
  if (( ! ${+_comps[npm]} )) && command -v npm &>/dev/null; then
    autoload -U +X bashcompinit 2>/dev/null && bashcompinit 2>/dev/null
    source <(npm completion 2>/dev/null) 2>/dev/null
  fi

  # General completion settings for a good out-of-the-box experience
  zstyle ':completion:*' menu select                    # arrow-key menu
  zstyle ':completion:*' matcher-list 'm:{a-z}={A-Z}'  # case-insensitive
  zstyle ':completion:*' list-colors "${(s.:.)LS_COLORS}"
  zstyle ':completion:*:descriptions' format '%F{cyan}-- %d --%f'
  zstyle ':completion:*:warnings' format '%F{red}No matches%f'
}

conch-setup() {
  # -s bindings: when key is pressed, zsh types the string literally
  # This is simpler and more reliable than ZLE widgets for key triggers
  bindkey -s '^ ' 'ask\n'        # Ctrl+Space -> types "ask" and Enter
  bindkey -s '\e\e' 'ask\n'      # Escape Escape -> types "ask" and Enter
  bindkey -s '^G' 'ask\n'        # Ctrl+G -> types "ask" and Enter
  bindkey -s '^X^G' 'chat\n'     # Ctrl+X then Ctrl+G -> starts chat
  KEYTIMEOUT=${KEYTIMEOUT:-40}
  (( KEYTIMEOUT < 80 )) && KEYTIMEOUT=80  # 0.8s window for Esc Esc
  conch-completion-setup
}
