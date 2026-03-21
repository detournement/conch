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

Conch reads config from `~/.config/conch/config`:

```ini
provider=cerebras
model=zai-glm-4.7
chat_model=zai-glm-4.7
api_key_env=CEREBRAS_API_KEY
```

Switch providers at any time in chat with `/provider openai`, `/provider anthropic`, or `/provider ollama`.

### Supported providers

| Provider | Models | Cost |
|----------|--------|------|
| Cerebras | zai-glm-4.7 | Free |
| OpenAI | gpt-4.1, gpt-4.1-mini, gpt-4o, o3, o4-mini | Paid |
| Anthropic | claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5 | Paid |
| Ollama | llama4, llama3.3, deepseek-r1, qwen3, mistral | Free (local) |

## Features

### Streaming replies
Tokens stream to the terminal in real time with syntax-highlighted code blocks (via Pygments) and inline markdown formatting (bold, italic, headers, lists).

### MCP tools
Connect external tools via the [Model Context Protocol](https://modelcontextprotocol.io). Configure servers in `~/.config/conch/mcp.json`. Supports both stdio and HTTP transports.

### Local shell execution
The LLM can run shell commands on your machine. In normal mode, you confirm each command. Toggle `/agent` for auto-execution.

### Memory
Conch remembers facts across sessions. Use `/remember` to save manually, or the LLM saves important context automatically via the `save_memory` tool.

### Conversations
Full conversation persistence with `/new`, `/switch`, `/convos`, `/delete`, and `/clear`. Titles are set automatically from your first message.

### Tool profiles
Switch between named tool presets: `/profile minimal` (shell only), `/profile dev` (GitHub, Jira), `/profile comms` (Gmail, Slack), `/profile full` (everything).

### Composio integration
Connect OAuth services like Gmail, GitHub, and Slack with `/connect <app>`. Uses the Composio API for real OAuth flows.

### Scheduling
Run recurring prompts with `/schedule 10m check disk usage` or natural language like `/schedule daily email report`.

### Cost tracking
See token usage and estimated cost per turn and per session. `/cost` for session totals.

### Background input
Type your next message while the LLM is still working — it queues and runs next. Toggle with `/queue`.

### Automatic retry
Transient API errors (429, 5xx) are retried once with a 1-second backoff before falling through to the provider fallback chain.

## Chat commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/models` | List available models |
| `/model <name>` | Switch model |
| `/provider <name>` | Switch provider |
| `/agent` | Toggle agent mode (auto-execute shell) |
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
| `/rounds <n>` | Set max tool call rounds |
| `/queue` | Toggle typeahead input |
| `/reload` | Reload MCP tools |

## Development

```bash
python3 -m unittest discover -s tests
```

51 tests covering rendering, message normalization, context compression, tool profiles, and conversation handling.

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
