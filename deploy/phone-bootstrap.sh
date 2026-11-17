#!/usr/bin/env bash
# Bootstrap the sovereign phone stack (Conduit + ntfy) for conch.
#
# What it does, idempotently where possible:
#   1. ensures a registration token exists in deploy/.env (generated,
#      never printed to the log) and the stack is up
#   2. registers two users on YOUR homeserver: you, and conch (the bot)
#   3. logs conch in and writes its access token BY REFERENCE to a
#      0600 env file (default ~/.config/conch/phone.env) — the token is
#      never echoed
#   4. creates the direct room between the two users
#   5. prints the conch config block, the Element login steps, and the
#      ntfy app subscribe steps
#
# Usage:
#   deploy/phone-bootstrap.sh [owner-username]
#
# Environment overrides:
#   PHONE_HOMESERVER   default http://localhost:6167
#   PHONE_SERVER_NAME  default conch.local  (must match compose .env)
#   PHONE_NTFY_URL     default http://localhost:8093
#   PHONE_TOKEN_FILE   default ~/.config/conch/phone.env
#   PHONE_NTFY_TOPIC   default conch-alerts
set -euo pipefail

HOMESERVER="${PHONE_HOMESERVER:-http://localhost:6167}"
SERVER_NAME="${PHONE_SERVER_NAME:-conch.local}"
NTFY_URL="${PHONE_NTFY_URL:-http://localhost:8093}"
TOKEN_FILE="${PHONE_TOKEN_FILE:-$HOME/.config/conch/phone.env}"
NTFY_TOPIC="${PHONE_NTFY_TOPIC:-conch-alerts}"
OWNER="${1:-you}"
BOT="conch"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DEPLOY_DIR/.env"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || fail "curl is required"
command -v python3 >/dev/null || fail "python3 is required"

json_get() {  # json_get <key> — reads JSON on stdin, prints the key or ""
    python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except Exception:
    print("")' "$1"
}

# --- 1. registration token + stack ------------------------------------------
if [ ! -f "$ENV_FILE" ] || ! grep -q '^CONDUIT_REGISTRATION_TOKEN=' "$ENV_FILE"; then
    say "generating a registration token into deploy/.env"
    REG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    {
        echo "PHONE_SERVER_NAME=$SERVER_NAME"
        echo "CONDUIT_REGISTRATION_TOKEN=$REG_TOKEN"
    } >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
else
    REG_TOKEN="$(grep '^CONDUIT_REGISTRATION_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
fi

if ! curl -fsS "$HOMESERVER/_matrix/client/versions" >/dev/null 2>&1; then
    say "starting the stack (docker compose -f deploy/docker-compose.phone.yml up -d)"
    docker compose -f "$DEPLOY_DIR/docker-compose.phone.yml" --env-file "$ENV_FILE" up -d
    for _ in $(seq 1 30); do
        curl -fsS "$HOMESERVER/_matrix/client/versions" >/dev/null 2>&1 && break
        sleep 1
    done
    curl -fsS "$HOMESERVER/_matrix/client/versions" >/dev/null 2>&1 \
        || fail "homeserver did not come up at $HOMESERVER"
fi
say "homeserver answering at $HOMESERVER"

# --- 2. register the two users ------------------------------------------------
register_user() {  # register_user <name> <password>; ok if already taken
    local name="$1" password="$2" session response errcode
    # UIA: first request opens the session, second completes the
    # registration-token stage.
    response="$(curl -fsS -X POST "$HOMESERVER/_matrix/client/v3/register" \
        -H 'Content-Type: application/json' -d '{}' 2>/dev/null || true)"
    session="$(printf '%s' "$response" | json_get session)"
    response="$(curl -sS -X POST "$HOMESERVER/_matrix/client/v3/register" \
        -H 'Content-Type: application/json' \
        -d "$(python3 -c 'import json,sys
print(json.dumps({
    "username": sys.argv[1], "password": sys.argv[2],
    "initial_device_display_name": "conch-phone-bootstrap",
    "auth": {"type": "m.login.registration_token",
             "token": sys.argv[3], "session": sys.argv[4]},
}))' "$name" "$password" "$REG_TOKEN" "$session")")"
    errcode="$(printf '%s' "$response" | json_get errcode)"
    if [ -n "$errcode" ] && [ "$errcode" != "M_USER_IN_USE" ]; then
        fail "registering @$name:$SERVER_NAME failed: $errcode"
    fi
    if [ "$errcode" = "M_USER_IN_USE" ]; then
        say "@$name:$SERVER_NAME already exists — keeping it"
    else
        say "registered @$name:$SERVER_NAME"
    fi
}

read -r -s -p "password for @$OWNER:$SERVER_NAME (your Element login): " OWNER_PW; echo
BOT_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
register_user "$OWNER" "$OWNER_PW"
register_user "$BOT" "$BOT_PW"

# --- 3. conch access token, by reference ---------------------------------------
LOGIN_RESPONSE="$(curl -fsS -X POST "$HOMESERVER/_matrix/client/v3/login" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys
print(json.dumps({
    "type": "m.login.password",
    "identifier": {"type": "m.id.user", "user": sys.argv[1]},
    "password": sys.argv[2],
    "initial_device_display_name": "conch-daemon",
}))' "$BOT" "$BOT_PW")")" \
    || fail "conch login failed (existing bot user with a different password? remove it or set a fresh server)"
ACCESS_TOKEN="$(printf '%s' "$LOGIN_RESPONSE" | json_get access_token)"
[ -n "$ACCESS_TOKEN" ] || fail "no access token in the login response"

mkdir -p "$(dirname "$TOKEN_FILE")"
umask 077
printf 'MATRIX_ACCESS_TOKEN=%s\n' "$ACCESS_TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
say "conch access token written to $TOKEN_FILE (0600, by reference — never put it in conch config)"

# --- 4. the direct room ---------------------------------------------------------
ROOM_RESPONSE="$(curl -fsS -X POST "$HOMESERVER/_matrix/client/v3/createRoom" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys
print(json.dumps({
    "name": "conch", "is_direct": True,
    "preset": "trusted_private_chat",
    "invite": [f"@{sys.argv[1]}:{sys.argv[2]}"],
}))' "$OWNER" "$SERVER_NAME")")" \
    || fail "createRoom failed"
ROOM_ID="$(printf '%s' "$ROOM_RESPONSE" | json_get room_id)"
[ -n "$ROOM_ID" ] || fail "no room_id in the createRoom response"
say "created room $ROOM_ID (invited @$OWNER:$SERVER_NAME)"

# --- 5. what to do next ----------------------------------------------------------
cat <<DONE

Add to ~/.config/conch/config:

    remote_enabled = true
    notify_channel = matrix
    matrix_homeserver = $HOMESERVER
    matrix_room = $ROOM_ID
    matrix_user = @$BOT:$SERVER_NAME
    matrix_allowed_senders = @$OWNER:$SERVER_NAME
    notify_push = ntfy
    ntfy_url = $NTFY_URL
    ntfy_topic = $NTFY_TOPIC

Give the daemon the token by reference (never inline):
    systemd: EnvironmentFile=$TOKEN_FILE in a drop-in
    launchd: launchctl setenv MATRIX_ACCESS_TOKEN "\$(cut -d= -f2- $TOKEN_FILE)"
    shell:   set -a; . $TOKEN_FILE; set +a

Element (your phone):
    1. Install Element (iOS/Android/desktop).
    2. Sign in — "Edit" the homeserver and enter: $HOMESERVER
       (reachable address from the phone, e.g. your tailnet IP)
    3. Log in as @$OWNER:$SERVER_NAME with the password you just set.
    4. Accept the "conch" room invite. That room is your conch thread.

ntfy (your phone):
    1. Install the ntfy app (F-Droid / Play Store / App Store).
    2. In the app settings, set the default server to: $NTFY_URL
       (again: the address reachable from the phone)
    3. Subscribe to the topic: $NTFY_TOPIC
    4. Approval requests, digests, and mission milestones now buzz the
       phone; tapping one deep-links into the Element room.

Hardening (optional, recommended once bootstrapped):
    - close registration: set CONDUIT_ALLOW_REGISTRATION=false in
      deploy/.env and re-run docker compose up -d
    - require auth on ntfy: NTFY_AUTH_DEFAULT_ACCESS=deny-all in
      deploy/.env, then create a user + token (see the compose file
      comments) and export it as NTFY_TOKEN next to the matrix token.
DONE
