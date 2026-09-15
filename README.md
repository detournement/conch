# Conch

Conch is an LLM-assisted shell with two interfaces:

- **`conch-ask`** / **`ask`** — one-shot command generation
- **`conch`** / **`conch-chat`** — multi-turn chat with MCP tools, memory, and scheduling

## Install

### One line (macOS & Linux)

```bash
curl -fsSL https://conch.usecapitol.ai/install | sh
```

Installs the latest conch into an isolated environment (uv → pipx →
venv, whichever is available), puts `conch` on your PATH for zsh and
bash, and never needs sudo. Re-running upgrades; `sh -s -- --uninstall`
removes it. Windows is not supported (WSL2 works via the Linux path).
On first launch conch walks you through picking a provider and storing
an API key (`0600` in `~/.config/conch/env`).

### From source (development)

```bash
git clone https://github.com/detournement/conch.git
cd conch
./install.sh
```

`install.sh` is the from-source developer installer (repo checkout,
shell aliases); the one-liner above is the end-user path.

## Configuration

On a first interactive launch with no configuration anywhere, `conch`
runs a short setup wizard: pick a provider (Ollama is auto-detected at
its local port), paste an API key (hidden input, stored `0600` in
`~/.config/conch/env`, loaded by conch itself — daemons see it too),
optionally verify it live, and start chatting. Set `CONCH_NO_WIZARD=1`
to suppress it; non-interactive contexts never see it.

Conch reads config from `~/.config/conch/config`, then `~/.conchrc`, then a
per-project `.conchrc` (nearest file between the current directory and the
git root — later files override earlier ones):

```ini
provider=anthropic
model=claude-sonnet-5
chat_model=claude-sonnet-5
api_key_env=ANTHROPIC_API_KEY
```

Or for a local Ollama server:

```ini
provider=ollama
ollama_base_url=http://192.0.2.247:11434
model=qwen3.6:27b
chat_model=qwen3.6:27b
local_only=true
```

Switch providers at any time in chat with `/provider openai`, `/provider anthropic`, or `/provider ollama`.
For containers and automation, the same settings can be supplied as
`CONCH_PROVIDER`, `CONCH_MODEL`, `CONCH_OLLAMA_BASE_URL`,
`CONCH_CUSTOM_BASE_URL`, `CONCH_LOCAL_ONLY`, and
`CONCH_SSH_CONTROL_PERSIST`; environment variables take
precedence over config files. `OLLAMA_HOST` remains supported by Conch and
other Ollama clients. `local_only` rejects public inference endpoints and
isolates provider fallback/switches; it is not a network sandbox for tools
you explicitly configure or approve.

### Config keys

| Key | Default | Description |
|-----|---------|-------------|
| `provider` | `anthropic` | `anthropic`, `openai`, `cerebras`, `bedrock`, `openrouter`, `ollama`, or `custom` |
| `model` / `chat_model` | per provider | Model for ask / chat mode |
| `api_key_env` | per provider | Env var holding the API key |
| `agent_mode` | `false` | Auto-execute shell commands without confirmation (a startup notice is shown when enabled from config) |
| `base_url` / `ollama_base_url` | `http://localhost:11434` | Ollama server URL (`OLLAMA_HOST` also works) |
| `local_only` | `auto` | Prevent cloud provider fallback/switches; `auto` enables this for Ollama/custom sessions |
| `detect_location` | `false` | Opt in to public-IP geolocation at startup |
| `ollama_num_ctx` | server-managed | Optional explicit Ollama context override, clamped to the model maximum |
| `ollama_context_window` | `4096` before load | Conservative accounting fallback until `/api/ps` reports the loaded context |
| `ollama_temperature` | `0.2` | Local chat sampling temperature |
| `ollama_num_predict` | server default | Optional output-token limit |
| `ollama_think` | server default | Optional `true`, `false`, or supported thinking level |
| `ollama_keep_alive` | `10m` | How long Ollama keeps the model loaded between turns |
| `tool_profile` | — | Tool profile to apply for the session (see profiles below) |
| `profile_<name>` | — | Define a custom tool profile, e.g. `profile_research = github, jira` |
| `chat_prompt:<provider>/<model-glob>` | — | Path to a custom chat system-prompt template for matching models |
| `ask_prompt:<provider>/<model-glob>` | — | Same for ask mode |
| `permission_mode` | `prompt_all` | Shell approval policy: `prompt_all`, `safe_auto`, or `yolo` |
| `allow_prefixes` | — | Comma-separated command prefixes that never prompt, e.g. `git status, ls` |
| `ssh_control_persist` | `600` | OpenSSH ControlMaster persistence in seconds (clamped to 1–86400) |
| `hook_pre_tool_use` | — | Shell script gating every tool call (JSON on stdin; non-zero exit blocks) |
| `hook_post_tool_use` / `hook_on_turn_end` | — | Scripts receiving tool results / the final reply |
| `custom_base_url` / `custom_model` | — | OpenAI-compatible `/v1` endpoint for `provider=custom` (vLLM, LM Studio, llama.cpp) |
| `custom_context_window` | discovered / `32768` | Optional safe upper bound; llama.cpp is probed via `/v1/props`, then `/props` |
| `custom_temperature` / `custom_max_tokens` | `0.2` / automatic | Local OpenAI-compatible generation settings |
| `subagent_model` / `subagent_rounds` | parent model / `10` | Model and tool-round budget for `delegate_task` subagents (a skill's `model`/`rounds` override these) |
| `remote_enabled` | `false` | Start the remote loop (channel polling + replies) |
| `remote_host` | `daemon` | Who polls channels when `edge_daemon` is on: the daemon (default) or `shell`; a kernel lease enforces exactly one consumer either way |
| `notify_channel` | — | Channel for scheduled-task output and notifications: `slack`, `sms`, or `email` |
| `remote_poll_interval` / `remote_rounds` | `60` / `8` | Inbound poll cadence (seconds) and tool-round cap for remote turns |
| `slack_channel` / `slack_allowed_senders` | — | Slack channel id + allowlisted user ids (token: `SLACK_BOT_TOKEN`) |
| `twilio_from` / `sms_to` / `sms_allowed_senders` | — | Twilio number, default recipient, allowlisted phone numbers (`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`) |
| `email_address` / `email_to` / `email_smtp_host` / `email_imap_host` / `email_allowed_senders` | — | Email gateway (password: `EMAIL_PASSWORD`; optional `*_port` keys) |
| `turn_token_budget` | off | Per-turn token cap; on exhaustion the model summarizes progress |
| `weak_model` / `weak_provider` | — | Small fast model for side tasks; cloud weak providers are disabled in local-only sessions |
| `repo_map` | `true` | Inject a ~1k-token repository map when cwd is a git repo |
| `send_cwd`, `send_os_shell`, `send_history_count` | — | Extra context sent in ask mode |

### Supported providers

Conch requires tool-calling-capable models on every provider; non-tool models
are rejected.

| Provider | Models | Cost |
|----------|--------|------|
| Cerebras | gpt-oss-120b, gemma-4-31b, zai-glm-4.7 (catalog unverified in the 2026-09-14 audit: no key available — re-audit before relying on it) | Paid / free tier |
| OpenAI | gpt-5.6-sol/terra/luna, gpt-5.5, gpt-5.4 family, gpt-5-mini/nano, gpt-4.1 family, gpt-4o, gpt-4o-mini, o3, o3-mini, o4-mini, o1 (all verified tool-capable 2026-09-14; o1-mini is not supported, and gpt-5.3-codex, gpt-5.4-pro, o3-pro, o1-pro are v1/responses-only so conch cannot use them) | Paid |
| Anthropic | claude-opus-5, claude-sonnet-5, claude-opus-4-8, claude-opus-4-7, claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5, claude-sonnet-4-5-20250929 (all verified tool-capable 2026-09-14) | Paid |
| Bedrock (AWS) | moonshotai.kimi-k2.5, moonshot.kimi-k2-thinking via Bedrock's OpenAI-compatible endpoint; auth is a long-term Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` (region via `bedrock_region`, default us-east-2) | Paid (AWS) |
| OpenRouter | moonshotai/kimi-k3 (2.8T MoE, 1M context, $3/$15 per MTok), z-ai/glm-5.2 (~750B MoE, 1M context, $0.98/$3.08 per MTok), deepseek/deepseek-v4-pro and deepseek/deepseek-v4-flash (1M context); key in `OPENROUTER_API_KEY` | Paid |
| Ollama | Discovered live from your server's `/api/tags`, filtered to models that advertise the `tools` capability | Free (local) |
| Custom | Models discovered from an OpenAI-compatible `/v1/models` endpoint (vLLM, LM Studio, llama.cpp); each visible model must pass a forced native tool-call probe | Depends |
| llama-idx | The whole self-hosted fleet discovered from one [llama-idx](https://github.com/detournement/llama-idx) registry (`llamaidx_url`); entries route through the ollama/custom adapters by flavor | Free (local) |

Ollama models are never hardcoded: `/models`, `/model`, `/provider ollama`,
and the fallback chain all use the live list from your server, and switching
to a model that isn't installed or doesn't support tool calling is rejected
with a clear message. Cloud providers are validated too: `/model` (and the
model-facing `conch_config` switches) reject names that aren't in the
provider's catalog and suggest close matches for typos ("Did you mean:
claude-sonnet-4-6?"). Validation cannot be bypassed with `--force`.
An unknown cloud model in a config file is replaced with that provider's
curated default instead of being sent optimistically.
Ollama capability results are cached by model digest, so replacing a tag
causes revalidation. Missing capability metadata fails closed: older servers
that cannot positively report native tool support expose no selectable
models. If a local service is unavailable, its models are not shown and
requests are blocked without contaminating conversation history.

#### llama-idx registry discovery

When the fleet is more than one box, per-box configuration stops scaling:
[llama-idx](https://github.com/detournement/llama-idx) is the
owned-and-operated registry that every llama.cpp/Ollama/OpenAI-compatible
server self-registers with (one curl), and conch discovers them all from
one endpoint:

```
# ~/.config/conch/config
llamaidx_url=http://registry.example:8642
```

- `/models` grows an **llamaidx** section with namespaced entries
  (`llamaidx/gpubox/qwen3-32b`, with context size); `/model
  llamaidx/gpubox/qwen3-32b` switches to one. Routing goes through the
  **existing** adapters by flavor — ollama flavor via the ollama adapter
  (`ollama_base_url` pointed at the box), llama.cpp/OpenAI-compatible via
  the custom adapter — the registry only supplies flavor + base_url +
  model id.
- Only the registry's **tool-verified** models are listed (it runs the
  same forced tool-call probe conch uses, cached by model identity), and
  conch still runs its own probe-on-select before committing — belt and
  braces; a stale registry verdict is caught by the component that
  actually talks to the model.
- Down providers vanish from lists; degraded (loading/recovering)
  providers stay visible with a marker. Fallback chains gain a registry
  tier: other up, tool-verified boxes are tried before any cloud hop.
- `local_only` still applies to the registry URL and to every discovered
  provider's base_url (tailnet `.ts.net` names and CGNAT 100.64/10
  addresses count as local). A registry cannot override the policy.
- Registries with `read_auth=true`: set `llamaidx_token_env` to the NAME
  of the env var holding the read token. Unset `llamaidx_url` = feature
  off, zero new traffic.

#### Auditing the cloud catalogs

The cloud model catalogs are hardcoded and go stale as providers rename and
decommission models. Re-run the live audit periodically (quarterly, or before
a release):

```bash
python tools/audit_models.py                     # every provider
python tools/audit_models.py --provider openai   # one provider
python tools/audit_models.py --prune-suggestions # entries to remove
```

For each cataloged model it checks existence against the provider's
model-list endpoint and then runs one minimal forced-tool-call probe (tiny
request, capped output tokens, one retry on transients) to prove native tool
calling — the same bar Ollama and custom endpoints are held to at runtime.
API keys are used by reference from the standard env vars and never printed;
providers without a key are reported as unverifiable rather than silently
blessed. After pruning or adding models, update the `MODEL_VERIFIED`
annotations in `conch/providers.py` with the audit date — the test suite
fails on any catalog entry without one. Note the annotation's limit:
verification confirms the model exists and answers a forced tool call on
the *auditing* account — per-account entitlement can still differ, and a
key without access to a cataloged model simply falls through the provider
fallback chain at runtime (which reports the degradation clearly).

## Features

### Streaming replies
Tokens stream to the terminal in real time with syntax-highlighted code blocks
(via Pygments) and inline markdown formatting. Native tool calls are
accumulated across streaming chunks, including fragmented OpenAI-compatible
arguments and Ollama calls emitted before the final chunk. If a model writes a
tool call as JSON/XML/prose, Conch may suppress that malformed payload from the
live display, but it never executes it; the turn returns a protocol error.

### MCP tools
Connect external tools via the [Model Context Protocol](https://modelcontextprotocol.io). Configure servers in `~/.config/conch/mcp.json`. Supports both stdio and HTTP transports.

### Local shell execution
The LLM can run shell commands on your machine. In normal mode, each command shows a prompt: **y**/Enter to run, **n** to decline (with optional feedback), **e** to edit the command first, **a** to always-allow commands with the same prefix for the session (`git status`, `docker ps`, …). Toggle `/agent` (or `/yolo`) for auto-execution. Command output streams live to your terminal. This captured path has no interactive stdin or controlling TTY, so a password prompt cannot be read accidentally.

### Passwords and direct terminal handoff
Use `/terminal <command>` (or the model-facing `interactive_terminal` tool) whenever `sudo`, `getpass`, SSH, a key passphrase, or another program may request private input:

```bash
/terminal sudo -k systemctl status my-service
```

Conch first shows the exact non-secret command and requires an explicit **y** confirmation even in agent/yolo mode. Queued or pasted input from before approval is discarded. After the `[Conch terminal handoff]` banner, the child inherits the real terminal directly. Type the password or passphrase only at the requesting program's prompt; that program disables echo when appropriate. Conch does not read, pipe, capture, log, or return any input or output from the handoff. Finish the program normally or press Ctrl+C to return to Conch. Never place a credential in the slash command, chat message, command argument, or environment variable; insecure forwarding forms such as `sshpass` and `sudo -S` are rejected.

Interactive handoff is available only in the local foreground TTY. Scheduled tasks, delegated noninteractive sessions, and Slack/SMS/email channel sessions cannot trigger it.

### Remote SSH operation
`/ssh` manages OpenSSH ControlMaster connections without storing a password:

```bash
/ssh connect user@192.0.2.152
/ssh status
/ssh exec curl -fsS http://127.0.0.1:8080/v1/models
/ssh shell sudo systemctl status llama-server
/ssh disconnect
```

`connect` performs an explicit direct terminal handoff, so OpenSSH can show its normal host-key, password, or key-passphrase prompt. Host-key verification remains at the user's OpenSSH default, and normal `~/.ssh/config` and `known_hosts` handling apply. Conch passes no password option and never sees the response.

After authentication, `exec` reuses the control socket in BatchMode with no TTY. Its output is bounded and returned to the model, and the remote command still passes Conch's permission modes, command-prefix allowlist, lifecycle hooks, timeout, and result budget. Use `shell` for a login shell or a command that genuinely needs a TTY (especially remote `sudo`); it again requires explicit confirmation and returns no transcript. `disconnect` closes the master. Conch also closes masters it created on normal/best-effort process exit. Control sockets use randomized, per-manager paths in a Conch runtime directory forced to mode `0700`; cleanup removes only sockets reserved and tracked by that manager.

SSH user, host/alias, and port fields are strictly validated and passed after OpenSSH's option terminator, so a target cannot inject SSH options. Interactive SSH tools are not exposed to remote channel loops.

### Graded permissions
Three approval modes via `permission_mode`: `prompt_all` (default — every command prompts), `safe_auto` (read-only commands like `ls`, `cat`, `git status` auto-approve; anything mutating prompts), and `yolo` (everything auto-executes; same as agent mode). Destructive commands — `rm`, `dd`, `mkfs`, `git push --force`, `git reset --hard`, and friends — always prompt for confirmation, **even in agent/yolo mode**, and are refused outright in non-interactive (scheduled) runs. Pre-seed trusted prefixes with `allow_prefixes=git status, ls`.

### Lifecycle hooks
Deterministic gates around the agent loop, configured as shell scripts: `hook_pre_tool_use` runs before every tool call (payload as JSON on stdin; non-zero exit blocks the call and the model sees why; stdout that parses as JSON rewrites the tool arguments), `hook_post_tool_use` receives each result, and `hook_on_turn_end` receives the final reply. Broken or missing hooks never brick the loop.

### Executable tools
Drop any executable into `~/.config/conch/tools/` and it becomes a tool: `<exe> --schema` must print `{"name", "description", "parameters"}` JSON; invocations pass the arguments as JSON on stdin and stdout becomes the tool result (budget-truncated like everything else). Grouped as `user` for profile filtering — far lighter than writing an MCP server for one-off tools.

### Plan tracking
The model keeps itself on track with the `todo_list` tool: its current plan is re-injected into every round *outside* compactable history, so long tool sequences and auto-compaction never lose the thread.

### Subagent delegation
The `delegate_task` tool runs a self-contained subtask in a fresh context with a narrowed toolset (no recursive delegation, no config access) and its own round budget, returning only a summary to the parent — big explorations stop polluting the main context. Subagents run strictly serially (one local GPU), default to the parent's model, and can use a configured `subagent_model`. Pass `skill=<name>` for a **skill-scoped subagent**: it gets that skill's instructions as its system prompt, only the skill's allowed tools, and the skill's model preference and round budget.

### Skills
Reusable procedures live in `~/.config/conch/skills/` — one markdown file per skill with frontmatter (`name`, `description`, `tools`, optional `model` and `rounds`) and a body of instructions. Where a custom slash command is a one-shot prompt template, a skill also scopes *tools* and *model*, and the model can invoke it itself: available skills are listed in the system prompt and loaded on demand with the `skill_manage` tool. Use `/skills` to list, `/skill <name> [task]` to run one on a task, or `delegate_task(skill=...)` for an isolated run. The **in-chat skill builder** closes the loop: ask conch to "turn what we just did into a skill" and it drafts the file (steps, commands, pitfalls, verification) and saves it via `skill_manage` — after showing you the file and getting a y/n confirmation, never silently.

### Remote loop (Matrix, Slack, SMS, email)
With `remote_enabled=true`, conch messages you proactively and you can steer it from anywhere: scheduled task output is delivered over your `notify_channel`, and inbound replies are polled (Matrix /sync long-poll, Slack bot channel, Twilio SMS, IMAP inbox) and routed into conversations — a channel thread *is* a conch conversation, so replies resume it. Safety is enforced in code, not prompts: inbound senders must be on a per-channel allowlist (no allowlist = no inbound, fail closed); remote sessions are capped at **safe_auto** permissions regardless of local agent mode; and remote sessions never see self-management, delegation, direct-terminal, or SSH-control tools. Mutating local commands create short-lived approvals bound to the exact channel, sender, thread, command, and timeout. Approval execution reruns lifecycle hooks, and approvals cannot be replayed from another conversation. Channel approvals can never produce a local password/passphrase prompt.

Slack photo attachments are captured too: message `files[]` from allowlisted senders are downloaded with the bot bearer, validated by content (JPEG/PNG/GIF/WebP magic bytes, 12 MB cap, ≤12 per message), and quarantined under the XDG state dir before any flow may use them. This needs the **`files:read`** bot scope on the Slack app (plus the usual `chat:write` and `channels:history`/`groups:history`/`im:history` for the channel type) — add it to the app manifest and reinstall the app. Because Slack's `conversations.history` never returns replies inside threads, conch also remembers each thread it posts into and polls `conversations.replies` for those, so a thread can carry a full back-and-forth.

**Who hosts the loop:** with `edge_daemon` enabled the `conch-edge` daemon
hosts channel intake by default, so inbound messages are answered 24/7
with no shell attached — same allowlists, same safe_auto cap, same
excluded tools, same approval binding. Set `remote_host = shell` to keep
the loop in the interactive shell instead. Either way, exactly one process
polls: every would-be host must hold the kernel `channel_intake` lease for
each pass (cursors advance only under the lease), so messages are never
double-answered, and a graceful stop releases the lease for immediate
handoff — pending messages simply wait on the transport for the next
holder. Messages of the form `input msn-<id> <your text>` from an
allowlisted sender are delivered to that mission through the kernel inbox
and wake it immediately (a parked `waiting_input`/`waiting_timer` mission
runs its next session at the next scheduler slot — no timer wait). Local
processes can post arbitrary events the same way through the control
socket's `event.post` op.

### Phone: sovereign setup (Matrix + ntfy)
The sovereign phone-interaction layer: conversation over **Matrix** on
your own homeserver, interrupts over **ntfy** on your own push server —
no Slack workspace, no Twilio account, no third party in the message
path. Both adapters are stdlib-only (no new Python dependencies).

**What rides where.** The Matrix room carries the conversation: your
messages get full (safe_auto-capped) agent turns, an Element thread maps
to one conch conversation (`thread_id` = room + thread root), image
attachments from allowlisted senders are quarantined with the same
content-sniffing caps as Slack, and `approve N` / `deny N` consume
origin-bound approvals — the origin is the exact (matrix, room/thread,
sender) triple, so an approve from the wrong thread or the wrong user id
does nothing. ntfy carries interrupts: with `notify_push = ntfy`,
approval requests, mission digests, and milestones are POSTed to your
topic as real push notifications (title/priority/tags), and tapping one
deep-links into the Element room (`ntfy_click`, defaulting to a
matrix.to link). Every remote-safety invariant is unchanged for Matrix
sessions: fail-closed allowlists of **full** Matrix user IDs
(`@user:server` — exact match, so lookalike homeservers fail), the
safe_auto cap, excluded tools, bounded replies, and the daemon's
`channel_intake` lease covering the Matrix poller.

**Setup.** `deploy/docker-compose.phone.yml` runs
[Conduit](https://conduit.rs) (a lightweight Matrix homeserver) and
[ntfy](https://ntfy.sh), both pinned by image digest, with named
volumes, federation off, and everything bound to 127.0.0.1 (set
`PHONE_BIND_ADDR` to your tailnet IP to reach it from the phone — never
a public port). Then `deploy/phone-bootstrap.sh` registers your user and
the `conch` user, mints conch's access token into a 0600 env file **by
reference** (`matrix_token_env` names the variable; the token never
enters conch config or logs), creates the DM room, and prints the
config block plus the Element and ntfy-app steps. Config surface:
`matrix_homeserver`, `matrix_room`, `matrix_allowed_senders`,
`matrix_token_env`, `matrix_user`, `matrix_sync_timeout_ms` (the /sync
long-poll window — the server parameter *is* the poll loop's block), and
`ntfy_url` / `ntfy_topic` / `ntfy_token_env` / `ntfy_priority` /
`ntfy_click` with `notify_push = ntfy` routing.

**E2EE, honestly.** The Python standard library cannot do Olm/Megolm,
and conch does not pretend otherwise. v1 gives you two documented modes:

- **Unencrypted room on your own homeserver (the simple default).** The
  transport is TLS (or your tailnet); messages sit in plaintext only in
  Conduit's database on your own box. That is sovereign — the exposure
  is "someone who already owns your server can read your chat with the
  agent that runs on the same server."
- **pantalaimon (the E2EE path).** A self-hosted proxy daemon that
  transparently handles encryption; conch just points
  `matrix_homeserver` at it (default `http://localhost:8009`) and gains
  E2EE rooms with zero code changes. Honesty required here too: the
  upstream [matrix-org/pantalaimon](https://github.com/matrix-org/pantalaimon)
  repository is **archived** (last release 0.10.5, September 2022, still
  built on the deprecated libolm). It still works against current
  client-server v3 endpoints and is still the standard answer for
  E2EE-unaware bots, but treat it as frozen software: pin what you
  deploy, and know that a community Rust successor (pantalaimon-rs,
  vodozemac-based) exists but is very young. If E2EE-at-rest matters
  more to you than software freshness, run pantalaimon; if you would
  rather not depend on archived crypto code, use the unencrypted-room
  mode knowingly.

**Push metadata note.** Element's default push runs through Google/Apple
gateways and Matrix's push gateway — metadata (not content) leaves your
box. For fully sovereign notifications on Android, use Element with
**UnifiedPush** and let your own ntfy server be the distributor; on iOS
there is no UnifiedPush, which is exactly why conch's interrupt path
posts to ntfy directly (the ntfy iOS app polls your server). You can
also mute Element notifications entirely and rely on the ntfy topic.

**Named upgrade paths (deliberately not built in v1):**
- *One-tap approve.* ntfy action buttons could carry an "Approve"
  HTTP action, but that needs an authenticated endpoint conch does not
  have — the upgrade path is a **tailnet-only listener** (bound to the
  tailscale interface, token-checked, origin-verified against the
  pending approval), never a public endpoint. Until then, approving is
  typing `approve N` in the Matrix room — one line, same phone.
- *UnifiedPush for Element itself* (Android): point Element at your ntfy
  server as the UnifiedPush distributor so even conversation
  notifications never touch Google's gateway.

### Capitol as a governed execution fabric
Conch drives
[Capitol](https://capitol.ai) — A2A orchestrator agents and durable Temporal
workflows — as a subordinate, governed process-execution fabric while mission
truth, policy, budgets, approvals, and the external-action ledger stay on the
Conch controller. The adapter (`conch/capitol/`) is stdlib-only (HTTP + a
hand-rolled SSE reader, no CLI on the control path) and mirrors the
[A2Actrl](https://github.com/Faction-V/A2Actrl) reference client's wire
behavior; the adapter is verified differentially against the `a2actrl` CLI
during development.

**Durable mission supervision.** A mission may carry a `capitol` authority
envelope — an allowlist of workflow ids plus `allow_start` / `allow_respond` /
`max_runs` — and its bounded `capitol_control` tool derives every grant from
that envelope *and* a fail-closed required-policy check, never from the model's
prompt. The edge daemon supervises bound runs on its own cadence: it advances
a persisted event cursor, wakes parked missions on terminal/failure, maps each
Human-in-the-Loop checkpoint into an origin-bound, TTL'd kernel approval whose
decision flows back as the exact HITL reply exactly once, and maps a mission
abort onto the run's cancel. If Capitol is unreachable the binding degrades
with exponential backoff and the mission parks — it is never failed and no
alternate provider is substituted — and supervision resumes from the persisted
cursor when the platform returns. Configure `capitol_base_url` / `capitol_org`
/ `capitol_agent` / `capitol_bearer_env` (the bearer is read from that env var
or `~/.capitol-a2a/agents.yaml`, never stored in Conch config) plus
`capitol_poll_seconds` and `capitol_hitl_ttl_seconds`.

**Bounded provisioning (builder profile).** For pre-authorized production
creation, `CapitolAdmin` is a separately-authorized surface over Capitol's
management APIs — create orchestrator agents, publish/pin workflow versions,
manage an agent's workflow allowlist, bind collections, and create/update
schedules. It is **off by default** (`capitol_admin=true` to enable, plus
`capitol_platform_url`) and every mutation additionally passes a fail-closed
required-policy check. Each mutation records a caller idempotency key as a
kernel external-action ledger entry *before* the wire call (a committed
duplicate replays the recorded outcome instead of acting again), captures a
version pin and rollback reference, and reconciles transport-uncertain
outcomes by query rather than blind retry. A minted or rotated agent bearer is
written straight into the A2Actrl registry (0600) under a name-only reference;
callers, the ledger, and logs only ever see a fingerprint. The admin token
(a platform user JWT) is resolved per call from `CAPITOL_ADMIN_TOKEN` or a
registry `x_user_token` and is scrubbed from every error.

**Ask for Capitol things in plain language.** With Capitol configured, every
chat session carries the model-callable `capitol_control` tool, so "backfill
funding intake for the first week of September", "how's that run doing?",
"answer its question with …", or "grab the packet docx" just work: the model
discovers the agent card and workflow catalog (it never invents workflow ids),
describes inputs before starting, starts runs under a required idempotency key
(derived from workflow+inputs when omitted, so a retried ask replays instead
of double-running), watches to a hard deadline and summarizes events —
relaying any Human-in-the-Loop question verbatim for you to answer — and
fetches outputs, eval roll-ups, and artifacts (quarantine-dir paths only, with
digests to verify). It is a runtime surface only: every admin/provisioning op
and pack mutation is refused with the exact user-explicit `/capitol admin …` /
`/capitol pack …` command named. Authority follows the session: interactive
sessions get the full runtime surface; over Slack/SMS reads and HITL answers
work while an effectful start becomes an origin-bound "approve N" in the
thread (the exact workflow/inputs/key are pinned at propose time); delegated
sub-turns and fleet workers only see the tool when a skill or task envelope
names it; mission sessions keep their envelope-scoped variant. Two shipped
skills back this up (`conch/skills_data/`, discoverable via `/skills` and
`skill_manage` like user skills): **capitol** — the operating procedure,
op reference, and cookbook recipes for this machine's real workflows — and
**pack-author** — flow-pack anatomy, the implemented `conch.flow_pack.v1`
grammar, and the authoring loop ending in `/capitol pack verify`.

Dev-time live and differential tests exercise all of this against a local
Capitol stack behind `CONCH_CAPITOL_LIVE=1` (they skip cleanly when the stack,
CLI, or token is absent); see `tests/test_capitol_live.py` and
`tests/test_capitol_crossclient.py`.

**The ProcessCompiler (`/compile`).** A stated goal becomes governed,
operating infrastructure through a reviewed compilation step. `/compile
"<goal>"` runs a bounded design session (the capitol + pack-author skills
loaded, `capitol_control` restricted to read/discovery — the session designs,
it never provisions) that discovers what already exists and emits a versioned
**Architecture Card**: goal and success criteria, process narrative,
reuse-vs-create asset lists (created workflows as declarative stage graphs
with deterministic uuid5 identities), the generated flow-pack manifest, caps
and approval classes, HITL points, eval criteria, synthetic drill fixtures,
rollout rung (shadow by default), rollback plan, estimates, and open
questions. Reuse-first is enforced in code — a card that recreates an
existing workflow/collection/agent is rejected at validation naming the
existing id — and validation is fail-closed everywhere (unknown fields, bad
stages, non-catalog tools, real-looking accounts in fixtures, credential
bytes anywhere). Cards are kernel events (the `compilations` aggregate:
replay == live, versions diff cleanly, recompilation is a new version).
Review with `/compile show|approve|reject|revise`: approval is an
origin-bound, local-only kernel record pinning the exact card digest — the
session can never approve its own card, and a revision voids any approval.
`/compile materialize` then drives `CapitolAdmin` in the card's declared
order (collections → workflows → agent + exact allowlist → schedules),
writes the generated pack + drill fixtures, and is idempotent end-to-end
(re-materializing replays receipts; partial failure stops with everything
recorded and `/compile rollback <id>` reverts it all via the recorded
rollback refs, never deleting adopted pre-existing assets). The validation
gate loads the pack fail-closed, runs the generated acceptance drill
(`workflow_drill`, also runnable via `/capitol pack verify`), and only on a
pass creates the dry-run supervising mission — status advances
compiled→materialized→verified→operating, one kernel event each. v1 is
interactive-only and materializes on the local stack only; materialization
authority is exactly your `capitol_admin` authority. The live end-to-end
proof (materialize → drill → mission → rollback) runs behind
`CONCH_CAPITOL_LIVE=1` in `tests/test_compiler_live.py`.

### Budget-aware turns
Besides `/rounds`, an optional `turn_token_budget` caps token spend per turn. When either budget runs out, the model writes a progress summary (what's done, what remains) instead of dropping a bare "[max tool call rounds reached]".

### Weak-model side tasks
Point `weak_model` (and optionally `weak_provider`) at a small fast model and Conch runs session summaries and history compaction on it, keeping the main model's KV cache and VRAM untouched.

### Memory
Conch remembers facts across sessions. Use `/remember` to save manually, or the LLM saves important context automatically via the `save_memory` tool. Recall is ranked with SQLite FTS5 (bm25) when available. A separate always-loaded tier lives in `~/.config/conch/facts.md` — append with `/fact <text>`, view with `/facts`; its (bounded) contents ride in the system prompt of every session. Conversation search (`/search`, `search_conversations`) runs on a SQLite FTS5 index instead of scanning every file, synced incrementally as conversations are saved.

Memory never stores credentials. Every write path (`save_memory`,
`/remember`, auto session summaries, mission-lesson consolidation) runs a
deterministic credential detector — JWTs, Atlassian/AWS/OpenAI/Slack/
GitHub/GitLab/Google token shapes, private-key blocks, `password:`/`token:`
style assignments, and high-entropy strings next to auth words — and a
matching entry is rejected whole with the type named (never sanitized and
saved). Retrieval re-scans on the way out, so a legacy entry written
before the gate (or matching patterns added later) is dropped from model
context and logged by type instead of surfacing. Mentioning credentials is
fine ("the Jira token lives in mcp.json"); pasting one is not. The store
file itself is owner-only (0600).

### Repository map
When you start Conch inside a git repo, a ~1k-token structural overview (ranked files + top-level symbols) is injected into the system prompt so the model starts oriented. Disable with `repo_map=false`.

### Self-knowledge
Ask conch "what can you do?" or "how does your memory system work?" and it answers from itself, not from memory: the `conch_introspect` tool reports its full feature surface generated live from the running registries (`capabilities`: slash commands, loaded tools/MCP servers, skills, profiles, providers), its effective configuration with secrets hidden (`config`), a map of its **own** source tree with branch/version/recent commits (`source_overview`), and any of its source files, path-validated and paginated (`read_source`). Everything is token-bounded for small local models, and like the other self-management tools it is never exposed to remote sessions.

### Backend preflight
After a failed turn, Conch pings the Ollama server before sending your next message; if it's still offline you get a clean "still offline" notice, your message is kept in the input line for a one-keystroke retry, and nothing broken enters the transcript.

### Conversations
Full conversation persistence with `/new`, `/switch`, `/convos`, `/delete`, and `/clear`. Titles are set automatically from your first message.

The shell resumes your most recent conversation on startup; `conch --new` (or `-n`) starts with a fresh one instead. Conversation files are written crash-safely (unique tmp file, fsync, atomic rename), and a corrupt file never blocks startup: the valid JSON prefix is salvaged when possible, otherwise the file is quarantined to `<name>.json.corrupt-<timestamp>` and the shell falls through to the next conversation.

### Tool profiles
Switch between named tool presets: `/profile minimal` (core local-agent tools), `/profile dev` (GitHub, Jira), `/profile comms` (Gmail, Slack), `/profile full` (everything). Define your own in config with `profile_<name> = group1, group2`, or pick one per session with `tool_profile=<name>`. On Ollama and custom local endpoints the minimal profile is active by default — `/profile full` overrides. When more tools are available than the provider's cap, Conch sends a small always-on agent core plus the tools most relevant to the current message.

### Local-model context management
Ollama owns `num_ctx` by default so Conch does not unexpectedly allocate a
large KV cache on a smaller machine. Set `ollama_num_ctx` only when you want an
explicit override. Conch reads the effective loaded context from `/api/ps`,
falls back conservatively before load, and reports the same value in
`/status` and its model-facing self-description. Token calibration is isolated
per provider/model. Compaction preserves complete assistant-call/tool-result
groups, and all results from a parallel tool round share one context-scaled
budget rather than each claiming a separate 10%.

### Tool-calling drift control
Conch executes only native structured tool calls. Textual JSON/XML calls are
diagnostic failures, never an executable fallback and never classifier input.
Every call is canonicalized once, checked against the exact tool list sent in
that request, and validated against its JSON schema before hooks or execution.
Repeated identical call batches are stopped. Local requests can include a
small native-call exemplar (`local_tool_exemplar=false` disables it) without
adding extra or trailing system roles.

### Custom slash commands
Markdown files in `~/.config/conch/commands/` become commands: `review.md` becomes `/review`, and `$ARGUMENTS` in the file body is replaced with whatever follows the command. Custom commands tab-complete alongside builtins (builtins always win on name conflicts).

### Project-level context
When the current directory (or any parent up to the git root) contains a `CONCH.md` or `AGENTS.md`, its contents are injected into the system prompt as persistent project instructions (capped so they can't swamp small models). A `.conchrc` in the project overrides global config — pin a provider, model, or `ollama_num_ctx` per repo.

### Per-model prompt overrides
Map a provider/model glob to your own system-prompt template file: `chat_prompt:ollama/qwen* = ~/.config/conch/prompts/qwen.md`. The most specific matching pattern wins; `ask_prompt:` works the same for ask mode.

### Composio integration
Connect OAuth services like Gmail, GitHub, and Slack with `/connect <app>`. Uses the Composio API for real OAuth flows.

### Scheduling
Run recurring prompts with `/schedule 10m check disk usage` or natural language like `/schedule daily email report`.

### Edge daemon and missions
Opt-in (`edge_daemon = true` in the config): the `conch-edge` daemon keeps
long-horizon missions running when the terminal closes. Everything durable
lives in one transactional SQLite kernel under
`~/.local/state/conch/kernel/kernel.db` — missions, plans, tasks, timers,
checkpoints, integer-unit budgets (reserve/commit/release), origin-bound
approvals with one-use nonces, an external-action ledger, a transactional
inbox/outbox, artifacts, and an immutable hash-chained event journal that
projections replay from exactly. Missions run as bounded, checkpointed work
sessions (fresh rehydrated context each time, never a growing transcript)
with hard wall/token/round caps; the model schedules its own next wake via
the `mission_control` tool or falls back to the mission cadence.

Cadence missions — a recurring schedule and no success criteria — run
forever by design, so their specs default to
`allow_model_completion = false`: a `complete_mission` call from the model
is refused with a steering message and journaled as a `completion_denied`
event, and only an operator finishes the mission (`fail_mission` stays
available). Missions with success criteria or `run_once` default to
`true`; set the field explicitly in the spec to override either way.

Quickstart:

```bash
# 1. enable in ~/.config/conch/config
echo "edge_daemon = true" >> ~/.config/conch/config
# 2. install the supervised daemon — the default way to run it: launchd
#    (macOS) or a systemd user unit (Linux) restarts it on crash and
#    brings it back after reboot
conch-edge install
conch-edge status          # supervisor state + daemon health
# (conch-edge with no subcommand still runs it in the foreground;
#  conch-edge uninstall stops and removes the supervised daemon)
# 3. from the conch shell, attach
#    /missions               list missions and next wakes
#    /mission new <goal>     start a durable mission (daily cadence default)
#    /mission show <id>      spec, plan, budgets, checkpoint, recent events
#    /mission pause|resume|abort <id>
#    /mission input <id> <answer>   answer a parked mission (wakes it now)
#    /approvals              pending exact-action approvals
#    /approve <id>           decide one (origin-bound, expiring, one-use)
```

`conch-edge install` renders the template in `deploy/` with this machine's
paths (resolved program, PATH, log files under the kernel dir), loads it,
and verifies the daemon answers its control socket. The rendered unit
carries no secrets — supervised daemons start with a minimal environment,
so channel/provider tokens must be supplied by reference:
`launchctl setenv SLACK_BOT_TOKEN ...` on macOS, or a systemd user drop-in
with `EnvironmentFile=` pointing at a 0600 file on Linux. (Capitol bearers
resolve from the A2Actrl registry file, which needs no environment.)

With `remote_enabled = true` the daemon also hosts channel intake (see the
remote loop section): inbound Slack/SMS/email get full agent turns around
the clock, no shell attached, and external events wake parked missions
immediately instead of waiting out their timers.

**Mission judgment.** Every standard mission also gets a scheduled critic
session — every N work sessions or daily (whichever comes first; spec
`review` object or `mission_review_*` config keys), on the cheap
`weak_model` when configured, falling back to the main model. Stall
detection is deterministic and runs before the model sees anything: no
material kernel-event change (plan versions, task movement, external
actions, artifacts, approvals, bindings) across the last N sessions, or
the same step failing repeatedly. The critic scores each success
criterion (met / on-track / stalled / at-risk with one-line evidence) and
lands exactly one action as kernel events: **continue**, **re-plan** (a
new numbered plan version through the normal plans machinery, journaled
as an explicit `plan_revised` event with rationale), or **escalate** (a
channel notification through the outbox — never silent; a stalled mission
may never "continue"). Reviews are bounded sessions with no tools, obey a
`reviews` budget line when the spec declares one (exhaustion is a
journaled skip, never a crash), are suppressed by STOP/pause like any
session, and `/mission show` renders the latest verdict.

**Shared memory across missions.** After each checkpoint a weak-model
consolidation pass distills durable, non-secret lessons from that
session's journal delta into the shared memory store, tagged with mission
id + topic, deduplicated and size-capped; a deterministic scrubber
rejects anything resembling credentials, org UUIDs, or channel identities
whole. Any mission's next rehydration then retrieves the top-K relevant
lessons (FTS match on its goal/plan/task keywords, hard char cap) into a
clearly labeled "Lessons from prior missions" block, so what one mission
learns the others get for free — within the same overall context bound.
Consolidation is skippable (`mission_consolidation = false`) and runs as
a post-checkpoint side task with a timeout: failure or timeout is a
logged skip, never a blocked session.

The first daemon start migrates existing `tasks.json` schedules into the
kernel (original preserved as `tasks.json.bak`); `/schedule`, `/tasks`, and
`/cancel` keep their exact UX, now executed by the daemon. Two daemons can
never share a kernel (OS lock + controller epoch fencing); `kill -9` is
recoverable by design — timers fire exactly once in the ledger, interrupted
sessions are abandoned by lease expiry and retried from the last
checkpoint, and notifications ride the outbox with dedupe keys.
Notifications deliver over `notify_channel` (Slack/SMS/email) when
configured; without one they land in the daemon log
(`~/.local/state/conch/kernel/daemon.log`) as delivered records, and
configuring Slack later upgrades delivery with no mission changes.
Emergency stop: `touch ~/.local/state/conch/kernel/STOP` halts every
mission session (checked at session start and, through the fail-closed
required-policy layer, before each tool round). Without `edge_daemon`
enabled, none of this loads — the interactive shell and its in-process
scheduler behave exactly as they always have.

### Personal items (todos, recipes, papers)
Conch as a capture-and-recall companion: durable personal records in named
**spaces** — `todo` (the default), `recipes`, `papers`, and any space you
invent by writing to it — stored in the local mission kernel with the same
event-sourcing discipline as missions (immutable per-item history, replay
== live, the existing backup/restore story). Capture and query from two
surfaces backed by one store: the `/todo` command family (`/todo add renew
passport due:tomorrow p1 #errand -- bring the old one`, `/todo done 3`,
`/todo list`, `/todo show 3`; bare `/todo` prints today's view — due,
overdue, top urgent) with the general `/list <space>` form for the rest
(`/list recipes add carbonara`, `/list papers show 2`), and the
`personal_items` tool for plain chat ("add milk to the shopping list",
"what's most urgent?"). "Most urgent" is **computed, never model-ranked**:
overdue > due today > explicit priority (p1–p5) > age, deterministic and
explained inline (`<overdue 2d>`, `<priority p1>`). The tool is also
available to remote/channel sessions — texting `todo: renew passport by
Friday` to the always-on daemon captures it, gated by the same fail-closed
sender allowlists as every channel feature — but never flows into
delegated sub-turns or fleet workers unless a skill or task envelope
offers it explicitly.

The escalation ladder: items start as records; `/todo work <id>` loads one
(with its full event history) into the current session as context to chip
away at; `/todo escalate <id>` births a durable **mission** through the
normal intake (goal seeded from the item's title/body, criteria/budgets/
cadence confirmable via a JSON spec, `{"success_criteria": [...],
"budgets": {"sessions": 5}}`), binds `mission_id` on the item, and when
that mission later succeeds or aborts, a proposal event lands back on the
item's history (`proposes complete` / `proposes review`) — the item's
status stays yours to change. Privacy is structural: personal spaces never
leave the machine (no Capitol calls, no org surface, excluded from
cross-mission lesson consolidation and memory retrieval — items are
*siblings* of memory, not memories), the credential write-guard rejects
secret-bearing items whole exactly like memory does, and item content is
stored text — reading an item whose body says `/agent on` or `approve 1`
executes nothing.

### Notes (/notes) — replacing the Apple Notes habit
Editor-backed notes on the same personal-items store (space `notes`): no
new database, the same event-sourcing discipline, the same privacy rules.
`/notes new [title]` opens your editor on a scratch buffer through the
direct terminal handoff (the `/terminal` gate — interactive local sessions
with a real TTY only); the buffer's first line `# Title` names the note
(falling back to the argument, then `Untitled <date>`), save+quit stores
it, and abandoning an empty buffer stores nothing. `/notes open
<#|id|title>` reopens a note — titles resolve by unambiguous substring,
ambiguity lists the candidates — and every save is an `item_updated`
event, so `/notes show` gets the full edit history ("edited 3 times, last
…") for free. `/notes add "title" [#tag] -- body` quick-captures without
the editor (the `/todo add` grammar); `search` scans titles and bodies;
`archive`/`reopen` complete the lifecycle; `/note` aliases. The editor
chain is the `editor` config key → `$VISUAL` → `$EDITOR` → nano if
installed → vi (set `editor = nano` or `= vi` to pin; the value may carry
arguments, e.g. `editor = code --wait`). In remote and channel sessions
the editor verbs refuse and point at quick-add and the `personal_items`
tool, which work everywhere. The point is weaning off Apple Notes: capture
keeps the frictionless open-an-editor-and-type shape, but notes land in a
queryable, history-bearing, agent-reachable store — the model pulls them
through `personal_items` search when relevant, never wholesale. Context
pinning and `note:` references for missions/packs land in N2; Apple Notes
import in N3.

### Trusted SSH fleet (workers)
Opt-in distributed execution: the controller deploys bounded, replaceable
workers to trusted SSH hosts and dispatches versioned task envelopes to
them. Workers are compute, never state authorities — mission truth,
credentials, budgets, and the external-action ledger stay on the
controller; a worker receives only a task envelope (its exact
tool/model/data/budget authority) and returns events and artifacts.

Every deployment is a **signed single-file artifact**: a reproducible
`.pyz` (stdlib `zipapp`, bundling `conch` + `pygments`, entry point =
`conch-worker`) with a canonical-JSON sha256 manifest, signed with OpenSSH
`sshsig` (`ssh-keygen -Y sign`). Verification on the host is mandatory and
fail-closed — a missing or wrong signature, an unknown manifest version,
or a digest/size mismatch refuses activation. Containers are the optional
strict-isolation profile (the `worker` target in the Dockerfile,
digest-pinned); delivery, verification, and rollback are identical across
profiles.

```bash
# Build + sign a worker artifact (operator machine)
ssh-keygen -t ed25519 -f ~/.config/conch/fleet/signing-key   # once
python tools/build_worker_artifact.py --out dist/conch-worker.pyz \
    --sign-key ~/.config/conch/fleet/signing-key \
    --principal fleet@you --emit-allowed-signers > allowed_signers
```

The build summary prints to stderr, so the redirection yields exactly the
one signer line; `--allowed-signers-out PATH` writes the trust anchor
straight to a file instead. `trust-install` validates that every
non-comment line of the incoming file parses as an OpenSSH
allowed-signers entry and refuses the whole install (naming the bad
line) otherwise.

Lifecycle (enroll → probe → deploy → dispatch):

1. **Enroll** a host interactively with strict host-key verification, then
   confirm restart-safe key-based `BatchMode` auth. `conch-hostctl` is
   streamed to the host and installed by verified sha256 (stdlib-only, so a
   bare host needs only `python3`). A host is `autonomy_capable` only when
   unattended key auth works; password-only hosts enroll but never run
   unattended. While you are still in the interactive session, enable
   session lingering for the worker user — `loginctl enable-linger <user>`
   (it usually needs sudo/polkit, which is why it belongs in this
   interactive step and not an unattended path). Without linger the user
   systemd manager is torn down at logout, killing every user-scope worker
   (systemd *and* process profiles); `probe` reports the linger state and
   `worker-start` refuses without `--force` while it is off.
2. **Probe** records the host's arch/OS, Python, systemd version and
   sandboxing, Docker, disk, cgroups, GPU (`nvidia-smi`), and reachable
   local model endpoints as *observed capabilities* — kept separate from
   the administrator-assigned trust level, data ceiling, and labels.
3. **Deploy / activate / rollback** are idempotent with operation
   receipts under a remote deployment lock: repeating a deploy is a no-op,
   the previous revision is retained, and rollback swaps back (symlink/unit
   swap for the process/systemd profiles, image rollback for docker).
   Workers run under a hardened user-level **systemd** unit on Linux
   (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `ProtectHome`,
   `MemoryMax`/`CPUQuota`/`TasksMax`, `IPAddressDeny` + localhost allow),
   a plain **supervised process** where systemd is absent (macOS dev), or
   the **docker** profile for strict isolation.
4. **Dispatch** goes over `WorkerTransport` — a fixed JSON request/response
   line each way carried by `conch-hostctl rpc <worker>` over SSH stdio (no
   secrets or prompts in argv, no worker network port; the worker name may
   also be passed as `--worker <worker>`). For manual debugging,
   `conch-hostctl rpc <worker> --op worker.status` mints a valid request
   itself — canonical `rpc_id` included — instead of requiring a
   hand-built JSON line on stdin. The controller
   schedules by filter→score (protocol/state/trust/data ceiling/model
   residency/capacity, with shared-endpoint resource groups so one Ollama
   box is never oversubscribed), offers with a fencing token + controller
   epoch, leases, retries by failure class with backoff/jitter, and
   heartbeats with monotonic deadlines. A worker's `delegate_task` becomes
   a controller-brokered child dispatch (validated for depth, fan-out,
   budget, authority-subset, and trust placement); the parent parks and its
   child's result returns as a complete tool-result group.

None of this loads for shell-only users; the fleet is entirely opt-in and
the interactive CLI never depends on it. See `PLAN.md` (Swarm Phase 2) for
the current status and the items that still need a live Linux/SSH host for
final verification.

### The fleet awakening: conch-controller, /fleet, grants, fleet_delegate

Set `fleet_controller = true` and the substrate above comes alive — the
`conch-controller` daemon drives the task plane end to end, the `/fleet`
command family is the cockpit, and `fleet_delegate` lets local agent
sessions hand bounded tasks to remote workers mid-conversation.

**`conch-controller`** is a supervised daemon exactly like `conch-edge`:
its own kernel scope at `~/.local/state/conch/fleet/` (worker registry,
dispatches, dispatch events, the grant ledger — separate database, lock,
and fencing epoch from the edge daemon's mission kernel, so both coexist
on one machine), a 0600 control socket (`fleet.sock`), and
`conch-controller install|uninstall|status` rendering the same
launchd/systemd machinery. Each tick schedules queued dispatches onto
eligible workers over SSH stdio, polls events and fenced receipts,
retries by failure class with backoff, sweeps heartbeats (silent worker →
UNREACHABLE + requeue), propagates cancellations, records worker skill
inventories, and pulls completed tasks' content-addressed artifacts into
the local store. Kill it mid-dispatch and a restarted controller adopts a
higher epoch, finishes supervising the same attempt from the worker's
idempotent receipts, and the superseded process can never write again.

**`/fleet`** works against the running controller, or direct-drives the
fleet kernel one-shot when no daemon is up (the same two-transport client
the mission commands use):

```
/fleet workers                    # registry + live reachability probe
/fleet enroll burt-1 burt@host    # interactive first-enroll (host key,
                                  # hostctl bootstrap, BatchMode probe)
/fleet run burt-1 "report uname -a and disk free"
/fleet run auto "..." --skill capitol --model qwen3:8b --budget 50000
/fleet task <id> | tasks | cancel <id>
/fleet artifacts <id> pull        # digest-verified artifact pull
/fleet drain burt-1 | enable burt-1
/fleet grant burt-1 --actions communicate --tools save_memory,full ...
/fleet status
```

**Owner grants and the authority clamp.** Every worker starts narrow:
action classes `{read, compute, write_local}`, the small default tool set
(`local_shell`, `public_api`, `todo_list`, brokered `delegate_task`), and
its admin-assigned data ceiling. `/fleet grant` raises one worker's
ceiling — grants are admin labels riding ledgered `worker_updated` kernel
events (hash-chained; replay == live). Every dispatch clamps to
`min(requested, worker ceiling, caller authority)`: explicitly requested
excess is refused with the exact names, omitted dimensions default
narrow. The hard exclusions never lift, whatever the grants say:
`conch_config`, `manage_tools`, `skill_manage`, `interactive_terminal`,
`ssh_remote`, and `fleet_delegate` (recursive fleet delegation — a
worker's `delegate_task` is the controller-brokered form). The scheduler
is ceiling-aware too: an envelope a worker's grants would refuse never
places there.

**Skill-addressed dispatch.** `--skill <name>` makes the remote agent act
as that skill: the envelope carries the skill name, the skill's declared
tool scope becomes the requested tool set, and the worker loads its own
installed copy of the skill (prompt + tool re-intersection) at execution.
Skill availability is capability reporting — `worker.status` lists
installed skills (built-ins ship inside the signed artifact; user skills
live in the worker account's `~/.config/conch/skills/`), the controller
records the inventory, and dispatch to a worker missing the skill is
refused with a clear error (v1 never ships skill files to hosts).

**`fleet_delegate`** is the model-callable form for local sessions
(interactive and missions — never remote/channel sessions, never workers,
never inherited by local subagents implicitly): the model delegates a
bounded task, the controller brokers it through the TaskPlane with the
authority-subset rule enforced in code, and the worker's summary plus
artifact references return as tool output. Missions opt in through the
spec's `fleet` block — allowed `workers`/`skills`, a `tools`/`actions`/
`data`/`token_budget` authority ceiling — and their fleet calls are
serialized across shared-endpoint resource groups like every other
dispatch.

### Cost tracking
See token usage and estimated cost per turn and per session. `/cost` for session totals.

### Background input
Type your next message while the LLM is still working — it queues and runs next. Toggle with `/queue`. A block pasted while the model is working queues as one message instead of one per line.

### Multiline input & paste
Pasting a block of text (logs, code, a long prompt) sends it as **one** message, never one per line, and interior lines are never interpreted as slash commands. Layers, all stdlib:

- **Paste at the prompt.** On GNU readline 8.1+ true bracketed paste is enabled: the paste lands in the edit buffer as a unit and one Enter sends it. macOS system Pythons link libedit, which can't do bracketed paste; there Conch detects the paste as it arrives, reassembles it (tabs and all), shows the captured lines with a dim `...` gutter, and waits for a single Enter to send — same shape, either backend. A pasted trailing newline never auto-sends. Escape hatch: `multiline_paste=false` in config.
- **Code fences.** A line opening a ``` fence keeps reading at a `...` prompt until the closing fence, then sends the whole block.
- **Backslash continuation.** End a typed line with `\` to continue it on the next line.
- **`/paste`.** Reads raw lines — no completion, no history, nothing interpreted — until a line that is exactly `.` or Ctrl+D; Ctrl+C cancels.
- **`/edit`** (also `/paste --editor`). Composes the message in your editor, git-commit style, using the same `editor` chain as `/notes`: config `editor` → `$VISUAL` → `$EDITOR` → nano if installed → vi. Save and quit to send; an empty file or nonzero exit aborts. Like `/notes new`, it needs the interactive local session (the `/terminal` gate); elsewhere `/paste` still works.

Ctrl+C during any continuation discards the pending block and returns to the prompt — nothing partial is ever sent. Multiline messages are stored as a single history entry with newlines shown as ` ⏎ ` (readline history files are newline-delimited).

### Automatic retry and clean failure
Transient API errors (429, 5xx, connection refused, timeouts, missing models) are retried once with a 1-second backoff before falling through to the provider fallback chain. All providers signal failure uniformly; error text is never saved into conversation history or memories — a dead Ollama server prints "server unreachable at <url>" and the session stays alive so you can just send your message again.

### Structured ask mode
`conch-ask` never scrapes commands out of free text. Ollama, llama.cpp/custom,
OpenAI, Anthropic, Cerebras, Bedrock, and OpenRouter must return a native
`shell_command` tool call.

## Containers

The default Compose service builds the non-root development image, mounts only
the current project, persists Conch config/state in named volumes, uses bridge
networking, and enables a read-only root filesystem with dropped capabilities:

```bash
docker compose build
OLLAMA_HOST=http://host.docker.internal:11434 \
  CONCH_MODEL=llama3.2:3b \
  docker compose run --rm conch
```

For a LAN server, set `OLLAMA_HOST=http://192.0.2.247:11434`. On Linux,
Compose maps `host.docker.internal` through `host-gateway`. To run an Ollama
sidecar:

```bash
docker compose --profile ollama-sidecar up -d ollama
docker compose exec ollama ollama pull llama3.2:3b
OLLAMA_HOST=http://ollama:11434 \
  docker compose --profile ollama-sidecar run --rm conch
```

For llama.cpp, place a tool-capable GGUF under `./models` (or set
`LLAMACPP_MODEL_DIR`) and run:

```bash
LLAMACPP_MODEL_FILE=model.gguf \
  docker compose --profile llamacpp-sidecar up -d llamacpp
CONCH_PROVIDER=custom \
  CONCH_CUSTOM_BASE_URL=http://llamacpp:8080/v1 \
  CONCH_CUSTOM_MODEL=your-model-id \
  docker compose --profile llamacpp-sidecar run --rm conch
```

Use `docker compose run --rm conch ask "list files"` for `conch-ask`, or
`docker compose run --rm conch shell` for a shell. The entrypoint never
rewrites mounted config and never deletes conversations, memories, or
`search.db`. Set `CONCH_WORKSPACE=/absolute/project/path` to mount a different
project. Compose builds the user as UID/GID 1000; on Linux set
`CONCH_UID=$(id -u)` and `CONCH_GID=$(id -g)` before building when your host
IDs differ. Build `--target minimal` for the smaller image; when using that
target directly, pass Docker's `--init`.

## Chat commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/models` | List available models |
| `/model <name>` | Switch to a model validated against the provider catalog/live local service |
| `/provider <name>` | Switch provider |
| `/agent` | Toggle agent mode (auto-execute shell) |
| `/yolo` | Alias for `/agent on` |
| `/terminal <command>` | Hand the real terminal to a credential-aware command without capture |
| `/ssh connect <user@host> [port]` | Authenticate interactively and create an OpenSSH control connection |
| `/ssh status` | Check the active SSH control connection |
| `/ssh exec <command>` | Run a captured, permission-gated command over the control connection |
| `/ssh shell [command]` | Open an uncaptured remote TTY (including remote sudo) |
| `/ssh disconnect` | Close the active SSH control connection |
| `/install [component]` | List conch components or set one up (edge, fleet, works) |
| `/missions` | List durable missions (edge daemon) |
| `/mission show\|new\|pause\|resume\|abort\|input …` | Manage a mission |
| `/todo` | Today's personal todos: due, overdue, top urgent |
| `/todo add\|done\|due\|list\|show\|work\|escalate …` | Manage the personal todo list |
| `/list <space> [verb …]` | Other personal item spaces (recipes, papers, …) |
| `/notes` | Recent notes; `new [title]` / `open <#\|id\|title>` write in your editor (`/note` aliases) |
| `/notes add\|show\|search\|archive\|reopen …` | Quick-capture and manage notes without the editor |
| `/approvals` | List pending mission approvals |
| `/approve <id>` / `/deny <id>` | Decide a pending mission action |
| `/ebay <photo...> [-- notes]` | Draft and publish an eBay listing through a Capitol workflow |
| `/new` | Start a new conversation |
| `/clear` | Wipe history (keep conversation) |
| `/convos` | List conversations |
| `/switch <id>` | Switch conversation |
| `/delete <id>` | Delete conversation |
| `/remember <text>` | Save a memory |
| `/memories` | List saved memories |
| `/forget <id>` | Delete a memory |
| `/fact <text>` | Save an always-loaded fact (`facts.md`) |
| `/facts` | Show the always-loaded facts |
| `/skills` | List saved skills |
| `/skill <name> [task]` | Run a skill's procedure on a task |
| `/tools` | List tool groups |
| `/enable <group>` | Enable a tool group |
| `/disable <group>` | Disable a tool group |
| `/profile [name]` | Switch tool profile |
| `/connect <app>` | Connect a service (OAuth) |
| `/apps` | List connectable services |
| `/schedule <spec>` | Schedule a recurring task |
| `/tasks` | List scheduled tasks |
| `/cancel <id>` | Cancel a task |
| `/cost` | Show session token usage |
| `/status` | Show provider, model, context window/usage, and config |
| `/verbose` | Toggle showing tool args and results |
| `/rounds <n>` | Set max tool call rounds |
| `/queue` | Toggle typeahead input |
| `/paste` | Paste lines literally; end with a lone `.` or Ctrl+D |
| `/edit` | Compose the next message in your editor (the `/notes` `editor` chain) |
| `/reload` | Reload MCP tools |
| `/resettools` | Reset tool-calling when a local model drifts into writing tool calls as text |
| `/search <query>` | Search conversations, memories, and config |
| `/<custom>` | Any markdown file in `~/.config/conch/commands/` |

## Use cases

Complete flows built by composing the core product — the Capitol
integration, channels, origin-bound approvals, and the mission kernel.
These are worked examples, not core product surface.

### eBay listing pilot (photo → listing over Capitol workflows)
`/ebay <photo...> [-- notes]` sells an item photo-first through a governed
Capitol A2A workflow. Conch talks to the gateway with the stdlib-only
adapter (`conch/capitol/`): agent-card discovery, handshake, idempotent
workflow calls, SSE event streaming with resume, HITL
clarification/intervention relay, and artifact upload. The workflow's model
owns all listing judgment — what the item is, title, description, category,
price, and when to ask you a clarifying question (relayed in the shell).
Deterministic machinery exists only at the money boundary: publishing requires
the exact-approval challenge (`proceed to post` + `POST r{rev} {hash[-12:]}`
over the immutable revision hash), an idempotency key so retries can never
double-post, a thin caps clamp (optional price ceiling + category allowlist
deciding auto vs. explicit approval), and a required-policy consult. Configure
`capitol_base_url` / `capitol_org` / `capitol_agent` / `capitol_bearer_env`
(the bearer is read from that env var or `~/.capitol-a2a/agents.yaml`, never
stored in config) plus the `ebay_*` keys in `config.example`. Run linkage
(run ids, revision hashes, listing ids) persists as a tiny JSON file under the
XDG state dir.

**Message-first Slack intake.** With the remote loop running,
a Slack message containing item photo(s) plus whatever you know about the item
*is* the intake: conch quarantines and validates the photos, starts a listing
session in the same governed pipeline, and the message's thread carries
everything that follows — clarifying questions (reply in-thread to answer,
including mid-run HITL checkpoints, which park durably and survive restarts),
the drafted-revision review, and the publish decision. The caps clamp decides
auto vs. approval: within caps *and* with the explicit
`ebay_channel_auto_publish=true` opt-in it publishes and confirms in-thread;
otherwise it posts an origin-bound approval — the immutable revision summary
plus the exact challenge (`POST r{rev} {hash[-12:]}`) — that only `approve N`
from the same channel, thread, and sender can consume (TTL'd; expired ones are
reissued). Consuming the approval *constructs* the publish request from the
pinned revision, and Capitol's approval node re-verifies the same identity —
two staleness checks in series; any new revision voids the pending approval.
Inbound text is item data, never control: only the anchored `approve N` /
`deny N` replies carry control semantics, so approval-like text buried in a
message body, sent from the wrong thread, or from a non-allowlisted sender
publishes nothing. Slack app requirements: the `files:read` bot scope (plus
`chat:write` and your channel type's history scope), then reinstall the app;
see `config.example` for `ebay_channel_intake` / `ebay_channel_auto_publish`.

### Funding-intake pipeline
`conch/capitol/together_funding.py` builds a scheduled email-intake →
research → document pipeline entirely out of the same primitives:
`CapitolAdmin` provisions the versioned workflows, schedule, and ledger
collection idempotently, and mission supervision binds the scheduled runs.
It is an example of provisioning and supervising a multi-stage Capitol
pipeline from Conch, not a core feature.

## Development

```bash
python3 -m unittest discover -s tests
uvx ruff check --select E9,F63,F7,F82 conch tests
docker compose config --quiet
docker compose build conch
```

The suite includes real local HTTP integration harnesses for Ollama-style Qwen
and Llama loops plus llama.cpp-compatible discovery/probing, as well as unit
coverage for request-scoped authorization, protocol pairing, outage recovery,
digest invalidation, context limits, local-only policy, containers, remote
approvals, and the existing UI/storage/tooling surfaces.

## Architecture

```
conch/
├── app.py           Main chat loop and CLI entrypoint
├── capitol/         Capitol A2A adapter, admin, supervision + use-case drivers
├── channels.py      Matrix/Slack/SMS/email gateways + ntfy push + allowlists
├── cli.py           One-shot ask entrypoint
├── commands.py      Slash command handlers (+ user-defined commands)
├── composio.py      Composio OAuth integration
├── config.py        Config file loading, project .conchrc/CONCH.md
├── conversations.py Conversation persistence + FTS5 search index
├── llm.py           Ask-mode LLM calls (structured output)
├── mcp.py           MCP stdio + HTTP transport
├── memory.py        Persistent memory store + facts file
├── prompts.py       Provider-specific system prompts + overrides
├── providers.py     LLM provider adapters + streaming (incl. custom)
├── remote.py        Remote agentic loop: sessions, approvals, safe_auto cap
├── render.py        Syntax highlighting + StreamPrinter
├── repomap.py       Repository-map orientation context
├── runtime.py       Chat turn logic, compaction, budgets, hooks dispatch
├── scheduler.py     Background task scheduler
├── secure_terminal.py Direct terminal handoff + terminal-state restoration
├── skills.py        Skill definitions: loader, builder, rendering
├── ssh_control.py   Validated OpenSSH ControlMaster lifecycle
└── tooling.py       Tools, profiles, permissions, hooks, subagents
```

Container assets live at the repository root (`Dockerfile`,
`docker-compose.yml`, `docker-entrypoint.sh`). CI verifies Python
3.9/3.11/3.13, wheel installation, Compose validation, non-root execution, and
both image targets.
