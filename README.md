# Conch

An LLM in your shell. Describe any task in plain English — file operations, text processing, networking, DevOps, security, whatever — and get the right command back. Works with every command on your system.

Created by **Tom Hallaran**.

## Features

- **`ask`** — Describe any task in plain English, get the shell command for it. Works for everything: `find`, `grep`, `awk`, `sed`, `curl`, `tar`, `rsync`, `ffmpeg`, `jq`, `xargs`, pipes, redirects — any command your shell can run.
- **`chat`** — Multi-turn conversation with the LLM for general questions, explanations, and follow-ups. Supports **MCP tool calling** — connect any MCP server (Composio, filesystem, GitHub, Jira/Confluence, custom) and the LLM can use those tools during chat.
- **Understands your system** — Auto-detects 50+ installed tools and adapts suggestions to what you actually have. Knows your OS, shell, current directory, and current date/time.
- **DevOps expertise** — Deep knowledge of kubectl, helm, terraform, AWS CLI, Vercel, npm, Docker, git, and infrastructure-as-code workflows.
- **Security expertise** — Deep knowledge of nmap, nikto, sqlmap, hydra, nuclei, and 30+ security/networking tools.
- **Configurable LLM** — OpenAI, Anthropic, or Ollama. Swap models in one line.
- **Shell completions** — kubectl, helm, terraform, AWS, npm, argocd, istioctl, kustomize, k9s, Docker, git, and general zsh completions out of the box.
- **Self-aware** — Ask Conch what it can do, what shortcuts it has, or how to configure it, and it will tell you.
- **Time-aware** — Always knows the current date and time for scheduling, flight lookups, time-sensitive queries.
- **Zero deps** — Python stdlib only. No pip packages required.

## Install

```bash
git clone git@github.com:detournement/conch.git ~/conch
cd ~/conch
./install.sh
```

The installer will:
- Check for Python 3
- Prompt for your OpenAI API key (or skip for later)
- Make scripts executable
- Add Conch to your `.zshrc` (or `.bashrc`)
- Run a quick test

Then open a **new terminal**.

## Usage

### `ask` — Get any shell command

```
$ ask find all python files larger than 1mb
→ find . -name "*.py" -size +1M

$ ask replace all tabs with spaces in every .js file
→ find . -name "*.js" -exec sed -i '' 's/\t/    /g' {} +

$ ask compress this directory into a tar.gz excluding node_modules
→ tar czf archive.tar.gz --exclude='node_modules' .

$ ask show disk usage sorted by size
→ du -sh * | sort -rh

$ ask download this url and extract all email addresses
→ curl -s https://example.com | grep -oE '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

$ ask scan localhost for open ports and vulnerabilities
→ nmap -sS -sV -O --script vuln localhost
```

**Keyboard shortcuts:**

| Key | Action |
|---|---|
| **Ctrl+G** | Runs `ask` (works in all terminals) |
| **Ctrl+Space** | Runs `ask` |
| **Esc Esc** | Runs `ask` (press Escape twice quickly) |

The suggested command is placed on your command line — press Enter to run it, or edit first. Nothing executes without your confirmation.

### `chat` — Multi-turn conversation

```
$ chat
Conch chat (openai/gpt-4o-mini). Type 'exit' or Ctrl+D to quit.

you: what is a kubernetes pod?
assistant: A pod is the smallest deployable unit in Kubernetes...

you: how does it differ from a container?
assistant: A pod can contain one or more containers...

you: exit
```

One-shot mode:

```
$ chat what is a Dockerfile?
A Dockerfile is a text document that contains all the commands...
```

**Keyboard shortcut:** **Ctrl+X then Ctrl+G** starts `chat`.

**Slash commands** (type these during chat):

| Command | Action |
|---|---|
| **/models** | List available models for all providers |
| **/model \<name\>** | Switch model (auto-detects provider) |
| **/provider \<name\>** | Switch provider (openai, anthropic, ollama) |
| **/help** | Show available commands |

```
you: /models
  openai ← active
    ○ gpt-4o
    ● gpt-4o-mini  (current)
    ○ gpt-4.1
    ...
  anthropic
    ○ claude-sonnet-4-6-20250929
    ...

you: /model claude-sonnet-4-6-20250929
  Switched to anthropic/claude-sonnet-4-6-20250929
```

When MCP tools are configured (see below), chat can also call external tools — search the web, read files, run code, and more.

### DevOps: Kubernetes, Terraform, AWS, Vercel & npm

Conch also has deep knowledge of cloud-native and DevOps workflows:

```
$ ask list all pods in kube-system namespace
→ kubectl get pods -n kube-system -o wide

$ ask scale web deployment to 5 replicas in production
→ kubectl scale deployment web --replicas=5 -n production

$ ask show all EKS clusters in us-east-1
→ aws eks list-clusters --region us-east-1 --query "clusters[]" --output table

$ ask deploy to vercel production with env from .env
→ vercel deploy --prod --env-file .env

$ ask create a terraform plan
→ terraform plan -out=plan.tfplan

$ ask install deps and check for vulnerabilities
→ npm install && npm audit
```

**Tab-completion** is enabled for all of these — type `kubectl get p<Tab>` and you'll see completions for pods, pv, pvc, etc.

### Security & vulnerability assessment

Deep knowledge of 30+ security tools. Auto-detects which are installed and picks the best available:

```
$ ask check SSL vulnerabilities on example.com
→ nmap --script ssl-enum-ciphers -p 443 example.com

$ ask enumerate subdomains for example.com
→ subfinder -d example.com

$ ask test for sql injection on http://target.com/login?id=1
→ sqlmap -u "http://target.com/login?id=1" --batch --risk=3 --level=5
```

If a preferred tool isn't installed, Conch uses the best alternative and notes what to install.

**Recommended DevOps tools** (install with `brew install`):

```
kubectl helm kustomize k9s kubectx argocd istioctl
terraform awscli vercel-cli
```

**Recommended security tools** (install with `brew install`):

```
nmap nikto gobuster ffuf sqlmap hydra masscan nuclei
subfinder amass john-jumbo hashcat mtr socat testssl
arp-scan wireshark
```

## MCP Tools

Conch's `chat` mode supports [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) tools. Connect any MCP-compatible server and the LLM can call those tools during conversation — read files, create GitHub issues, search the web, manage infrastructure, and more.

### Setup

Create `~/.config/conch/mcp.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/projects"]
    },
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_your_token"
      }
    },
    "composio": {
      "type": "http",
      "url": "https://backend.composio.dev/v3/mcp/YOUR_SERVER_ID/mcp?user_id=YOUR_USER_ID"
    }
  }
}
```

Two transport types:
- **`stdio`** — spawns a local process (e.g. `npx @modelcontextprotocol/server-*`)
- **`http`** — connects to a remote HTTP endpoint (e.g. [Composio](https://composio.dev/))

### Usage

When MCP tools are configured, `chat` loads them automatically:

```
$ chat
Conch chat (openai/gpt-4o-mini)
126 MCP tools available
Type 'exit' or Ctrl+D to quit.

you: search the web for kubernetes 1.32 release notes
  ⚡ COMPOSIO_SEARCH_WEB
assistant: Kubernetes 1.32 "Penelope" was released December 11, 2024...

you: list the files in my project directory
  ⚡ list_directory
assistant: Here are the files in /Users/you/projects...

you: read the README and summarize it
  ⚡ read_text_file
assistant: The README describes...
```

The LLM decides when to call tools based on your request. Tool calls show as `⚡ tool_name` in the output.

### Jira & Confluence

[mcp-atlassian](https://github.com/sooperset/mcp-atlassian) provides 65+ tools for Jira and Confluence — search issues with JQL, create and update tickets, transition statuses, search Confluence with CQL, read and edit pages, and more. Supports both Cloud and Server/Data Center deployments.

To set it up:

1. Create an API token at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Add to `~/.config/conch/mcp.json`:

```json
{
  "mcpServers": {
    "atlassian": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-atlassian"],
      "env": {
        "JIRA_URL": "https://your-company.atlassian.net",
        "JIRA_USERNAME": "your.email@company.com",
        "JIRA_API_TOKEN": "your_api_token",
        "CONFLUENCE_URL": "https://your-company.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "your.email@company.com",
        "CONFLUENCE_API_TOKEN": "your_api_token"
      }
    }
  }
}
```

> For Server/Data Center, use `JIRA_PERSONAL_TOKEN` instead of `JIRA_USERNAME` + `JIRA_API_TOKEN`.

You can configure just Jira or just Confluence — only include the env vars for the products you use.

Then in `chat`:

```
you: find issues assigned to me in the PROJ project
  ⚡ jira_search
assistant: Here are your open issues in PROJ...

you: transition PROJ-123 to Done
  ⚡ jira_transition_issue
assistant: PROJ-123 has been moved to Done.

you: search confluence for onboarding docs
  ⚡ confluence_search
assistant: Found 3 pages matching "onboarding"...
```

### Composio

[Composio](https://docs.composio.dev/docs/tools-and-toolkits) provides 1000+ tools across GitHub, Slack, Jira, Gmail, and more via a single MCP endpoint. Conch uses Composio's no-auth toolkits out of the box — web search, news, code interpreter, web scraping, and more.

To set it up:

1. Sign up at [composio.dev](https://composio.dev/) and get your API key
2. Create an MCP server via the [dashboard](https://platform.composio.dev/?next_page=/mcp-configs) or API:

```bash
curl -X POST https://backend.composio.dev/api/v3/mcp/servers \
  -H "x-api-key: YOUR_COMPOSIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "conch-tools", "auth_config_ids": [], "no_auth_apps": ["serpapi", "composio_search", "codeinterpreter", "firecrawl", "tavily"]}'
```

3. Copy the `mcp_url` from the response (append `/mcp?user_id=conch` to it) and add it to `~/.config/conch/mcp.json`

This gives you 100+ tools including web search, news, code execution, web scraping, flight search, finance data, and more.

See `mcp.example.json` in this repo for a full example config.

## Configuration

Config file (first found wins): `$CONCH_CONFIG`, `~/.config/conch/config`, or `~/.conchrc`.

```ini
# LLM provider: openai | anthropic | ollama
provider = openai
api_key_env = OPENAI_API_KEY
model = gpt-4o-mini

# For chat (optional — falls back to model above)
# chat_model = gpt-4o

# For Ollama (local, no API key)
# provider = ollama
# base_url = http://localhost:11434
# model = llama3.2

# Context sent to the LLM
send_cwd = true
send_os_shell = true
send_history_count = 0
```

API key is stored in `$CONCH_DIR/.env` (created by the installer, chmod 600, gitignored).

## Keyboard shortcuts (summary)

| Shortcut | Action |
|---|---|
| **Ctrl+G** | `ask` — get a shell command |
| **Ctrl+Space** | `ask` — same |
| **Esc Esc** | `ask` — same (press twice quickly) |
| **Ctrl+X Ctrl+G** | `chat` — multi-turn conversation |

## How it works

```
conch/
├── bin/
│   ├── conch-ask              # CLI: request → one shell command
│   ├── conch-chat             # CLI: multi-turn chat
│   └── conch-run-with-timeout # Subprocess timeout wrapper
├── conch/
│   ├── cli.py                 # ask entrypoint (25s process timeout)
│   ├── chat.py                # chat loop + MCP tool calling
│   ├── config.py              # Config loader (file + defaults)
│   ├── llm.py                 # OpenAI/Anthropic/Ollama clients + tool detection
│   ├── mcp.py                 # MCP client (stdio + HTTP transports)
│   └── render.py              # Terminal markdown highlighting + spinner
├── shell/
│   ├── conch.zsh              # Zsh: ask, chat, key bindings, completions
│   └── conch.bash             # Bash: ask, key bindings
├── install.sh                 # One-command installer
├── config.example             # Example config file
├── mcp.example.json           # Example MCP server config
└── pyproject.toml             # Optional pip install
```

- **`conch-ask`** sends your request + context (cwd, OS, installed tools) to the LLM and returns one command. Works for any shell command — not limited to specific tools.
- **`conch-chat`** maintains conversation history for multi-turn Q&A with highlighted output, animated spinner, and MCP tool execution.
- **Shell integration** uses `bindkey -s` to map shortcuts to the `ask`/`chat` functions. Completions for kubectl, helm, terraform, AWS, npm, argocd, istioctl, kustomize, k9s, Docker, git, and general commands are set up via `compinit`.
- **Tool detection** scans for 50+ DevOps, security, and development tools at each invocation so the LLM knows what's available.
- **MCP integration** connects to any MCP server (stdio or HTTP) for tool calling in chat mode. Config in `~/.config/conch/mcp.json`.
- **No auto-execution** — commands are inserted on your line; you always press Enter to run.

## Requirements

- Python 3.9+ (stdlib only, no pip packages)
- Zsh 5+ or Bash 4+
- OpenAI/Anthropic API key, or Ollama running locally

## Author

Tom Hallaran

## License

MIT. See [LICENSE](LICENSE) for details.
