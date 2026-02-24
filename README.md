# Conch

LLM-assisted shell with built-in DevOps and security expertise. Press a key, describe what you want, get a command. Multi-turn chat for general questions. No dependencies beyond Python 3.9+ stdlib.

Created by **Tom Hallaran**.

## Features

- **`ask`** — Describe what you want in plain language, get a shell command inserted on your line.
- **`chat`** — Multi-turn conversation with the LLM for general questions and follow-ups.
- **DevOps-aware** — Deep knowledge of kubectl, helm, terraform, AWS CLI, Vercel, npm, and infrastructure-as-code workflows. Auto-detects installed tools.
- **Security-aware** — Deep knowledge of nmap, nikto, sqlmap, hydra, nuclei, and 30+ security tools. Adapts suggestions based on what's installed.
- **Configurable LLM** — OpenAI, Anthropic, or Ollama. Swap models in one line.
- **Shell completions** — kubectl, helm, terraform, AWS, npm, argocd, istioctl, kustomize, k9s, Docker, git, and general zsh completions out of the box.
- **Zero deps** — Python stdlib only. No pip packages required.

## Install

```bash
git clone <repo-url> ~/conch
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

### `ask` — Get a shell command

```
$ ask find all python files larger than 1mb
→ find . -name "*.py" -size +1M

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

### Kubernetes, Terraform, AWS, Vercel & npm

Conch understands cloud-native and DevOps workflows:

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
│   ├── chat.py                # chat loop + one-shot mode
│   ├── config.py              # Config loader (file + defaults)
│   ├── llm.py                 # OpenAI/Anthropic/Ollama clients + tool detection
│   └── render.py              # Terminal markdown highlighting + spinner
├── shell/
│   ├── conch.zsh              # Zsh: ask, chat, key bindings, completions
│   └── conch.bash             # Bash: ask, key bindings
├── install.sh                 # One-command installer
├── config.example             # Example config file
└── pyproject.toml             # Optional pip install
```

- **`conch-ask`** sends your request + context (cwd, OS, installed tools) to the LLM and prints one command.
- **`conch-chat`** maintains conversation history for multi-turn Q&A with highlighted output and animated spinner.
- **Shell integration** uses `bindkey -s` to map shortcuts to the `ask`/`chat` functions. Completions for kubectl, helm, terraform, AWS, npm, argocd, istioctl, kustomize, k9s, Docker, git, and general commands are set up via `compinit`.
- **Tool detection** scans for 50+ DevOps, security, and development tools at each invocation so the LLM knows what's available.
- **No auto-execution** — commands are inserted on your line; you always press Enter to run.

## Requirements

- Python 3.9+ (stdlib only, no pip packages)
- Zsh 5+ or Bash 4+
- OpenAI/Anthropic API key, or Ollama running locally

## Author

Tom Hallaran

## License

MIT. See [LICENSE](LICENSE) for details.
