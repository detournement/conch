# Capitol frontend wire contracts

Document version 2026-10-02.2. Verified against: local Capitol stack,
A2A gateway wire schema `1.0.27` (agent card `version`), backend
`agentic-backend-guardrails` commit `678435ca`, conch adapter at edge
`d26eb10`+. Bump the version line whenever an observed wire fact below
changes; the drift test in `tests/test_shipped_skills.py` keeps the
skill ids here equal to the adapter's.

The contracts a frontend app speaks, as implemented by the gateway and
mirrored by conch's own adapter (`conch/capitol/client.py`) and the
browser client shipped with this skill
(`templates/console/js/a2a-client.js`). Where this document and those
implementations diverge, the implementations win — fix this file.

## The A2A gateway

- Endpoint: `{base}/a2a/{org_id}/{agent_id}` — JSON-RPC 2.0 over POST.
- Agent card (discovery): `GET {endpoint}/.well-known/agent-card.json`,
  authenticated. Card lists the agent's name, wire schema version,
  capability flags, and skill catalog — features absent from the card
  do not exist on that agent (fail closed).
- Auth: `Authorization: Bearer cap_a2a_…` on every request, including
  the card fetch. 401/403 means the token is missing/rotated: surface a
  re-auth screen; never retry in a loop.
- Methods: `message/send` (one request/response), `message/stream`
  (opens an SSE stream), `tasks/resubscribe` (resume a stream after a
  drop; older gateways without it answer JSON-RPC `-32601` — fall back
  to re-opening `message/stream` with the same cursor).

### Envelope

Every call wraps one skill payload in an A2A message data part:

```json
{
  "jsonrpc": "2.0",
  "id": "<uuid>",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "context_id": "<from handshake, once known>",
      "parts": [{"type": "data", "data": {"skill_id": "…"}}]
    }
  }
}
```

The response `result` is a Task; the skill's response data is the first
data part of `result.status.message.parts`. A JSON-RPC `error` object
may carry `data.retryable` and `data.actionable_hint`.

### Gateway skill catalog

| skill_id | request fields | response / notes |
|---|---|---|
| `handshake` | `caller {system, version}`, `capabilities {supports_sse}` | `session.context_id` — thread onto later messages |
| `list_workflows` | — | the agent's workflow allowlist; ids pass back verbatim |
| `get_workflow_details` | `workflow_id`, `version_id?` | fields list: `node_instance_id`, `field_id`, `valid_types`, `required`. Input key = `"{node_instance_id}.{field_id}"`. Also returns `version_id` (the saved version the schema came from — latest unless pinned) and `capitol_workflow_id` (the UUID behind a slug id) |
| `get_workflow_versions` | `workflow_id` | `{workflow_id, count, versions[{version_id, created_at}]}` newest first — the set a pin must belong to |
| `call_workflow` | `workflow_id?`, `inputs` (keyed map), `artifacts[]`, `idempotency_key`, `version_id?` (UUID — run exactly that saved version; default latest) | returns `{run_id, session_id, status: queued\|running, sub_agents[], total_sub_agents}`; same key within 24 h ⇒ the **stored original response** (same `run_id`, `status` still `queued`, no `replayed` flag — detect replay by run id and follow with `get_workflow_status`); same key + different inputs ⇒ `-32005 IdempotencyConflict` |
| `get_workflow_status` | `run_id` | terminal statuses: `success/failed/stopped/cancelled` |
| `get_workflow_output` | `run_id` | terminal-node outputs under `outputs` (keyed by output node id, text in `value`) plus `logical_outputs`; eval roll-up under `eval_rollup`. File deliverables ride `workflow.files_available` events (below); some gateways also expose a terminal `files[]` with `{id, name, presigned_url, mime_type}` — normalize both |
| `get_workflow_events` | `run_id`, `since_sequence`, `types[]` | polling fallback when streaming is unavailable |
| `subscribe_workflow_events` | `run_id`, `since_sequence`, `types[]` | sent via `message/stream` (see SSE below) |
| `upload_file` | top-level `filename`, `content_base64`, `content_type?` | inline ≤ 50 MB; returns `file_id` for workflow file inputs. NOT a nested artifact object |
| `request_upload_url` | name, size, content type | presigned PUT ≤ 500 MB; the PUT goes to storage with **no bearer** (the URL authenticates) |
| `submit_intervention_response` | `run_id`, `node_id`, `request_id`, `response` | continue/stop panels advance only on the literal `"continue"` |
| `submit_clarification_response` | `run_id`, `request_id`, `response`, `declined` | for `node.input_required` with `data.input_kind == "clarification"` |
| `suggest_workflows` | `goal` | ranked guesses; confirm with the user before starting |
| `pause_workflow` / `resume_workflow` | `run_id` (+ resume payload) | lifecycle; rarely frontend-driven |

### SSE streaming (`message/stream`)

EventSource cannot be used — the stream opens with a POST. Fetch with
`Accept: text/event-stream`, read the response body stream, split
frames on `\n\n`, and join the `data:` lines of each frame as JSON:

- The payload's `result.artifact` tunnels one WorkflowEvent in its
  `parts[*].data`. Frames without an artifact are TaskStatusUpdateEvent
  — skip. Artifact names containing `keepalive` — skip.
- Every event carries a numeric `sequence`. Track the max seen; it is
  an **exclusive** resume cursor (`since_sequence`), so resuming never
  re-delivers.
- Terminal events: `workflow.run_completed` / `workflow.run_failed`.
  A stream that ends without one was dropped: reconnect with
  `tasks/resubscribe` (exponential backoff, cap ~30 s), falling back to
  `message/stream` on `-32601`, falling back to `get_workflow_events`
  polling if streaming is unavailable entirely. The run keeps executing
  server-side across all of this.
- Status first. The stream backfills persisted events and then tails
  live; for a run that already finished the backfill does not always
  include the terminal frame, so the stream can sit open forever. Call
  `get_workflow_status` before subscribing (a keyed re-submit often
  returns a finished run) and periodically while the stream is open;
  a terminal status ends the wait regardless of the stream.
- Node progress: `node.node_started` / node-scoped events flip the
  pipeline display pending → running → done. File deliverables arrive
  as `workflow.files_available` events whose `data.files[]` rows carry
  `{file_id, filename, download_url (presigned), mime_type,
  size_bytes, node_id}`; dedupe by `file_id` across events and any
  terminal-output file rows.

### HITL events

`node.input_required` events carry `data.request_id`, `data.prompt`,
and `data.input_kind`. `input_kind == "clarification"` answers via
`submit_clarification_response`; otherwise it is an intervention
(continue/stop) answered via `submit_intervention_response` with the
node id from the event. Render the prompt verbatim; submit the user's
words verbatim.

## Workflow versions, authoring and the approval gate

Observed on the local stack (2026-10-02):

- Every save of a workflow is a new **version** (`get_workflow_versions`
  lists them newest first; `get_workflow_details.version_id` is the
  latest). `call_workflow` and `get_workflow_details` take an optional
  `version_id` to pin; without it the gateway uses the latest saved
  version. The shipped console sends the pin from
  `config.workflow_version_id` and folds it into the idempotency key.
- Workflow ids on the wire may be slugs (`market_research_sources_sought_analyzer`)
  or UUIDs; the admin API (`/api/v1/orgs/{org}/workflows/{id}/versions`)
  takes the UUID only — `capitol_workflow_id` from describe.
- Authoring is an **operator** surface, not a tool the model calls:
  - `/compile "<goal>"` → Architecture Card (stage graph, assets to
    create, schedules, HITL points, drill fixtures) as a kernel event;
    `/compile approve <id>` pins the card digest (local origin only);
    `/compile materialize <id>` drives `CapitolAdmin` in order —
    collections → `POST /api/v1/orgs/{org}/workflows` (one new version,
    the pin) → orchestrator agent + exact allowlist (bearer sunk into
    the user's registry, never shown) → schedules (disabled unless the
    card says) → pack + `materialization-lock.json` (workflow ids,
    version ids, payload digests, schedule ids) → acceptance drill (one
    real run) → supervising mission. Every mutation is keyed
    `compile:<id>:<step>` and carries a rollback ref; `/compile rollback
    <id>` replays those refs in reverse.
  - `/capitol admin persist|publish|rollback|schedule-add|…` is the same
    ledgered client for hand-written payloads (`--key` replays a prior
    idempotency key). `capitol_admin=true` is required; the policy
    registry can deny any `capitol.admin.<op>`.
  - The compile session itself gets `capitol_control` restricted to
    read ops; it designs, it never provisions or runs.
- Rollback semantics: a first persist's undo deletes the workflow;
  publish's undo re-persists with `publish_to_api=false` (same version
  lineage); created agents and schedules are deleted. After a rollback
  the app's preflight fails closed on the agent card — by design.

## The filestore facade

REST, org-scoped bearer (`FILESTORE_ORG_TOKEN`) — **server-side only**:

- `GET {FILESTORE_BASE}/v1/orgs/{org_id}/repos/{repo}/files/{path}` —
  one document's bytes.
- `GET {FILESTORE_BASE}/v1/orgs/{org_id}/repos/{repo}/tree?recursive=true&path={subpath}` —
  listing under a folder.
- Writes are workflow-side (the pipeline PUTs documents); frontend apps
  are read-only consumers unless the user explicitly says otherwise.

Validate documents before trusting them (parse + expected `id`/schema
field); an unusable live read falls back to last-known-good, then the
baked baseline, silently retrying next request.

## Environment conventions

| var | holds | lives |
|---|---|---|
| `CAPITOL_A2A_BEARER` | `cap_a2a_*` gateway token | the user's shell env (ask them to export it — never read it out of `~/.capitol-a2a/agents.yaml` or any config file yourself); browser consoles hold it in localStorage |
| `FILESTORE_BASE` | facade origin (e.g. `http://127.0.0.1:19700`, a tunnel URL, or `https://…/proxy/filestore`) | server env |
| `FILESTORE_ORG_TOKEN` | org filestore token | server env, marked sensitive in the host (e.g. `vercel env add … --sensitive`) |
| `FILESTORE_ORG_ID`, `FILESTORE_REPO`, `DOC_PATH` (fed page), `RECORDS_PREFIX` (portal) | document addressing — the names the shipped templates read | server env, required (fail closed) |
| `EXPECTED_ID`, `EXPECTED_SCHEMA` | fed page: values the fetched document must carry to be trusted | server env, optional |
| `ALLOWLIST`, `SESSION_SECRET`, `GOOGLE_OAUTH_CLIENT_ID`, `ALLOW_DEV_LOGIN` | portal sign-in (Google Identity + allowlist + HMAC session cookie; dev login is local-only and ignored in production) | server env |

Config values enter code via env reads with explicit fail-closed checks
— a missing required var is a 500 "misconfigured", never a fixture
fallback in production.

## Deployment profiles

- **Local static** (console against a localhost gateway): serve the
  directory with `python3 -m http.server <port>`; the gateway (e.g.
  `http://localhost:8300`) is reachable only from that machine, which
  is why the app runs locally.
- **Vercel static + function** (fed page / portal): static assets plus
  `api/*.js` serverless handlers, `vercel.json` for rewrites and
  security headers; env via `vercel env add` (tokens `--sensitive`);
  a cloudflared tunnel reaches a local-stack facade, with the caveat
  that quick tunnels rotate hostnames — snapshot reads
  (`DATA_SOURCE=kv`) keep the app serving when the tunnel is down.
- Zero runtime dependencies is the house style: hand-rolled A2A/SSE
  client, no framework, ES modules, one CSS file. Playwright for e2e
  where the app warrants tests.
