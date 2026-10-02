# Capitol frontend wire contracts

The contracts a frontend app speaks, as implemented by the gateway and
mirrored by conch's own adapter (`conch/capitol/client.py`) and the
browser reference client
(`~/composer/cap-app-acc-market-research/js/a2a-client.js`). Where this
document and those implementations diverge, the implementations win —
fix this file.

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
| `get_workflow_details` | `workflow_id` | fields list: `node_instance_id`, `field_id`, `valid_types`, `required`. Input key = `"{node_instance_id}.{field_id}"` |
| `call_workflow` | `workflow_id?`, `inputs` (keyed map), `artifacts[]`, `idempotency_key` | returns `run_id`; same key + same inputs ⇒ `replayed: true` (success); same key + different inputs ⇒ `-32005 IdempotencyConflict` |
| `get_workflow_status` | `run_id` | terminal statuses: `success/failed/stopped/cancelled` |
| `get_workflow_output` | `run_id` | terminal-node outputs; files as `files[]` with `{id, name, presigned_url, mime_type}`; eval roll-up under `eval_rollup` |
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
- Node progress: `node.node_started` / node-scoped events flip the
  pipeline display pending → running → done. File deliverable events
  carry `{file_id, filename, download_url, mime_type, size_bytes}`;
  dedupe against the terminal output's `files[]` by id.

### HITL events

`node.input_required` events carry `data.request_id`, `data.prompt`,
and `data.input_kind`. `input_kind == "clarification"` answers via
`submit_clarification_response`; otherwise it is an intervention
(continue/stop) answered via `submit_intervention_response` with the
node id from the event. Render the prompt verbatim; submit the user's
words verbatim.

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
| `CAPITOL_A2A_BEARER` (or the env named by conch's `capitol_bearer_env`) | `cap_a2a_*` gateway token | shell env / `~/.capitol-a2a/agents.yaml`; browser consoles hold it in localStorage |
| `FILESTORE_BASE` | facade origin (e.g. `http://127.0.0.1:19700`, a tunnel URL, or `https://…/proxy/filestore`) | server env |
| `FILESTORE_ORG_TOKEN` | org filestore token | server env, marked sensitive in the host (e.g. `vercel env add … --sensitive`) |
| `*_ORG_ID`, `*_REPO`, `*_DOC_PATH` / records path | document addressing | server env with safe defaults |
| `ALLOWLIST`, `SESSION_SECRET`, `GOOGLE_OAUTH_CLIENT_ID` | portal sign-in (Google Identity + allowlist + signed session cookie) | server env |

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
