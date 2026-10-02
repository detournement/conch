# capitol-frontend cookbook — recipes with exact commands

One recipe per archetype. Every recipe starts from the matching
directory under `templates/` (beside this file; the absolute path is in
the `[Skill assets: …]` line of the rendered skill), copies it, fills in
the `REPLACE_*` placeholders, and ends with the template's headless
verifier plus a fallback drill. The expected output lines shown are
what a passing run prints (ids will differ). A scaffold ships only after
they pass.

The templates were distilled from proven apps and fix three defects the
originals had: the idempotency key is derived from the form contents
(the originals minted a UUID per click), the run follower checks
`get_workflow_status` before trusting the stream (a finished run's
stream does not always replay its terminal frame), and HITL prompts are
relayed. Do not "improve" the templates back toward the originals.

## Recipe 1 — workflow console (A2A, static ES modules)

Template: `templates/console/`

```
index.html, app.css          one <main>, four panels (setup, form, run, output)
config/app.config.json       gateway_url, workflow_id, idempotency_prefix, fields[]
js/a2a-client.js             the browser/Node A2A client — do not edit
js/run.js                    idempotency key, composeInputs, waitForTerminal, HITL answer
js/run-view.js               node progress, reasoning toggle, deliverables
js/app.js                    token setup (localStorage) → form → run → deliverables
preflight.sh                 card + handshake + list_workflows with the bearer
verify-run.mjs               headless card → handshake → keyed run → terminal → deliverables
README.md                    run/serve/verify instructions for the user
```

1. Pin the workflow (tool calls, not curl):

```json
{"op": "workflows"}
```

```json
{"op": "describe", "workflow_id": "<id from the catalog>"}
```

   Keep the workflow id and the `fields[].key` strings
   (`"<node_instance_id>.<field_id>"`) exactly as returned.

2. Scaffold and configure:

```
cp -R "<skill assets dir>/templates/console" <app-dir> && cd <app-dir>
```

   Edit `config/app.config.json`: `gateway_url` = the agent's full
   `{base}/a2a/{org}/{agent}` URL from `op='discover'`; `workflow_id`;
   `fields[]` — one row per field you expose (`key` from describe,
   `label`, `type` = `textarea`|`text`|`number`, `required`, `default`,
   `help`). Required fields render as visible inputs, optional ones
   under an "Optional fields" fold. Leave no `REPLACE_` anywhere
   (`grep -r REPLACE_ . ` must print nothing).

3. Preflight (the bearer comes from the user's shell, never from a file
   you went looking for):

```
bash preflight.sh
```

   Expected:

```
card: <agent name> (streaming=true)
handshake: context_id=<uuid>
workflows: <workflow id> …
preflight: PASS
```

4. Verify headlessly. The verifier uses the config's `fields[].default`
   values unless you pass `--input KEY=VALUE`:

```
node verify-run.mjs --timeout 2400
```

   Expected:

```
card: <agent name> streaming=true
handshake: context_id=<uuid>
run: <run id> key=<prefix>:<workflow id>:<16 hex> nodes=<n> status=<queued|running|success> (new | existing run — replayed, nothing new started)
stream: live
events: workflow.run_started=1 node.node_started=… workflow.files_available=… workflow.run_completed=1
terminal: success
deliverable: <filename> <mime> url=yes
deliverables: <n>
verify: PASS
```

   Run it twice with the same inputs: the second run prints the same
   run id with `existing run — replayed` — that is the idempotency
   drill. A long workflow takes as long as it takes (tens of minutes
   is normal); keep the timeout generous and do not start a second run
   while one is in flight.

5. Serve and hand over:

```
python3 -m http.server 8080      # in <app-dir>; curl -sf http://localhost:8080/ | head -c 200
```

   The user pastes the `cap_a2a_*` token on the setup screen (stored in
   localStorage). Remind them the console is for local/demo deployments
   where the gateway is reachable from the browser.

6. Fallback drill: set `gateway_url` to a dead port (e.g.
   `http://localhost:1/a2a/x/y`), reload — the app must show "Could not
   reach the agent" and nothing else; restore the URL.

## Recipe 2 — workflow-fed page (filestore data island)

Template: `templates/fed-page/`

```
template/index.html          the page; only the app-data island content changes at serve time
public/page.js, page.css     reads the island, renders it (replace with the real design)
data/baseline.json           cold-start fallback — a REAL copy of the document
api/index.js                 fetch doc → validate → splice island → serve (X-Data-Source header)
vercel.json                  / and /index.html → the function; security headers
serve.mjs                    local stand-in for the Vercel runtime
verify-page.mjs              fetches the page, parses the island, checks the source
```

1. Confirm with the user: `FILESTORE_BASE`, org id, repo, document
   path, and (if the document has them) the `id`/`schema` values to
   trust. Fetch the document once and make it the baseline:

```
curl -sf -H "Authorization: Bearer $FILESTORE_ORG_TOKEN" \
  "$FILESTORE_BASE/v1/orgs/$FILESTORE_ORG_ID/repos/$FILESTORE_REPO/files/<url-encoded DOC_PATH>" \
  > data/baseline.json && python3 -m json.tool data/baseline.json | head -20
```

2. Scaffold: `cp -R "<skill assets dir>/templates/fed-page" <app-dir>`,
   replace `data/baseline.json` with the real document (step 1), then
   adapt `public/page.js` to the document's actual keys.

3. Run and verify (env vars are read by `api/index.js`; the token never
   appears in the page):

```
FILESTORE_BASE=… FILESTORE_ORG_TOKEN=… FILESTORE_ORG_ID=… FILESTORE_REPO=… \
DOC_PATH=… EXPECTED_SCHEMA=… node serve.mjs 3000 &
node verify-page.mjs http://localhost:3000/ --expect-source live
```

   Expected:

```
status: 200
source: live
island: id=<document id> keys=<n> title="<title>"
verify: PASS
```

   A second request within 15 s prints `source: cached`.

4. Fallback drill: restart with `FILESTORE_ORG_TOKEN=bad` (or
   `FILESTORE_BASE=http://127.0.0.1:1`) and run the verifier with
   `--expect-source baseline`: the page still serves, from the baked
   baseline. Restore and re-check `source: live`.

5. Deploy (`vercel --prod`) only when the user asks; set the same env
   vars in the project settings. The page must never fetch the facade
   from the browser.

## Recipe 3 — filestore portal (BFF + sign-in)

Template: `templates/portal/`

```
public/index.html, app.js, app.css   sign-in panel (Google button or dev login), records list
api/_shared.js                       env config (fail-closed), session cookie, allowlist, facadeFetch
api/auth/login.js, me.js, logout.js  Google ID-token verify + allowlist → HMAC session cookie
api/feed.js                          tree + files under RECORDS_PREFIX → records (live | cached | last-known-good)
serve.mjs                            local stand-in for the Vercel runtime (routes /api/* to api/*.js)
verify-portal.mjs                    401 anonymous, 403 denied, session, feed, me, logout
vercel.json                          security headers
```

1. Confirm the records contract: `FILESTORE_BASE`, org id, repo,
   `RECORDS_PREFIX` (folder holding `*.json` records), the allowlist,
   and one real record fetched and read before touching render code:

```
curl -sf -H "Authorization: Bearer $FILESTORE_ORG_TOKEN" \
  "$FILESTORE_BASE/v1/orgs/$FILESTORE_ORG_ID/repos/$FILESTORE_REPO/tree?recursive=true&path=<RECORDS_PREFIX>" | head -c 600
```

2. Scaffold: `cp -R "<skill assets dir>/templates/portal" <app-dir>`;
   adapt `renderRecords` in `public/app.js` to the record shape.

3. Run locally with dev sign-in (local only — `ALLOW_DEV_LOGIN` is
   ignored when `VERCEL_ENV=production`) and verify:

```
FILESTORE_BASE=… FILESTORE_ORG_TOKEN=… FILESTORE_ORG_ID=… FILESTORE_REPO=… RECORDS_PREFIX=… \
SESSION_SECRET="$(openssl rand -hex 32)" ALLOWLIST="you@example.com,*@your-domain" \
ALLOW_DEV_LOGIN=1 node serve.mjs 3000 &
node verify-portal.mjs http://localhost:3000 --email you@example.com
```

   Expected:

```
ok   anonymous /api/feed is refused — status 401
ok   non-allowlisted sign-in is refused — status 403
ok   allowlisted dev sign-in issues a session — status 200
ok   /api/auth/me reflects the signed-in email — {…}
ok   /api/feed answers with records — status 200
     source=live records=<n> stale=false
ok   logout clears the session
verify: PASS
```

4. Fallback drills: start without `SESSION_SECRET` → every `/api/*`
   answers `{"error":"misconfigured", …}` (fail closed). With a warm
   cache, point `FILESTORE_BASE` at a dead port → `/api/feed` answers
   `source=last-known-good stale=true` and the UI shows the staleness
   note; with a cold cache it answers 502 `upstream` — never rows it
   made up.

5. Production: set `GOOGLE_OAUTH_CLIENT_ID` (Google Identity Services
   client id for the deployed origin), the `FILESTORE_*` vars,
   `SESSION_SECRET`, `ALLOWLIST`; do not set `ALLOW_DEV_LOGIN`.

## Shared checklist (any archetype)

- Secrets: `grep -rE "cap_a2a_[A-Za-z0-9_-]{20,}" <app-dir>` and
  `grep -rn "token" <app-dir> --include=*.json` must show no literal
  tokens (the templates mention the `cap_a2a_` *prefix* in docs and
  prompts — that is not a token; a real one is `cap_a2a_` + 48
  base64url chars); server tokens only in env, browser tokens only in
  localStorage, demo bootstrap tokens documented as demo-only.
- Honesty drill done (upstream dead ⇒ declared degraded state, never
  stale-as-fresh or invented content).
- Idempotency drill done (console): the second `verify-run.mjs` with the
  same inputs prints the same run id and `existing run — replayed`.
- The `capitol-frontend checklist` block from SKILL.md is in your final
  message, every line filled from real output.
