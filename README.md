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
| `ollama_num_ctx` | `32768` | Context window requested on every Ollama call (clamped to the model's max) |
| `ollama_keep_alive` | `10m` | How long Ollama keeps the model loaded between turns |
| `tool_profile` | — | Tool profile to apply for the session (see profiles below) |
| `profile_<name>` | — | Define a custom tool profile, e.g. `profile_research = github, jira` |
| `chat_prompt:<provider>/<model-glob>` | — | Path to a custom chat system-prompt template for matching models |
| `ask_prompt:<provider>/<model-glob>` | — | Same for ask mode |
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

Ollama models are never hardcoded: `/models`, `/model`, `/provider ollama`,
and the fallback chain all use the live list from your server, and switching
to a model that isn't installed or doesn't support tool calling is rejected
with a clear message.

## Features

### Streaming replies
Tokens stream to the terminal in real time with syntax-highlighted code blocks (via Pygments) and inline markdown formatting (bold, italic, headers, lists).

### MCP tools
Connect external tools via the [Model Context Protocol](https://modelcontextprotocol.io). Configure servers in `~/.config/conch/mcp.json`. Supports both stdio and HTTP transports.

### Local shell execution
The LLM can run shell commands on your machine. In normal mode, each command shows a prompt: **y**/Enter to run, **n** to decline (with optional feedback), **e** to edit the command first, **a** to always-allow that exact command for the session. Toggle `/agent` (or `/yolo`) for auto-execution. Command output streams live to your terminal.

### Memory
Conch remembers facts across sessions. Use `/remember` to save manually, or the LLM saves important context automatically via the `save_memory` tool.

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
| `/model <name>` | Switch model |
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

Tests covering rendering, message normalization, context compression and auto-compaction, error signaling, Ollama tool calling and model discovery, structured ask mode, tool profiles and selection, custom commands, project context, prompt overrides, shell approval, tool visibility, and conversation handling.

## Architecture

```
conch/
├── app.py           Main chat loop and CLI entrypoint
├── cli.py           One-shot ask entrypoint
├── commands.py      Slash command handlers
├── composio.py      Composio OAuth integration
├── config.py        Config file loading
├── conversations.py Conversation persistence
├── llm.py           Ask-mode LLM calls
├── mcp.py           MCP stdio + HTTP transport
├── memory.py        Persistent memory store
├── prompts.py       Provider-specific system prompts
├── providers.py     LLM provider adapters + streaming
├── render.py        Syntax highlighting + StreamPrinter
├── runtime.py       Chat turn logic, context compression
├── scheduler.py     Background task scheduler
└── tooling.py       Tool filtering, profiles, built-in tools
```
