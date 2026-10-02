# Filestore portal (BFF + sign-in)

The browser talks only to this app's `/api/*`. The BFF holds the
filestore org token, verifies Google sign-in against an allowlist,
issues an HMAC-signed session cookie, and reads records from the
facade (live, 30 s cache, last-known-good with `stale: true` when the
facade is down). Missing configuration makes every endpoint answer
`{"error":"misconfigured"}` instead of serving anything.

## Environment (server-side only)

| Variable | Meaning |
|---|---|
| `FILESTORE_BASE` | facade origin |
| `FILESTORE_ORG_TOKEN` | org-scoped bearer (sensitive) |
| `FILESTORE_ORG_ID` | org uuid |
| `FILESTORE_REPO` | repo name |
| `RECORDS_PREFIX` | folder holding the `*.json` records, e.g. `gov-feed/records` |
| `SESSION_SECRET` | HMAC key for the session cookie (`openssl rand -hex 32`) |
| `ALLOWLIST` | comma-separated exact emails and `*@domain` entries |
| `GOOGLE_OAUTH_CLIENT_ID` | Google Identity Services client id (required in production) |
| `ALLOW_DEV_LOGIN` | `1` enables `{dev_email}` sign-in without Google — local only, ignored when `VERCEL_ENV=production` |

## Local run and verification (Node 20+)

```
FILESTORE_BASE=… FILESTORE_ORG_TOKEN=… FILESTORE_ORG_ID=… FILESTORE_REPO=… RECORDS_PREFIX=… \
SESSION_SECRET="$(openssl rand -hex 32)" ALLOWLIST="you@example.com" ALLOW_DEV_LOGIN=1 node serve.mjs 4330 &
node verify-portal.mjs http://localhost:4330 --email you@example.com      # → "verify: PASS"
```

## Endpoints

- `POST /api/auth/login` `{credential}` (Google ID token) or `{dev_email}` (dev only) → session cookie
- `GET /api/auth/me` → `{signed_in, email, google_client_id, dev_login}`
- `POST /api/auth/logout` → clears the cookie
- `GET /api/feed` → `{records, source, fetched_at, stale}` (401 without a session)

## Deploy

`vercel --prod` with the production environment set (no
`ALLOW_DEV_LOGIN`). Editing `ALLOWLIST` revokes existing sessions for
removed accounts on their next request.
