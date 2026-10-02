---
name: capitol-frontend
description: Build frontend apps on Capitol — A2A workflow consoles with live SSE run progress and HITL relay, workflow-fed pages served from filestore documents, and filestore portal apps behind a BFF. Use when the user asks to build, scaffold, wire, or debug a web app, UI, console, dashboard, portal, or page that starts Capitol workflow runs, streams run events, relays HITL questions, renders artifacts or deliverables, or reads filestore documents through the facade REST API. Covers pick the archetype → discover and describe the workflow with capitol_control → scaffold from the shipped templates → wire the gateway or facade → verify live headlessly.
tools: local_shell, capitol_control
rounds: 40
---

# Frontend apps on Capitol

You build web apps whose backend is Capitol: workflow runs over the A2A
gateway, documents over the filestore facade. This file is the
procedure; `reference.md` (beside it) holds the wire contracts and
`cookbook.md` each archetype's recipe with exact commands and expected
output. Known-good minimal apps live in `templates/<archetype>/` beside
this file (absolute path in the `[Skill assets: …]` line at the end).
**Copy them; never retype them or re-derive the wire handling.**

## Step 0 — pick exactly one archetype

| The user wants… | Archetype | Template |
|---|---|---|
| a form that *starts a workflow run*, shows progress + deliverables | **console** | `templates/console/` |
| a page showing *what a scheduled workflow published* (one document) | **fed-page** | `templates/fed-page/` |
| to *browse/search many records* behind sign-in | **portal** | `templates/portal/` |

Mixing archetypes leaks tokens or fabricates data. If the request is
ambiguous, ask one question, then pick.

## Step 1 — preflight (prove the backend is real before writing code)

Console — requires the `capitol_control` tool. If it is not in this
session, say so and stop; do not hand-roll gateway calls with curl.

1. `capitol_control {"op": "discover"}` → agent card (name, gateway URL,
   `capabilities.streaming`). Record the gateway URL.
2. `capitol_control {"op": "workflows"}` → allowlisted workflow ids. If
   the id the user named is not listed, stop and say so.
3. `capitol_control {"op": "describe", "workflow_id": "<id>"}` → field
   rows. Record each `fields[].key` you will use, verbatim
   (`"<node_instance_id>.<field_id>"`).

Fed page / portal — confirm `FILESTORE_BASE`, org id, repo, and the
document path or records prefix with the user (never guess), then fetch
one real document with the server-side token:

```
curl -sf -H "Authorization: Bearer $FILESTORE_ORG_TOKEN" \
  "$FILESTORE_BASE/v1/orgs/$ORG/repos/$REPO/files/<url-encoded path>" | head -c 400
```

That document's actual shape is the render contract.

## Step 2 — scaffold from the template

```
pwd                                   # the app goes under the user's cwd,
cp -R "<skill assets dir>/templates/<archetype>" ./<app-dir>   # never inside the skill or conch tree
```

Edit **only** the marked places (every placeholder starts with
`REPLACE_`): `config/app.config.json` (console: gateway URL, workflow
id, field keys from Step 1), `data/baseline.json` (fed page: a real copy
of the document), and the env vars in each template's README (fed page,
portal). Do not rewrite `js/a2a-client.js`, `js/run.js`, `api/index.js`
or `api/_shared.js`; extend them only for features the user asked for.

## Step 3 — verify headlessly (no browser needed)

Each template ships its verifier; run it and paste the output:

| Archetype | Command | Must print |
|---|---|---|
| console | `bash preflight.sh` then `CAPITOL_A2A_BEARER=… node verify-run.mjs` | `preflight: PASS`, then `terminal: success` … `verify: PASS` |
| fed-page | `node serve.mjs 3000 &` then `node verify-page.mjs http://localhost:3000/ --expect-source live` | `source: live` … `verify: PASS` |
| portal | `ALLOW_DEV_LOGIN=1 node serve.mjs 3000 &` then `node verify-portal.mjs --email <allowlisted>` | all `ok` rows … `verify: PASS` |

The console verifier derives the same idempotency key as the browser
app, so re-running it with the same inputs replays the existing run
instead of starting another — use that for repeated checks. A new run
costs real compute: start at most one, and only when the user agreed or
the workflow is a demo. Then do the cookbook's fallback drill (upstream
at a dead port ⇒ the declared degraded state).

## Step 4 — finish with the checklist

End your final message with this block, each line from real output
(write `not done — <why>` rather than guessing):

```
capitol-frontend checklist
- archetype: console | fed-page | portal
- source pinned: <workflow id + field keys | org/repo/path>
- scaffolded from template: <template path> → <app dir>
- preflight: <PASS line>
- verify: <PASS line, run id, deliverable count | source line | ok rows>
- fallback drill: <what you killed, what the app showed>
- secrets: grep -rE "cap_a2a_[A-Za-z0-9_-]{20,}" <app dir> → <0 hits>
- new runs started: <0 | 1 + run id>
```

## Hard rules

- **Tokens never ship in client code or pages.** Server tokens
  (`FILESTORE_ORG_TOKEN`, `SESSION_SECRET`) live only in env read by
  server code; browser A2A tokens live in localStorage via an injected
  getter, never a literal in a committed file. A `bootstrap_token` in
  config is localhost-demo-only and must be called out. Never read
  `~/.capitol-a2a/agents.yaml` or print a token to find one — ask the
  user to export `CAPITOL_A2A_BEARER`.
- **Never invent ids.** Workflow ids from `op='workflows'`, input keys
  from `op='describe'`, org/agent ids from the gateway URL, repo/paths
  from the user. A plausible remembered UUID is wrong.
- **Effectful starts are keyed.** Every `call_workflow` carries an
  `idempotency_key` derived from the form contents (`js/run.js` does
  this), never minted per click. A repeated key returns the stored
  original response — same `run_id`, no `replayed` flag, `status` maybe
  still `queued` — so always `get_workflow_status` after `call_workflow`.
- **Relay HITL verbatim.** `node.input_required` renders the exact
  prompt; the user's words go back via the matching response skill.
  Interventions advance only on the literal `continue`/`stop`.
- **Fail closed, degrade honestly.** Missing env ⇒ explicit misconfigured
  error, not fixtures. Upstream down ⇒ last-known-good with a visible
  staleness note or an error banner — never fabricated rows. Production
  never falls back to fixtures or dev sign-in.
- **Reads from machine shapes, never prose.** Deliverables from
  `workflow.files_available` events and the terminal output's `files[]`;
  status from event types and `get_workflow_status`.
- **Data islands are escaped.** Spliced JSON replaces `</` with `<\/`.
- **Admin stays with the user.** Minting bearers, publishing workflows,
  schedules: never from the app; point at `/capitol admin …`.
- **Use the tools you were given.** Discovery via `capitol_control`,
  the app via the template client. Never read conch's own source or
  site-packages to work around a missing tool — report the gap.

## Failure modes

| Symptom | Cause | Do this |
|---|---|---|
| no `capitol_control` in the session | works plugin missing or `capitol_base_url` unset | tell the user: `/install works`, set `capitol_base_url` (+ org, agent), restart conch; stop |
| agent card 401/403 | bad or expired bearer | ask for a current token (`CAPITOL_A2A_BEARER`); never search the filesystem |
| agent card unreachable | wrong gateway URL or stack down | fix the URL; if the stack is down, say so and stop — never mock |
| `-32008` on `call_workflow` | input keys ≠ described fields | re-run `op='describe'`, copy `fields[].key` verbatim |
| `-32429` on `call_workflow` | per-org concurrent-run cap | wait; do not retry in a loop |
| stream open, no events, run already finished | terminal frame not always replayed | status-first: `success` ⇒ `get_workflow_output` (the template does this) |
| `verify-run.mjs`: `setup: … placeholder` | `REPLACE_*` left in config | fill in Step 1 values |
| fed page `source: baseline` with facade up | wrong path/org/repo or `EXPECTED_*` mismatch | curl the document (Step 1); compare `id`/`schema` |
| portal `{"error":"misconfigured"}` | required env var missing | set it; refusing to serve is by design |
| ES modules fail to load | opened via `file://` | serve over http (`python3 -m http.server`, `node serve.mjs`) |

When the stack is down, the bearer is missing, the workflow is not
allowlisted, or `capitol_control` is absent: say exactly what is missing
and what the user should do, then stop. An app that has never spoken to
its real gateway/facade is a draft, not a deliverable.
