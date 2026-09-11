#!/bin/sh
set -eu

mkdir -p \
  "${XDG_CONFIG_HOME:-$HOME/.config}/conch" \
  "${XDG_CACHE_HOME:-/tmp/conch-cache}" \
  "${XDG_STATE_HOME:-$HOME/.local/state}/conch"

case "${1:-chat}" in
  chat)
    [ "$#" -eq 0 ] || shift
    exec conch "$@"
    ;;
  ask)
    shift
    exec conch-ask "$@"
    ;;
  health)
    exec python -c '
from conch.config import load_config
from conch.providers import check_ollama_health, list_custom_models

config = load_config()
provider = config.get("provider", "").lower()
if provider == "ollama":
    ok = check_ollama_health(config)
elif provider == "custom":
    ok = list_custom_models(
        config, force_refresh=True, tool_capable_only=False
    ) is not None
else:
    ok = True
raise SystemExit(0 if ok else 1)
'
    ;;
  shell)
    shift
    exec /bin/sh "$@"
    ;;
  conch|conch-chat|conch-ask|python|python3|sh)
    exec "$@"
    ;;
  -*)
    exec conch "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
