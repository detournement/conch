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

## App catalog — what conch builds on Capitol

Each entry: the app in one line · archetype · the workflow(s) it leans
on · the leverage pattern (P1–P5 below). **Shipped** means the templates
produce it today; **extension** names the one piece that is missing.
Only allowlisted workflows exist for an app — when none fits, author one
(Recipe 4).

| # | App | Archetype | Workflow(s) | Pattern | Status |
|---|---|---|---|---|---|
| 1 | Market-research intake console: paste a requirement, watch the 12-node run, download the docx + xlsx (modeled on cap-app-acc-market-research) | console | `market_research_sources_sought_analyzer` | P1 | shipped — this skill's verification app |
| 2 | Public tracker page: a scheduled workflow publishes one JSON document; the page serves it with a baked baseline (modeled on cap-app-prop40-tracker) | fed-page | a *filestore-publishing* workflow + its schedule | P2 | shipped, if the workflow already writes the document to the filestore; compiled v1 workflows don't (extension: publish stage) |
| 3 | Deal-flow feed: team signs in, browses the briefs an intake workflow filed under one prefix (modeled on cap-app-together-feed) | portal | the fund's intake workflow (writes records) | P3 | shipped |
| 4 | Government-opportunities portal over `gov-feed/records` (verified live) | portal | the gov discovery/assessment workflows (write records) | P3 | shipped |
| 5 | Intake console returning deliverables for *any* allowlisted input→agent→docx/markdown workflow | console | existing, or one authored with Recipe 4 | P1 | shipped |
| 6 | Weekly digest page fed by a cron'd workflow (news themes, KPI roll-up) | fed-page | authored workflow + schedule | P2 | schedule + page shipped; publish-to-filestore hop is an extension (or use an existing publishing workflow) |
| 7 | Team portal over filestore with Google sign-in and an allowlist | portal | whichever workflows write the records | P3 | shipped |
| 8 | Approvals console: a run pauses at a HITL checkpoint, the reviewer answers in the panel (clarification or continue/stop) | console | any workflow with HITL nodes | P5 | shipped per run; a cross-run approvals *queue* is an extension (needs a runs listing endpoint) |
| 9 | Email-triggered pipeline with a status page: an ingest process starts keyed runs; a page shows the latest status/deliverables | console (read mode) or fed-page | authored workflow; `/compile from-email` drafts it | P4 | app side shipped; the trigger process is an extension (a conch mission or ingest job calling `call_workflow`) |
| 10 | Eval / QA review board across runs | portal | any workflow with evals | P3 | extension — needs the eval roll-ups published as filestore records |
| 11 | Multi-step wizard console: the agent asks clarifying questions mid-run; the wizard relays them verbatim | console | workflow with clarification nodes | P5 | shipped |
| 12 | Demo console for a freshly authored workflow, pinned to the version it was built against, with a one-command rollback | console | authored with Recipe 4 | P1 | shipped — Recipe 4 ends exactly here |

### Leverage patterns (how an app uses a workflow)

- **P1 — on-demand runs started by a console.** The form composes
  `<node_instance_id>.<field_id>` inputs, `call_workflow` carries a
  content-hashed `idempotency_key` and the pinned `version_id`, the run
  is followed status-first, deliverables come from
  `workflow.files_available`. Template: console.
- **P2 — scheduled runs feeding a page.** A cron schedule (created by
  the card's `schedules` in Recipe 4, or `/capitol admin schedule-add`)
  runs the workflow; the workflow publishes a document to the filestore;
  the page splices it at serve time with last-known-good fallback.
  Template: fed-page. The publish hop must be in the workflow.
- **P3 — workflow outputs written to filestore, read by a portal.** Many
  runs → many records under one prefix → a BFF lists them behind
  sign-in. Template: portal.
- **P4 — externally triggered workflows (email/event).** Something
  outside the app (a conch mission, an ingest job) calls `call_workflow`
  with a key derived from the message/event id; the app only *reads*
  (console read mode or a fed page). Never put the trigger in the
  browser.
- **P5 — HITL steps surfaced as app panels.** `node.input_required`
  events render the exact prompt; answers go back through
  `submit_clarification_response` / `submit_intervention_response`.
  Template: console (`hitl-panel`).

## Recipe 1 — workflow console (A2A, static ES modules)

Template: `templates/console/`

```
index.html, app.css          one <main>, four panels (setup, form, run, output)
config/app.config.json       gateway_url, workflow_id, workflow_version_id (the pin), idempotency_prefix, fields[]
js/a2a-client.js             the browser/Node A2A client — do not edit
js/run.js                    idempotency key, composeInputs, waitForTerminal, HITL answer
js/run-view.js               node progress, reasoning toggle, deliverables
js/app.js                    token setup (localStorage) → form → run → deliverables
preflight.sh                 card + handshake + list_workflows + version pin check with the bearer
verify-run.mjs               headless card → handshake → pin check → keyed run → terminal → deliverables
README.md                    run/serve/verify instructions for the user
```

1. Pin the workflow (tool calls, not curl):

```json
{"op": "workflows"}
```

```json
{"op": "describe", "workflow_id": "<id from the catalog>"}
```

   Keep the workflow id, the `fields[].key` strings
   (`"<node_instance_id>.<field_id>"`) and the `version_id` exactly as
   returned. The `version_id` is the pin: the app will run that exact
   saved version until someone moves the pin (see *Upgrading*).

2. Scaffold and configure:

```
cp -R "<skill assets dir>/templates/console" <app-dir> && cd <app-dir>
```

   Edit `config/app.config.json`: `gateway_url` = the agent's full
   `{base}/a2a/{org}/{agent}` URL from `op='discover'`; `workflow_id`;
   `workflow_version_id` = the `version_id` from describe (leave `""`
   only when the user explicitly wants "always latest");
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
version: pinned <version id> (current)        # or: unpinned (latest <id>) / pinned … (behind; …)
preflight: PASS
```

   `behind` means the workflow was re-saved after the app was built:
   the app keeps running its pinned version; see *Upgrading*. A pin
   that is not a saved version is `preflight: FAIL` (fail closed).

4. Verify headlessly. The verifier uses the config's `fields[].default`
   values unless you pass `--input KEY=VALUE`:

```
node verify-run.mjs --timeout 2400
```

   Expected:

```
card: <agent name> streaming=true
handshake: context_id=<uuid>
version: pinned <version id> (current)
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
python3 -m http.server 4310      # in <app-dir>; curl -sf http://localhost:4310/ | head -c 200
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
DOC_PATH=… EXPECTED_SCHEMA=… node serve.mjs 4320 &
node verify-page.mjs http://localhost:4320/ --expect-source live
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
ALLOW_DEV_LOGIN=1 node serve.mjs 4330 &
node verify-portal.mjs http://localhost:4330 --email you@example.com
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

## Recipe 4 — author the workflow an app needs (ProcessCompiler)

Use when `op='workflows'` lists nothing that fits. The model drafts;
**the user runs every command below** — approval is a user act at the
shell, nothing is created without it, and every mutation is
idempotency-keyed with a rollback reference in conch's ledger. Needs
`capitol_admin = true` in the conch config and the local serving stack
(`capitol_base_url`, `capitol_platform_url`, `capitol_org`); the admin
token resolves by reference (env or registry), never printed.

1. Draft the goal from the leverage pattern. Compiled v1 workflows are a
   stage graph: `text_input`/`json_input` → `agent` (optional catalog
   tools) → `docx` | `markdown_output` | `notify`. Say which, name the
   input, say whether a schedule is wanted (cron, disabled by default)
   and whether it is a test artifact. Example (a deliberately tiny test
   workflow):

```
/compile "TEST ARTIFACT for the capitol-frontend skill (safe to delete): a workflow that takes one text input 'requirement' and returns a one-paragraph markdown summary of it. One agent stage, no tools, no schedule, no notifications. Create a dedicated orchestrator agent allowlisting only this workflow. Name the assets however your identity convention requires, including the words frontend-test."
```

   Expected (ids differ):

```
✓ Compiled #<n> [compiled] TEST ARTIFACT …  card v1  <compilation id>…
review: /compile show <id> — then approve/reject/revise
```

2. Review, then approve (the authorization moment):

```
/compile show <id>          # stages, assets to create, schedules (enabled?), HITL points, open questions
/compile approve <id>
```

   Open questions on the card mean the model parked an ambiguity rather
   than guessing it into infrastructure — answer them with
   `/compile revise <id> "<guidance>"` before approving.

3. Materialize (collections → workflow version → agent + exact allowlist
   → schedules → pack + lock → acceptance drill → supervising mission):

```
/compile materialize <id>
```

   Expected:

```
materializing <compilation id>
  workflow <identity>: <workflow uuid> version <version uuid> (linked|documentation_pending)
  agent <identity>: <agent uuid>
  pack: <packs dir>/<pack name>
  lock: sha256:…
✓ materialized — running the validation gate …
  drill … passed
✓ verified — drill passed, supervising mission <mission id> (dry-run)
```

   The drill is one real run of the new workflow with synthetic input
   (that is your "new runs started: 1"). The agent's bearer is sunk into
   the user's A2A registry under the agent identity; the user exports it
   as `CAPITOL_A2A_BEARER` for the app (you never read the registry).

4. Read the ids and the pin:

```
/compile status <id>
cat <packs dir>/<pack name>/materialization-lock.json
```

   `workflows[].workflow_id` / `workflow_version_id` /
   `workflow_payload_digest` in the lock are the provenance of what the
   app is built against. Gateway URL for the app:
   `<capitol_base_url>/a2a/<capitol_org>/<agent uuid>`.

5. Wire the app: Recipe 1 against the new agent (`op='discover'` and
   `op='describe'` now go through the new gateway URL — set
   `capitol_agent` or pass the ids the user gives you), with
   `workflow_version_id` = the lock's `workflow_version_id`. The field
   key for a `text_input` stage is `<node uuid>.text_input` as describe
   returns it.

6. Undo, when it was a test or the user changes their mind:

```
/compile rollback <id>
```

   Everything the materialization created is reverted in reverse order
   from the recorded rollback refs (schedules, agent, workflow version
   or the whole workflow when it was first created). The app's preflight
   then fails closed (`agent card fetch failed`) — the honest state.

Hand-written payloads follow the same discipline through
`/capitol admin persist @payload.json` → `/capitol admin publish <wf>`
→ `/capitol admin schedule-add <wf> <name> <cron>` → `/capitol admin
rollback <wf>`; prefer the compiler, which generates payloads
deterministically from reviewed stages and ships the lock.

### Pinning and upgrading an app's workflow

- **Pin.** `config.workflow_version_id` is sent as `version_id` on
  `get_workflow_details` and `call_workflow`; the gateway runs that
  saved version even after the workflow is re-saved. The pin is part of
  the idempotency key, so moving it starts a new run for the same
  inputs. Record the pin in the app README ("built against vN,
  `<version id>`"); the compiler's `materialization-lock.json` is the
  source of truth for authored workflows.
- **Detect drift.** `bash preflight.sh` prints `version: pinned … (behind;
  latest <id> — upgrade available)`. Nothing changes by itself.
- **Upgrade.** (1) `op='describe'` with the new `version_id` — field
  keys may have changed; (2) copy the config with the new pin and run
  `node verify-run.mjs --config <copy>` (a new run — the user agreed);
  (3) on `verify: PASS`, move the pin, bump `app_version`, update the
  README line; (4) the old pin is the rollback — put it back and the app
  is exactly what it was.
- **Never** leave `workflow_version_id` empty to "fix" drift: an
  unpinned app silently changes behaviour every time someone saves the
  workflow.

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
- Version pinned (console): `workflow_version_id` set and preflight
  prints `(current)`; authored workflows cite the lock file.
- The `capitol-frontend checklist` block from SKILL.md is in your final
  message, every line filled from real output.
