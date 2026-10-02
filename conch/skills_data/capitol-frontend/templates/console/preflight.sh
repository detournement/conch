#!/usr/bin/env bash
# Preflight for the console: proves the gateway, the bearer and the
# workflow allowlist are all real before any app code is touched.
#
#   CAPITOL_A2A_BEARER=... bash preflight.sh [config/app.config.json]
#
# Expected output (ids will differ):
#   card: Market Research Demo Orchestrator (streaming=true)
#   handshake: context_id=ctx_...
#   workflows: market_research_sources_sought_analyzer
#   preflight: PASS
set -euo pipefail

CONFIG="${1:-$(dirname "$0")/config/app.config.json}"
: "${CAPITOL_A2A_BEARER:?CAPITOL_A2A_BEARER is not set — export the cap_a2a_* token from your shell (never paste it into files)}"

GATEWAY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_url"].rstrip("/"))' "$CONFIG")"
case "$GATEWAY" in
  *REPLACE_*) echo "preflight: FAIL — gateway_url in $CONFIG still holds a placeholder" >&2; exit 2 ;;
esac

AUTH="Authorization: Bearer $CAPITOL_A2A_BEARER"
CARD="$(curl -sS -m 10 -f -H "$AUTH" "$GATEWAY/.well-known/agent-card.json")" \
  || { echo "preflight: FAIL — agent card fetch failed (wrong gateway_url or rejected bearer)" >&2; exit 1; }
python3 - "$CARD" <<'PY'
import json, sys
card = json.loads(sys.argv[1])
print(f"card: {card.get('name', '(unnamed)')} (streaming={str(bool(card.get('capabilities', {}).get('streaming'))).lower()})")
PY

rpc() {
  curl -sS -m 20 -f -H "$AUTH" -H 'Content-Type: application/json' "$GATEWAY" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"preflight\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"parts\":[{\"type\":\"data\",\"data\":$1}]}}}"
}

HS="$(rpc '{"skill_id":"handshake","caller":{"system":"preflight","version":"0"},"capabilities":{"supports_sse":true}}')" \
  || { echo "preflight: FAIL — handshake request failed" >&2; exit 1; }
python3 - "$HS" <<'PY'
import json, sys
body = json.loads(sys.argv[1])
if body.get("error"):
    print(f"preflight: FAIL — handshake error {body['error']}", file=sys.stderr); sys.exit(1)
parts = body.get("result", {}).get("status", {}).get("message", {}).get("parts", [])
data = next((p.get("data") for p in parts if isinstance(p, dict) and p.get("data")), {})
print(f"handshake: context_id={data.get('session', {}).get('context_id', '(none)')}")
PY

WF="$(rpc '{"skill_id":"list_workflows"}')" || { echo "preflight: FAIL — list_workflows request failed" >&2; exit 1; }
python3 - "$WF" <<'PY'
import json, sys
body = json.loads(sys.argv[1])
if body.get("error"):
    print(f"preflight: FAIL — list_workflows error {body['error']}", file=sys.stderr); sys.exit(1)
parts = body.get("result", {}).get("status", {}).get("message", {}).get("parts", [])
data = next((p.get("data") for p in parts if isinstance(p, dict) and p.get("data")), {})
ids = [w.get("workflow_id") or w.get("id") for w in data.get("workflows", [])]
if not ids:
    print("preflight: FAIL — this agent allowlists no workflows", file=sys.stderr); sys.exit(1)
print("workflows: " + " ".join(str(i) for i in ids))
PY
echo "preflight: PASS"
