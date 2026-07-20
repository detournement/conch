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
model=claude-sonnet-4-6
chat_model=claude-sonnet-4-6
api_key_env=ANTHROPIC_API_KEY
```

Or for a local Ollama server:

```ini
provider=ollama
base_url=http://192.168.1.247:11434
model=qwen3.6:27b
chat_model=qwen3.6:27b
```

Switch providers at any time in chat with `/provider openai`, `/provider anthropic`, or `/provider ollama`.

### Config keys

| Key | Default | Description |
|-----|---------|-------------|
| `provider` | `anthropic` | `anthropic`, `openai`, `cerebras`, or `ollama` |
| `model` / `chat_model` | per provider | Model for ask / chat mode |
| `api_key_env` | per provider | Env var holding the API key |
| `agent_mode` | `false` | Auto-execute shell commands without confirmation (a startup notice is shown when enabled from config) |
| `base_url` / `ollama_base_url` | `http://localhost:11434` | Ollama server URL (`OLLAMA_HOST` also works) |
| `ollama_num_ctx` | `32768` | Context window requested on every Ollama call (clamped to the model's max; degrades to a conservative `8192` when the model's max is unknown — an explicit value always wins) |
| `ollama_keep_alive` | `10m` | How long Ollama keeps the model loaded between turns |
| `tool_profile` | — | Tool profile to apply for the session (see profiles below) |
| `profile_<name>` | — | Define a custom tool profile, e.g. `profile_research = github, jira` |
| `chat_prompt:<provider>/<model-glob>` | — | Path to a custom chat system-prompt template for matching models |
| `ask_prompt:<provider>/<model-glob>` | — | Same for ask mode |
| `permission_mode` | `prompt_all` | Shell approval policy: `prompt_all`, `safe_auto`, or `yolo` |
| `allow_prefixes` | — | Comma-separated command prefixes that never prompt, e.g. `git status, ls` |
| `hook_pre_tool_use` | — | Shell script gating every tool call (JSON on stdin; non-zero exit blocks) |
| `hook_post_tool_use` / `hook_on_turn_end` | — | Scripts receiving tool results / the final reply |
| `custom_base_url` / `custom_model` | — | OpenAI-compatible endpoint for `provider=custom` (vLLM, LM Studio, llama.cpp) |
| `custom_context_window` | `32768` | Context window assumed for the custom endpoint |
| `subagent_model` / `subagent_rounds` | parent model / `10` | Model and tool-round budget for `delegate_task` subagents (a skill's `model`/`rounds` override these) |
| `remote_enabled` | `false` | Start the remote loop (channel polling + replies) |
| `notify_channel` | — | Channel for scheduled-task output and notifications: `slack`, `sms`, or `email` |
| `remote_poll_interval` / `remote_rounds` | `60` / `8` | Inbound poll cadence (seconds) and tool-round cap for remote turns |
| `slack_channel` / `slack_allowed_senders` | — | Slack channel id + allowlisted user ids (token: `SLACK_BOT_TOKEN`) |
| `twilio_from` / `sms_to` / `sms_allowed_senders` | — | Twilio number, default recipient, allowlisted phone numbers (`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`) |
| `email_address` / `email_to` / `email_smtp_host` / `email_imap_host` / `email_allowed_senders` | — | Email gateway (password: `EMAIL_PASSWORD`; optional `*_port` keys) |
| `turn_token_budget` | off | Per-turn token cap; on exhaustion the model summarizes progress |
| `weak_model` / `weak_provider` | — | Small fast model for side tasks (summaries, compaction) |
| `repo_map` | `true` | Inject a ~1k-token repository map when cwd is a git repo |
| `send_cwd`, `send_os_shell`, `send_history_count` | — | Extra context sent in ask mode |

### Supported providers

Conch requires tool-calling-capable models on every provider; non-tool models
are rejected.

| Provider | Models | Cost |
|----------|--------|------|
| Cerebras | zai-glm-4.7 | Free |
| OpenAI | gpt-5.4 family, gpt-4.1 family, gpt-4o, o3, o4-mini, o1 (all tool-capable; o1-mini is not supported) | Paid |
| Anthropic | claude-opus-4-8, claude-sonnet-4-7, claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5 | Paid |
| Ollama | Discovered live from your server's `/api/tags`, filtered to models that advertise the `tools` capability | Free (local) |
| Custom | Any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp server, a second Ollama box) via `provider=custom` + `custom_base_url` + `custom_model`; verified tool-capable by a startup probe | Depends |

Ollama models are never hardcoded: `/models`, `/model`, `/provider ollama`,
and the fallback chain all use the live list from your server, and switching
to a model that isn't installed or doesn't support tool calling is rejected
with a clear message. Cloud providers are validated too: `/model` (and the
model-facing `conch_config` switches) reject names that aren't in the
provider's catalog and suggest close matches for typos ("Did you mean:
claude-sonnet-4-6?"). For a model newer than conch's catalog, use
`/model <name> --force` — it switches with a warning instead of validating.
Config-file model values get the same scrutiny at startup (warn, don't die). Older local servers are supported too: `/api/show`
requests are compatible with pre-rename servers, models whose tool
capability can't be determined stay listed (only affirmatively non-tool
models are excluded), ask mode falls back to the legacy `format="json"` on
Ollama < 0.5, and startup degrades to warnings (with a pull hint) instead of
dead-ending when the server is empty or unreachable.

## Features

### Streaming replies
Tokens stream to the terminal in real time with syntax-highlighted code blocks (via Pygments) and inline markdown formatting (bold, italic, headers, lists). When a local model (e.g. qwen2.5-coder) emits a tool call as plain content — bare JSON, `<tool_call>`/Claude XML, or a fenced json block — instead of a native `tool_calls` field, conch withholds that raw text from the terminal while it's being streamed, then recovers and executes it, so you never see the command printed instead of run. Ordinary replies that merely start with `{` or a code fence are still shown in full.

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
With `remote_enabled=true`, conch messages you proactively and you can steer it from anywhere: scheduled task output is delivered over your `notify_channel`, and inbound replies are polled (Slack bot channel, Twilio SMS, IMAP inbox) and routed into conversations — a channel thread *is* a conch conversation, so replies resume it. Safety is enforced in code, not prompts: inbound senders must be on a per-channel allowlist (no allowlist = no inbound, fail closed); remote sessions are capped at **safe_auto** permissions regardless of local agent mode (read-only commands run, everything else — including anything destructive — posts an approval request over the channel that you answer with `approve <id>` / `deny <id>`); and remote sessions never see self-management or delegation tools.

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
Switch between named tool presets: `/profile minimal` (shell only), `/profile dev` (GitHub, Jira), `/profile comms` (Gmail, Slack), `/profile full` (everything). Define your own in config with `profile_<name> = group1, group2`, or pick one per session with `tool_profile=<name>`. On Ollama the minimal profile is active by default (small models drown in big tool lists) — `/profile full` overrides. When more tools are available than the provider's cap, Conch sends the ones most relevant to your current message.

### Local-model context management
Every Ollama request sets `options.num_ctx` (default 32768, clamped to the model's max — no more silent 4k truncation) and `keep_alive` so the model stays warm. The system prompt stays byte-stable within a session so Ollama's KV prefix cache is reused; timestamps and recalled memories ride on the user message instead. Token estimates are calibrated against the real `prompt_eval_count` Ollama reports, a per-turn context gauge shows window usage (with a warning at 80%), and at ~70% full the model itself summarizes older history into a compact brief. Tool results are truncated to a budget scaled to the context window (head and tail kept) instead of a fixed char cap.

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
`conch-ask` no longer scrapes commands out of free text. Ollama is constrained to a `{"command": ...}` JSON schema via the `format` parameter; OpenAI, Anthropic, and Cerebras are forced into a single `shell_command` tool call.

## Chat commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/models` | List available models |
| `/model <name>` | Switch model (validated against the provider's catalog/server; suggests close matches on typos; `--force` bypasses for brand-new models) |
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
| `/search <query>` | Search conversations, memories, and config |
| `/browse` | Interactive conversation browser |
| `/<custom>` | Any markdown file in `~/.config/conch/commands/` |

## Development

```bash
python3 -m unittest discover -s tests
```

Tests covering rendering, message normalization, context compression and auto-compaction, error signaling, Ollama tool calling and model discovery, structured ask mode, tool profiles and selection, custom commands, project context, prompt overrides, shell approval and graded permissions, lifecycle hooks, executable user tools, custom providers, todo/plan tracking, subagent delegation, budgets and weak-model side tasks, FTS5 memory/search, repo maps, backend health, skills and skill-scoped subagents, channel gateways and the remote loop, tool visibility, and conversation handling.

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
