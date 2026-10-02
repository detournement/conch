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
#   version: pinned 0b7d349b-... (current)        # or: unpinned (latest ...)
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

# Version pin: the app runs the exact saved version it was built against
# (config.workflow_version_id; empty = latest). A pin that is not a saved
# version fails closed; a stale pin is reported, not silently upgraded.
WORKFLOW_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("workflow_id",""))' "$CONFIG")"
PIN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("workflow_version_id","") or "")' "$CONFIG")"
case "$WORKFLOW_ID" in
  ""|*REPLACE_*) echo "version: skipped (workflow_id not set yet)" ;;
  *)
    DT="$(rpc "{\"skill_id\":\"get_workflow_details\",\"workflow_id\":\"$WORKFLOW_ID\"}")" \
      || { echo "preflight: FAIL — get_workflow_details request failed" >&2; exit 1; }
    VS=""
    if [ -n "$PIN" ]; then
      VS="$(rpc "{\"skill_id\":\"get_workflow_versions\",\"workflow_id\":\"$WORKFLOW_ID\"}")" || VS=""
    fi
    python3 - "$DT" "$PIN" "$VS" <<'PY'
import json, sys
def data(body):
    if not body:
        return {}
    body = json.loads(body)
    if body.get("error"):
        print(f"preflight: FAIL — gateway error {body['error']}", file=sys.stderr); sys.exit(1)
    parts = body.get("result", {}).get("status", {}).get("message", {}).get("parts", [])
    return next((p.get("data") for p in parts if isinstance(p, dict) and p.get("data")), {})
details, pin, history = data(sys.argv[1]), sys.argv[2], data(sys.argv[3])
latest = str(details.get("version_id") or "")
if not pin:
    print(f"version: unpinned (latest {latest})")
elif pin == latest:
    print(f"version: pinned {pin} (current)")
else:
    known = any(str(v.get("version_id") or v.get("id")) == pin for v in history.get("versions", []))
    if not known:
        print(f"preflight: FAIL — pinned version {pin} is not a saved version (latest {latest}); re-pin from `capitol_control op=describe`", file=sys.stderr); sys.exit(1)
    print(f"version: pinned {pin} (behind; latest {latest} — upgrade available, see cookbook 'Upgrading')")
PY
    ;;
esac
echo "preflight: PASS"
