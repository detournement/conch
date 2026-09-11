# Conch

Conch is an LLM-assisted shell with two interfaces:

- **`conch-ask`** / **`ask`** — one-shot command generation
- **`conch`** / **`conch-chat`** — multi-turn chat with MCP tools, memory, and scheduling

## Install

### From PyPI

```bash
pipx install conch-shell
# or
pip install conch-shell
```

### From source

```bash
git clone https://github.com/detournement/conch.git
cd conch
./install.sh
```

The installer configures your API keys and shell integration (`ask` / `conch` aliases).

## Configuration

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
ollama_base_url=http://192.168.1.247:11434
model=qwen3.6:27b
chat_model=qwen3.6:27b
local_only=true
```

Switch providers at any time in chat with `/provider openai`, `/provider anthropic`, or `/provider ollama`.
For containers and automation, the same settings can be supplied as
`CONCH_PROVIDER`, `CONCH_MODEL`, `CONCH_OLLAMA_BASE_URL`,
`CONCH_CUSTOM_BASE_URL`, and `CONCH_LOCAL_ONLY`; environment variables take
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
| `hook_pre_tool_use` | — | Shell script gating every tool call (JSON on stdin; non-zero exit blocks) |
| `hook_post_tool_use` / `hook_on_turn_end` | — | Scripts receiving tool results / the final reply |
| `custom_base_url` / `custom_model` | — | OpenAI-compatible `/v1` endpoint for `provider=custom` (vLLM, LM Studio, llama.cpp) |
| `custom_context_window` | discovered / `32768` | Optional safe upper bound; llama.cpp is probed via `/v1/props`, then `/props` |
| `custom_temperature` / `custom_max_tokens` | `0.2` / automatic | Local OpenAI-compatible generation settings |
| `subagent_model` / `subagent_rounds` | parent model / `10` | Model and tool-round budget for `delegate_task` subagents (a skill's `model`/`rounds` override these) |
| `remote_enabled` | `false` | Start the remote loop (channel polling + replies) |
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
| Cerebras | gpt-oss-120b, gemma-4-31b, zai-glm-4.7 (deprecated 2026-08-17) | Paid / free tier |
| OpenAI | gpt-5.6 family (sol/terra/luna), gpt-5.5, gpt-5.3-codex, gpt-5.4 family, gpt-4.1 family, gpt-4o, o3, o4-mini, o1 (all tool-capable; o1-mini is not supported) | Paid |
| Anthropic | claude-fable-5, claude-opus-5, claude-sonnet-5, claude-opus-4-8, claude-opus-4-7, claude-sonnet-4-7, claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5 | Paid |
| Bedrock (AWS) | moonshotai.kimi-k2.5, moonshot.kimi-k2-thinking via Bedrock's OpenAI-compatible endpoint; auth is a long-term Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` (region via `bedrock_region`, default us-east-2) | Paid (AWS) |
| OpenRouter | moonshotai/kimi-k3 (2.8T MoE, 1M context, $3/$15 per MTok), z-ai/glm-5.2 (~750B MoE, 1M context, $0.98/$3.08 per MTok), deepseek/deepseek-v4-pro and deepseek/deepseek-v4-flash (1M context); key in `OPENROUTER_API_KEY` | Paid |
| Ollama | Discovered live from your server's `/api/tags`, filtered to models that advertise the `tools` capability | Free (local) |
| Custom | Models discovered from an OpenAI-compatible `/v1/models` endpoint (vLLM, LM Studio, llama.cpp); each visible model must pass a forced native tool-call probe | Depends |

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
The LLM can run shell commands on your machine. In normal mode, each command shows a prompt: **y**/Enter to run, **n** to decline (with optional feedback), **e** to edit the command first, **a** to always-allow commands with the same prefix for the session (`git status`, `docker ps`, …). Toggle `/agent` (or `/yolo`) for auto-execution. Command output streams live to your terminal.

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

### Remote loop (Slack, SMS, email)
With `remote_enabled=true`, conch messages you proactively and you can steer it from anywhere: scheduled task output is delivered over your `notify_channel`, and inbound replies are polled (Slack bot channel, Twilio SMS, IMAP inbox) and routed into conversations — a channel thread *is* a conch conversation, so replies resume it. Safety is enforced in code, not prompts: inbound senders must be on a per-channel allowlist (no allowlist = no inbound, fail closed); remote sessions are capped at **safe_auto** permissions regardless of local agent mode; and remote sessions never see self-management or delegation tools. Mutating commands create short-lived approvals bound to the exact channel, sender, thread, command, and timeout. Approval execution reruns lifecycle hooks, and approvals cannot be replayed from another conversation.

### Budget-aware turns
Besides `/rounds`, an optional `turn_token_budget` caps token spend per turn. When either budget runs out, the model writes a progress summary (what's done, what remains) instead of dropping a bare "[max tool call rounds reached]".

### Weak-model side tasks
Point `weak_model` (and optionally `weak_provider`) at a small fast model and Conch runs session summaries and history compaction on it, keeping the main model's KV cache and VRAM untouched.

### Memory
Conch remembers facts across sessions. Use `/remember` to save manually, or the LLM saves important context automatically via the `save_memory` tool. Recall is ranked with SQLite FTS5 (bm25) when available. A separate always-loaded tier lives in `~/.config/conch/facts.md` — append with `/fact <text>`, view with `/facts`; its (bounded) contents ride in the system prompt of every session. Conversation search (`/search`, `search_conversations`) runs on a SQLite FTS5 index instead of scanning every file, synced incrementally as conversations are saved.

### Repository map
When you start Conch inside a git repo, a ~1k-token structural overview (ranked files + top-level symbols) is injected into the system prompt so the model starts oriented. Disable with `repo_map=false`.

### Self-knowledge
Ask conch "what can you do?" or "how does your memory system work?" and it answers from itself, not from memory: the `conch_introspect` tool reports its full feature surface generated live from the running registries (`capabilities`: slash commands, loaded tools/MCP servers, skills, profiles, providers), its effective configuration with secrets hidden (`config`), a map of its **own** source tree with branch/version/recent commits (`source_overview`), and any of its source files, path-validated and paginated (`read_source`). Everything is token-bounded for small local models, and like the other self-management tools it is never exposed to remote sessions.

### Backend preflight
After a failed turn, Conch pings the Ollama server before sending your next message; if it's still offline you get a clean "still offline" notice, your message is kept in the input line for a one-keystroke retry, and nothing broken enters the transcript.

### Conversations
Full conversation persistence with `/new`, `/switch`, `/convos`, `/delete`, and `/clear`. Titles are set automatically from your first message.

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

### Cost tracking
See token usage and estimated cost per turn and per session. `/cost` for session totals.

### Background input
Type your next message while the LLM is still working — it queues and runs next. Toggle with `/queue`.

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

For a LAN server, set `OLLAMA_HOST=http://192.168.1.247:11434`. On Linux,
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
| `/reload` | Reload MCP tools |
| `/resettools` | Reset tool-calling when a local model drifts into writing tool calls as text |
| `/search <query>` | Search conversations, memories, and config |
| `/browse` | Interactive conversation browser |
| `/<custom>` | Any markdown file in `~/.config/conch/commands/` |

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
├── channels.py      Slack/SMS/email gateways + sender allowlists
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
├── skills.py        Skill definitions: loader, builder, rendering
└── tooling.py       Tools, profiles, permissions, hooks, subagents
```

Container assets live at the repository root (`Dockerfile`,
`docker-compose.yml`, `docker-entrypoint.sh`). CI verifies Python
3.9/3.11/3.13, wheel installation, Compose validation, non-root execution, and
both image targets.
