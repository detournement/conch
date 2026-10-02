---
name: capitol-frontend
description: Build frontend apps on Capitol — A2A workflow consoles with live SSE run progress and HITL relay, workflow-fed pages served from filestore documents, and filestore portal apps behind a BFF. Use when the user asks to build, scaffold, wire, or debug a web app, UI, console, dashboard, portal, or page that starts Capitol workflow runs, streams run events, relays HITL questions, renders artifacts or deliverables, or reads filestore documents through the facade REST API. Covers pick the archetype → discover and describe the workflow with capitol_control → scaffold → wire the gateway or facade → verify live.
tools: local_shell, capitol_control
---

# Frontend apps on Capitol

You build web apps whose backend is Capitol: workflow runs over the A2A
gateway, documents over the filestore facade. This skill is the operating
procedure; the exact wire contracts (JSON-RPC envelope, gateway skill
catalog, SSE framing, HITL events, facade endpoints, env conventions)
live in `reference.md`, and complete worked scaffolds — including the
local reference apps to copy from — live in `cookbook.md` (both beside
this file; read reference.md before writing any client code).

## The three archetypes

Pick exactly one before scaffolding. Mixing them produces apps that leak
tokens or fabricate data.

1. **Workflow console** — the user fills an intake form, the app starts
   a run over A2A, streams node progress live (SSE with resume), relays
   HITL questions, and renders file deliverables as download rows.
   Static ES modules, no build step, no server code. The `cap_a2a_*`
   token lives in browser localStorage (local/demo deployments only —
   the gateway must be reachable from the browser).
2. **Workflow-fed page** — a Capitol workflow writes a JSON document to
   the filestore on a schedule; the app is a static page with a JSON
   data island that a tiny server function splices at serve time from
   the latest filestore document. The org token stays server-side.
   Read-only, cache + last-known-good + baked baseline fallback.
3. **Filestore portal** — a browsing/search UI over filestore records
   behind a BFF (backend-for-frontend): the browser talks only to the
   app's own `/api/*`; the BFF holds the org token, verifies sign-in,
   and reads the facade (live or via a synced snapshot).

Deciding: user input that *starts runs* ⇒ console. Page that *shows what
a scheduled workflow produced* ⇒ workflow-fed page. *Browse/search many
records* with sign-in ⇒ portal.

## The build arc

1. **Pin the data source.** Console: `capitol_control op='workflows'`
   then `op='describe'` on the chosen workflow — the intake form is
   generated from the described fields, and input keys are the
   discovered `"<node_instance_id>.<field_id>"` strings, nowhere else.
   Fed page / portal: confirm the org id, repo, and document path(s)
   with the user, and fetch one real document before writing render
   code — the render contract is the actual document shape.
2. **Scaffold from the cookbook recipe.** Each archetype has a complete
   recipe with file layout and the reference app to copy from. Static
   apps are dependency-free ES modules served over http (`python3 -m
   http.server` — modules refuse `file://`). Server pieces are single
   small functions (Vercel-style handler or equivalent).
3. **Wire the contracts from reference.md.** Never improvise request or
   event shapes from memory: the JSON-RPC envelope, the gateway skill
   payloads, the SSE frame parsing (artifact-tunneled events, keepalive
   skip, exclusive `sequence` resume cursor), and the facade paths are
   all specified there.
4. **Verify live before calling it done.** Console: fetch the agent
   card through the gateway with the bearer, serve the app, and drive
   one real (or user-approved) run end to end. Fed page / portal: curl
   the app's endpoint and confirm it serves the real document, then
   kill the upstream and confirm the declared fallback happens. An app
   that has never spoken to its gateway/facade is a draft, not a
   deliverable.

## Hard rules

- **Tokens never ship in client code or pages.** Server-side tokens
  (filestore org token, any `FILESTORE_ORG_TOKEN`) exist only in env
  vars read by server code. Browser-held A2A tokens live in
  localStorage via an injected getter — never a literal in a committed
  JS file. A `bootstrap_token` in a config file is acceptable only for
  a localhost-only demo and must be called out to the user as such.
- **Never invent ids.** Workflow ids come from `op='workflows'`, input
  keys from `op='describe'`, org/agent ids from the configured gateway
  URL, repo/paths from the user or an existing contract doc. A
  plausible-looking remembered UUID is wrong.
- **Effectful starts are keyed.** Every `call_workflow` carries an
  `idempotency_key`; a retry reuses the key and treats `replayed` as
  success. The form's submit handler must not mint a fresh key on
  re-click.
- **Relay HITL verbatim.** `node.input_required` events render the
  run's exact prompt to the user; the app submits their words via the
  matching response skill. Interventions advance only on the literal
  `continue`/`stop` tokens — map a button press to exactly one.
- **Fail closed, degrade honestly.** Missing required env ⇒ explicit
  misconfigured error, not fixture data. Upstream down ⇒ last-known-good
  with a visible staleness banner, or an error banner — never fabricated
  rows. Production never silently falls back to fixtures.
- **Reads from machine shapes, never prose.** Deliverables come from
  file events and the terminal output's `files[]`; status from event
  types and `get_workflow_status` — never parsed out of display text.
- **Data islands are escaped.** JSON spliced into HTML replaces `</`
  with `<\/` so document content can never terminate the script element.
- **Admin stays with the user.** Minting bearers, publishing workflows,
  schedules: the app never does these, and you point the user at
  `/capitol admin …` rather than working around it.

## Verification is part of the build

The cookbook's verify steps are not optional polish: a recipe ends with
the live checks (agent card reachable, one run driven, fallback drill)
and the scaffold ships only after they pass. If the local stack is down,
say so and stop — do not substitute mocked responses and declare success.
