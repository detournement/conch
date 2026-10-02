# Workflow console (Capitol A2A)

Static ES-module app: paste the agent token once, fill the form, the
app starts the workflow with an idempotency key derived from the form
contents, streams node progress live, relays any HITL prompt, and lists
the deliverables. No build step, no server code.

## Configure

Edit `config/app.config.json`:

- `gateway_url` — the agent's full `{base}/a2a/{org}/{agent}` URL.
- `workflow_id` — an id the agent allowlists (`capitol_control op=workflows`).
- `fields[]` — `key` is the described field key (`<node_instance_id>.<field_id>`),
  plus `label`, `type` (`textarea`|`text`|`number`), `required`, `default`, `help`.
- `idempotency_prefix` — namespace for the derived key (default `console`).

Nothing in this directory may contain a token. The token is pasted on
the setup screen and lives in this browser's localStorage only.

## Check the backend, then the app (Node 20+)

```
export CAPITOL_A2A_BEARER=cap_a2a_...        # from the user; never written to a file
bash preflight.sh                            # card + handshake + workflows → "preflight: PASS"
node verify-run.mjs --timeout 2400           # keyed run → terminal → deliverables → "verify: PASS"
node verify-run.mjs --input "<field key>=<other text>"   # different inputs ⇒ a new run
```

Running the verifier twice with the same inputs replays the same run
(same run id, `existing run — replayed`) — nothing new is started.

## Serve

```
python3 -m http.server 4310     # then open http://localhost:4310/
```

ES modules do not load from `file://`; always serve over http. The
console is for local/demo deployments where the gateway is reachable
from the browser.
