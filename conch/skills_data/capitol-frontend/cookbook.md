# capitol-frontend cookbook — worked scaffolds for this machine

Three complete recipes, one per archetype. Each names the local
reference app whose code is the proven implementation — copy from it
rather than re-deriving the wire handling. Recipes end with the live
verify steps; a scaffold ships only after they pass.

## Recipe 1 — workflow console (A2A, static ES modules)

Reference app: `~/composer/cap-app-acc-market-research` — intake form →
`call_workflow` → live SSE node progress → HITL relay → deliverable
download rows. Its `js/a2a-client.js` is the dependency-free browser
client for the whole gateway contract (envelope, streaming, resume,
uploads); reuse it unchanged.

1. Pin the workflow with `capitol_control`:

```json
{"op": "workflows"}
```

```json
{"op": "describe", "workflow_id": "<id from the catalog>"}
```

   Record the workflow id, the field rows, and the request-input key
   (`"<node_instance_id>.<field_id>"`). The intake form is generated
   from these fields: required fields as visible inputs, optional ones
   under an "Advanced" fold, everything else from schema defaults.
2. Scaffold the directory:

```
<app>/
  index.html              ES-module entry, one <main> per view
  app.css                 single stylesheet
  config/app.config.json  app name, gateway_url, intake definition,
                          pipeline (workflow id + input mapping)
  js/a2a-client.js        copied from the reference app
  js/app.js               view router + token/localStorage handling
  js/intake.js            form generated from the described fields
  js/run-view.js          node progress + deliverables + HITL prompts
```

   `gateway_url` is the full `{base}/a2a/{org}/{agent}` endpoint. The
   token flows in via a setup screen into localStorage (a
   `bootstrap_token` in config is localhost-demo-only — say so in the
   README).
3. Wire the run per reference.md: `handshake` once per session;
   `call_workflow` with the canonical input keys and a stable
   `idempotency_key` derived from the form contents (not minted per
   click); `subscribeRunEvents` for progress with `tasks/resubscribe`
   resume; deliverables deduped by file id across events and the
   terminal `files[]`; `node.input_required` rendered verbatim with the
   matching response skill on submit.
4. Verify live:
   - `curl -sf -H "Authorization: Bearer $CAPITOL_A2A_BEARER" <gateway_url>/.well-known/agent-card.json | head -c 200`
     — card reachable with the configured token.
   - `python3 -m http.server 8080` in the app dir; `curl -sf http://localhost:8080/` serves the entry page.
   - Drive one run end to end in the browser (user-approved if the
     workflow has real external effects) and watch it reach a terminal
     event with deliverables rendered.

## Recipe 2 — workflow-fed page (filestore data island)

Reference app: `~/composer/cap-app-prop40-tracker` — a static page
whose `<script id="…-data" type="application/json">` island is spliced
at serve time with the latest filestore document; a Capitol workflow
(scheduled) keeps that document fresh. See its `api/index.js` for the
complete splice handler and `README.md` for the architecture diagram.

1. Confirm with the user: org id, repo, document path, the document's
   schema id, and where the static page template comes from. Fetch the
   real document once (server-side token) and extract the island's
   baseline bytes to `data/baseline.json`.
2. Scaffold:

```
<app>/
  template/index.html   the page, never modified by the handler
  data/baseline.json    cold-start fallback, byte-exact island content
  api/index.js          the only backend: fetch doc → validate →
                        splice island → serve
  vercel.json           rewrites / + /index.html to the function,
                        security headers
```

3. The handler contract (from the reference implementation): locate the
   island markers once at module load; fetch
   `{FILESTORE_BASE}/v1/orgs/{org}/repos/{repo}/files/{path}` with the
   server-side token and a timeout; validate (parse + expected schema
   id); cache ~15 s; on any failure serve last-known-good then
   baseline, retrying next request; escape `</` → `<\/` before
   splicing.
4. Verify live: run the handler locally (or `vercel dev`), curl `/` and
   confirm the island carries the live document; point `FILESTORE_BASE`
   at a dead port and confirm the page still serves (fallback), then
   restore. Byte-identity check if the template was provided:
   render with the baseline and `cmp` against the original.

## Recipe 3 — filestore portal (BFF + sign-in)

Reference apps: `~/cg-worktrees/cap-app-together-feed-ref` (deal-flow
feed) and `~/cg-worktrees/cap-app-government-opportunities` (records
portal). Browser → own `/api/*` only; the BFF holds the org token,
verifies Google Identity sign-in against an allowlist, and reads the
facade — live (`DATA_SOURCE=facade`) or from a synced snapshot
(`DATA_SOURCE=kv`) that survives tunnel outages.

1. Confirm the records contract first: repo, records path, the record
   schema id, and one real record fetched and read before any render
   code. Malformed records are dropped and logged, partial records
   degrade gracefully — never guessed into shape.
2. Scaffold from the reference app's layout:

```
<app>/
  index.html, app.css, js/   static UI (api-client, render, app)
  api/_shared.js             env config, session cookie, facadeFetch
  api/auth/…                 Google ID-token verify + allowlist gate
  api/feed.js                records list (snapshot or facade)
  api/file.js                document pass-through downloads
  api/cron/sync.js           facade /tree + /files → snapshot
  vercel.json                rewrites, headers, cron schedule
```

3. Fail-closed rules to preserve verbatim: missing org/filestore/auth
   env ⇒ 500 misconfigured; fixture mode refuses to run on production;
   production never falls back to fixtures; a down facade in snapshot
   mode serves last-known-good **with a staleness banner**, in live
   mode an error banner — never fabricated rows.
4. Verify: `npm test` (contract: allowlist, session, record shapes) and
   the e2e drill (sign-in gate, denial, rows render, download through
   the BFF, backend-down banner via a second instance pointed at a dead
   port), per the reference app's `tests/`.

## Shared checklist (any archetype)

- Secrets: grep the scaffold for `cap_a2a_`, `token`, and the org id
  before committing; server tokens only in env, browser tokens only in
  localStorage, demo bootstrap tokens documented as demo-only.
- Honesty drill: kill the upstream (gateway or facade) and confirm the
  app shows its declared degraded state rather than stale-as-fresh or
  invented content.
- Idempotency drill (console): double-submit the form; the second
  submit must replay (`replayed: true`), not start a second run.
