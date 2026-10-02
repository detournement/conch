---
name: capitol-frontend
description: Build frontend apps on Capitol — A2A workflow consoles with live SSE run progress and HITL relay, workflow-fed pages served from filestore documents, and filestore portal apps behind a BFF — and author, version-pin and wire the Capitol workflows those apps lean on. Use when the user asks to build, scaffold, wire, or debug a web app, UI, console, dashboard, portal, or page that starts Capitol workflow runs, streams run events, relays HITL questions, renders artifacts or deliverables, or reads filestore documents through the facade REST API; or asks what apps can be built on Capitol; or needs a new workflow created for an app. Covers pick the archetype → discover and describe the workflow with capitol_control (or author one via /compile) → scaffold from the shipped templates → wire the gateway or facade → pin the workflow version → verify live headlessly.
tools: local_shell, capitol_control
rounds: 40
---

# Frontend apps on Capitol

Web apps whose backend is Capitol: runs over the A2A gateway, documents
over the filestore facade. This file is the procedure; `reference.md`
the wire contracts; `cookbook.md` the recipes, the **App catalog** and
the **authoring** recipe. Known-good apps live in `templates/<archetype>/`
beside this file (see `[Skill assets: …]`). **Copy them; never retype
them.**

## Step 0 — pick exactly one archetype

| The user wants… | Archetype | Template |
|---|---|---|
| a form that *starts a workflow run*, shows progress + deliverables | **console** | `templates/console/` |
| a page showing *what a scheduled workflow published* (one document) | **fed-page** | `templates/fed-page/` |
| to *browse/search many records* behind sign-in | **portal** | `templates/portal/` |

Mixing archetypes leaks tokens or fabricates data. Unsure what to
build? Offer `cookbook.md` § App catalog. Ambiguous? Ask.

## Step 1 — preflight (prove the backend is real before writing code)

Console — needs `capitol_control`; without it say so and stop (no
curl).

1. `capitol_control {"op": "discover"}` → agent card (name, gateway URL,
   `capabilities.streaming`). Record the gateway URL.
2. `capitol_control {"op": "workflows"}` → allowlisted ids. Named id not
   listed? Stop and say so — or Step 1b if the user wants it created.
3. `capitol_control {"op": "describe", "workflow_id": "<id>"}` → record
   each `fields[].key` verbatim (`"<node_instance_id>.<field_id>"`) and
   the `version_id` (the pin).

Fed page / portal — confirm `FILESTORE_BASE`, org id, repo, document
path or records prefix with the user (never guess); fetch one real
document with the server token:

```
curl -sf -H "Authorization: Bearer $FILESTORE_ORG_TOKEN" \
  "$FILESTORE_BASE/v1/orgs/$ORG/repos/$REPO/files/<url-encoded path>" | head -c 400
```

That document's shape is the render contract.

## Step 1b — no fitting workflow? Author one (the user approves)

Only allowlisted workflows exist for an app. When none fits, draft the
goal (input → agent → markdown/docx; schedule? notify? HITL?) and hand
the user these commands; approval is a user act at the shell
(`capitol_admin=true`):

```
/compile "<goal>"          → card id; /compile show <id> to review
/compile approve <id>      # the authorization moment
/compile materialize <id>  # workflow (version pin) + agent + allowlist + schedules + lock; drill run
/compile status <id>       # ids to wire; /compile rollback <id> undoes it all
```

Then re-run Step 1 against the new agent and copy `workflow_version_id`
from the pack's `materialization-lock.json` (`cookbook.md` § Authoring).

## Step 2 — scaffold from the template

```
pwd                                   # the app goes under the user's cwd,
cp -R "<skill assets dir>/templates/<archetype>" ./<app-dir>   # never inside the skill/conch tree
```

Edit **only** the marked places (placeholders start with `REPLACE_`):
`config/app.config.json` (console: gateway URL, workflow id,
`workflow_version_id`, field keys from Step 1), `data/baseline.json`
(fed page: a real copy of the document), env vars per the template
README (fed page, portal). Do not rewrite `js/a2a-client.js`,
`js/run.js`, `api/index.js`, `api/_shared.js`; extend on request only.

## Step 3 — verify headlessly (no browser needed)

Run the template's verifier and paste the output:

| Archetype | Command | Must print |
|---|---|---|
| console | `bash preflight.sh` then `CAPITOL_A2A_BEARER=… node verify-run.mjs` | `version: pinned … (current)`, `preflight: PASS`; `terminal: success` … `verify: PASS` |
| fed-page | `node serve.mjs 4320 &` then `node verify-page.mjs http://localhost:4320/ --expect-source live` | `source: live` … `verify: PASS` |
| portal | `ALLOW_DEV_LOGIN=1 node serve.mjs 4330 &` then `node verify-portal.mjs http://localhost:4330 --email <allowlisted>` | all `ok` rows … `verify: PASS` |

The console verifier derives the browser app's idempotency key, so the
same inputs replay the existing run. A new run costs real compute: at
most one, only if the user agreed or the workflow is a demo. Then the
cookbook's fallback drill (upstream at a dead port ⇒ declared state).

## Step 4 — finish with the checklist

End with this block, each line from real output (`not done — <why>`,
never guessed):

```
capitol-frontend checklist
- archetype: console | fed-page | portal
- source pinned: <workflow id + field keys | org/repo/path>
- workflow version: <pinned <id> (current|behind) | unpinned | n/a>
- scaffolded from template: <template path> → <app dir>
- preflight: <PASS line>
- verify: <PASS line, run id, deliverable count | source line | ok rows>
- fallback drill: <what you killed, what the app showed>
- secrets: grep -rE "cap_a2a_[A-Za-z0-9_-]{20,}" <app dir> → <0 hits>
- new runs started: <0 | 1 + run id>
```

## Hard rules

- **Tokens never ship in client code or pages.** Server tokens
  (`FILESTORE_ORG_TOKEN`, `SESSION_SECRET`) only in server env; browser
  A2A tokens only in localStorage via an injected getter. Never read
  `~/.capitol-a2a/agents.yaml`, `env`, or any config to find or check a
  token: a 401 is a stop, not a puzzle — one sentence to the user
  (export `CAPITOL_A2A_BEARER` for agent `<id>`), then end the turn.
- **Never invent ids.** Workflow ids from `op='workflows'`, keys and
  `version_id` from `op='describe'`, org/agent ids from the gateway URL,
  repo/paths from the user. A plausible remembered UUID is wrong.
- **Effectful starts are keyed and pinned.** `call_workflow` carries an
  `idempotency_key` derived from the form contents (+ pinned version)
  and the `version_id` the app was built against (`js/run.js` does
  both). A repeated key returns the stored original response — same
  `run_id`, no flag — so always `get_workflow_status` after it.
- **Relay HITL verbatim.** `node.input_required` renders the exact
  prompt; the answer goes back via the matching response skill;
  interventions advance only on the literal `continue`/`stop`.
- **Fail closed, degrade honestly.** Missing env ⇒ explicit misconfigured
  error, not fixtures. Upstream down ⇒ last-known-good with a visible
  staleness note or an error banner — never fabricated rows; production
  never uses fixtures or dev sign-in.
- **Machine shapes, never prose.** Deliverables from
  `workflow.files_available` and the output's `files[]`; status from
  event types and `get_workflow_status`. Data islands escape `</`.
- **Admin stays with the user.** Creating/publishing/scheduling
  workflows, minting bearers: never from the app or by curl — hand over
  `/compile …` / `/capitol admin …` (Step 1b).
- **Use the tools you were given.** Never read conch's source or
  site-packages to work around a missing tool; report the gap.
- **Platform facts: the docs tools** (`how_do_i`, `explain_concept`,
  skill `capitol-docs`), cited by doc id.

## Failure modes

| Symptom | Cause | Do this |
|---|---|---|
| no `capitol_control` | works plugin missing or `capitol_base_url` unset | user: `/install works`, set `capitol_base_url` (+ org, agent), restart; stop |
| agent card 401 `Bearer token does not match agent` | the exported bearer is another agent's | say which agent id needs its bearer exported; stop — no file/env reads |
| `-32008` on `call_workflow` | input keys ≠ described fields | re-run `op='describe'`; copy `fields[].key` verbatim |
| `version: … (behind)` | workflow re-saved since the build | run as pinned; offer the cookbook's Upgrading steps — never move the pin silently |
| fed page `source: baseline`, facade up | wrong path/org/repo or `EXPECTED_*` | curl the document (Step 1); compare `id`/`schema` |
| portal `{"error":"misconfigured"}` | required env var missing | set it; refusing to serve is the design |

Stack down, bearer missing, workflow not allowlisted (and not to be
authored), or `capitol_control` absent: say what is missing and what
the user should do, then stop. An app that never spoke to its real
gateway/facade is a draft, not a deliverable.
