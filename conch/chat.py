"""Multi-turn chat with the configured LLM. Supports MCP tool calling."""
import datetime
import json
import os
import readline
import sys
import urllib.request
from typing import Any, Dict, List, Optional

from .config import load_config
from . import composio as composio_mod
from .memory import MemoryStore
from .render import Spinner, highlight
from . import mcp as mcp_mod

CHAT_SYSTEM_PROMPT = (
    "You are Conch, a helpful, concise assistant built into the user's shell. "
    "Answer clearly. Use markdown formatting sparingly — this is a terminal.\n\n"
    "About yourself (answer when the user asks what you can do, how to use you, etc.):\n"
    "- You are Conch, an LLM-assisted shell with two modes:\n"
    "  1. 'ask' — describe a task in plain English, get back one shell command placed on "
    "     the command line. Nothing runs until the user presses Enter.\n"
    "     Shortcuts: Ctrl+G, Ctrl+Space, or Esc Esc (press Escape twice).\n"
    "  2. 'chat' (this mode) — multi-turn conversation for general questions, explanations, "
    "     debugging help, architecture advice, and anything beyond a single command.\n"
    "     Shortcut: Ctrl+X then Ctrl+G. Type 'exit', 'quit', /q, or Ctrl+D to leave.\n"
    "- Tab completion is enabled for: kubectl, helm, terraform, aws, vercel, npm, argocd, "
    "  istioctl, kustomize, k9s, docker, git, and general commands.\n"
    "- You have deep expertise in: Kubernetes & container orchestration (kubectl, helm, "
    "  kustomize, argocd, istioctl, k9s, flux), Terraform & IaC, AWS CLI (50+ services), "
    "  Vercel deployments, npm/Node.js, Docker, git, and 30+ network security & vulnerability "
    "  assessment tools (nmap, nikto, sqlmap, hydra, nuclei, subfinder, etc.).\n"
    "- MCP tools: if tools are available, you can call them to take actions (create issues, "
    "  read files, search the web, manage infrastructure, etc.). Use tools when they would "
    "  help answer the user's request.\n"
    "- local_shell: you have a built-in tool that can run commands on the user's machine. "
    "  Use it for nmap, curl, docker, git, kubectl, file operations, etc. — anything that "
    "  needs to run locally. The user will confirm before execution. You can chain: run a "
    "  command locally, then use the output with other tools (e.g. scan a host, then email "
    "  the results via Gmail). Prefer local_shell over sandbox/remote code interpreters for "
    "  commands that need the user's local environment.\n"
    "- Configuration: ~/.config/conch/config or ~/.conchrc. Supports OpenAI, Anthropic, or Ollama.\n"
    "- MCP tools config: ~/.config/conch/mcp.json.\n"
    "- The user can switch models live in chat with /models and /model <name>.\n"
    "  /provider <name> switches the LLM provider (openai, anthropic, ollama).\n"
    "- manage_tools: you have a tool that can search and selectively load tools.\n"
    "  Some large tool groups (e.g. github with 800+ tools) are auto-disabled at start.\n"
    "  When you need tools from a disabled group, use this workflow:\n"
    "    1. manage_tools(action='search', query='repos org') — find the specific tools\n"
    "    2. manage_tools(action='enable_tools', tools=['GITHUB_LIST_REPOS_FOR_ORG', ...]) — load only those\n"
    "  For small groups (<50 tools) like gmail, use action='enable' to load the whole group.\n"
    "  NEVER enable a large group (github) entirely — always search and pick.\n"
    "- Connect services: /connect <app> authenticates services via Composio (e.g.\n"
    "  /connect gmail, /connect slack). /apps lists available services.\n"
    "  If the user asks for something that requires an unconnected service, suggest\n"
    "  they use /connect to set it up.\n"
    "- Memory: the user can save persistent memories with /remember <text>.\n"
    "  Saved memories are automatically included in context when relevant.\n"
    "  /memories lists all saved memories, /forget <id> removes one.\n"
    "  If the user shares a preference, fact, or instruction they'd clearly want\n"
    "  remembered across sessions, suggest they use /remember to save it.\n"
    "- Install/update: run install.sh from the conch directory.\n"
    "When answering about your capabilities, be specific and helpful."
)

MAX_TOOL_ROUNDS = 10

# ---------------------------------------------------------------------------
# Tool filtering — load all tools but only send enabled ones to the LLM.
# Preferences persist in ~/.local/state/conch/tool_prefs.json.
# ---------------------------------------------------------------------------

_TOOL_PREFS_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "conch", "tool_prefs.json")


def _load_tool_prefs() -> dict:
    try:
        with open(_TOOL_PREFS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_tool_prefs(prefs: dict):
    os.makedirs(os.path.dirname(_TOOL_PREFS_PATH), exist_ok=True)
    with open(_TOOL_PREFS_PATH, "w") as f:
        json.dump(prefs, f, indent=2)


def _tool_group(name: str, tool_map: dict) -> str:
    """Derive the group/source name for a tool.

    For Composio tools, split by prefix (GITHUB_*, GMAIL_*, SERPAPI_*, etc.)
    so each service can be enabled/disabled independently.
    """
    if name in ("local_shell", "manage_tools"):
        return name
    client = tool_map.get(name)
    client_name = getattr(client, "name", "unknown") if client else "unknown"
    if client_name == "composio" and "_" in name:
        return name.split("_")[0].lower()
    return client_name


def _group_tools(all_tools: List[dict], tool_map: dict) -> Dict[str, List[str]]:
    """Group tool names by their source."""
    groups: Dict[str, List[str]] = {}
    for t in all_tools:
        name = t["function"]["name"]
        grp = _tool_group(name, tool_map)
        groups.setdefault(grp, []).append(name)
    return groups


def _apply_filter(all_tools: List[dict], tool_map: dict,
                  prefs: dict) -> List[dict]:
    """Return tools whose group is enabled, plus any individually picked tools."""
    disabled = set(prefs.get("disabled_groups", []))
    picked = set(prefs.get("picked_tools", []))
    if not disabled:
        return all_tools
    result = []
    for t in all_tools:
        name = t["function"]["name"]
        grp = _tool_group(name, tool_map)
        if grp not in disabled or name in picked:
            result.append(t)
    return result

KNOWN_MODELS = {
    "openai": [
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4o",
        "gpt-4o-mini",
        "o4-mini",
        "o3",
        "o3-mini",
    ],
    "anthropic": [
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
    ],
    "ollama": [
        "llama4",
        "llama3.3",
        "deepseek-r1",
        "deepseek-v3",
        "qwen3",
        "qwen2.5-coder",
        "mistral",
        "gemma3",
        "phi4",
    ],
}


# ---------------------------------------------------------------------------
# Raw LLM calls — return structured dicts instead of plain strings so we can
# detect tool_calls and feed results back.
# ---------------------------------------------------------------------------

def _raw_openai(config: dict, messages: List[dict],
                tools: Optional[List[dict]] = None) -> dict:
    api_key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "").strip()
    if not api_key:
        print("conch: OPENAI_API_KEY not set", file=sys.stderr)
        return {"content": "", "tool_calls": None}

    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "gpt-4o-mini")),
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 16384,
    }
    if tools:
        body["tools"] = tools

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"content": f"[API error: {e}]", "tool_calls": None}

    msg = (data.get("choices") or [{}])[0].get("message", {})
    return {
        "role": "assistant",
        "content": (msg.get("content") or "").strip(),
        "tool_calls": msg.get("tool_calls"),
    }


def _raw_anthropic(config: dict, messages: List[dict],
                   tools: Optional[List[dict]] = None) -> dict:
    api_key = os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"), "").strip()
    if not api_key:
        print("conch: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return {"content": "", "tool_calls": None}

    system = ""
    user_messages: List[dict] = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"] if isinstance(m["content"], str) else str(m["content"])
        else:
            user_messages.append(m)

    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "claude-sonnet-4-6")),
        "max_tokens": 16384,
        "system": system,
        "messages": user_messages,
    }
    if tools:
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters",
                                                   {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"content": f"[API error: {e}]", "tool_calls": None}

    text_parts: List[str] = []
    tool_calls: List[dict] = []
    raw_content = data.get("content", [])
    for block in raw_content:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    return {
        "role": "assistant",
        "content": "\n".join(text_parts).strip(),
        "tool_calls": tool_calls if tool_calls else None,
        "_anthropic_content": raw_content,
    }


def _raw_ollama(config: dict, messages: List[dict],
                tools: Optional[List[dict]] = None) -> dict:
    base = (config.get("base_url") or
            os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "llama3.2")),
        "messages": messages,
        "stream": False,
    }
    if tools:
        body["tools"] = tools

    req = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"content": f"[Ollama error: {e}]", "tool_calls": None}

    msg = data.get("message", {})
    raw_tc = msg.get("tool_calls")
    tool_calls = None
    if raw_tc:
        tool_calls = []
        for i, tc in enumerate(raw_tc):
            fn = tc.get("function", {})
            tool_calls.append({
                "id": f"ollama_{i}",
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": json.dumps(fn.get("arguments", {})),
                },
            })

    return {
        "role": "assistant",
        "content": msg.get("content", "").strip(),
        "tool_calls": tool_calls,
    }


RAW_FNS = {
    "openai": _raw_openai,
    "anthropic": _raw_anthropic,
    "ollama": _raw_ollama,
}

DEFAULT_API_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": "",
}


def _handle_slash_command(cmd: str, config: dict, provider: str,
                          model_name: str,
                          memory: Optional["MemoryStore"] = None,
                          all_tools: Optional[List[dict]] = None,
                          tool_map: Optional[Dict[str, Any]] = None) -> Optional[tuple]:
    """Handle slash commands. Returns (provider, model, raw_fn) on change, None otherwise.

    Returns None if the command was handled but no model change occurred (or unknown command).
    """
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/help", "/h", "/?"):
        print(
            "\n\033[1;36mSlash commands:\033[0m\n"
            "  \033[1m/models\033[0m              List available models\n"
            "  \033[1m/model <name>\033[0m        Switch model (e.g. /model gpt-4o)\n"
            "  \033[1m/provider <name>\033[0m     Switch provider (openai, anthropic, ollama)\n"
            "  \033[1m/remember <text>\033[0m     Save a persistent memory\n"
            "  \033[1m/memories\033[0m            List all saved memories\n"
            "  \033[1m/forget <id>\033[0m         Delete a memory by ID\n"
            "  \033[1m/tools\033[0m               List loaded tools by source\n"
            "  \033[1m/enable <group>\033[0m      Enable a tool group (e.g. gmail, github)\n"
            "  \033[1m/disable <group>\033[0m     Disable a tool group\n"
            "  \033[1m/connect <app>\033[0m       Connect a service via Composio (e.g. gmail)\n"
            "  \033[1m/apps\033[0m                List connectable services\n"
            "  \033[1m/reload\033[0m              Reload MCP tools\n"
            "  \033[1m/help\033[0m                Show this help\n"
        )
        return None

    if command == "/remember" and memory is not None:
        if not arg:
            print("\n  \033[2mUsage: /remember <something to remember>\033[0m\n")
            return None
        entry = memory.add(arg)
        print(f"\n  \033[1;32m✓ Saved memory #{entry['id']}:\033[0m {entry['content']}\n")
        return None

    if command in ("/memories", "/mem") and memory is not None:
        all_mem = memory.get_all()
        if not all_mem:
            print("\n  \033[2mNo saved memories yet. Use /remember <text> to save one.\033[0m\n")
            return None
        print(f"\n  \033[1;36mSaved memories ({len(all_mem)}):\033[0m")
        for m in all_mem:
            print(f"    \033[1m#{m['id']}\033[0m  {m['content']}  \033[2m({m['created_at']})\033[0m")
        print(f"\n  \033[2mTip: /forget <id> to remove\033[0m\n")
        return None

    if command == "/forget" and memory is not None:
        if not arg:
            print("\n  \033[2mUsage: /forget <id>  (use /memories to see IDs)\033[0m\n")
            return None
        try:
            mid = int(arg.lstrip("#"))
        except ValueError:
            print(f"\n  \033[31mInvalid ID: {arg}\033[0m\n")
            return None
        if memory.forget(mid):
            print(f"\n  \033[1;32m✓ Forgot memory #{mid}\033[0m\n")
        else:
            print(f"\n  \033[31mNo memory with ID #{mid}\033[0m\n")
        return None

    if command in ("/models", "/ls"):
        print()
        for p, models in KNOWN_MODELS.items():
            marker = " \033[1;33m← active\033[0m" if p == provider else ""
            print(f"  \033[1;36m{p}\033[0m{marker}")
            for m in models:
                if m == model_name:
                    print(f"    \033[1;32m● {m}\033[0m  \033[2m(current)\033[0m")
                else:
                    print(f"    \033[2m○\033[0m {m}")
        print(f"\n  \033[2mTip: /model <name> to switch\033[0m\n")
        return None

    if command == "/model":
        if not arg:
            print(f"\n  \033[2mCurrent model:\033[0m \033[1m{model_name}\033[0m ({provider})\n"
                  f"  \033[2mUsage: /model <name>\033[0m\n")
            return None
        new_model = arg
        new_provider = provider
        for p, models in KNOWN_MODELS.items():
            if new_model in models:
                new_provider = p
                break
        new_fn = RAW_FNS.get(new_provider)
        if not new_fn:
            print(f"\n  \033[31mUnknown provider for model '{new_model}'\033[0m\n")
            return None
        if new_provider != provider:
            key_env = DEFAULT_API_KEY_ENVS.get(new_provider, "")
            if key_env and not os.environ.get(key_env, "").strip():
                print(f"\n  \033[31m{key_env} not set — cannot switch to {new_provider}\033[0m\n")
                return None
            config["provider"] = new_provider
            config["api_key_env"] = key_env
        config["chat_model"] = new_model
        config["model"] = new_model
        print(f"\n  \033[1;32mSwitched to {new_provider}/{new_model}\033[0m\n")
        return (new_provider, new_model, new_fn)

    if command == "/provider":
        if not arg:
            print(f"\n  \033[2mCurrent provider:\033[0m \033[1m{provider}\033[0m\n"
                  f"  \033[2mUsage: /provider <openai|anthropic|ollama>\033[0m\n")
            return None
        new_provider = arg.lower()
        if new_provider not in RAW_FNS:
            print(f"\n  \033[31mUnknown provider '{new_provider}'. "
                  f"Choose: openai, anthropic, ollama\033[0m\n")
            return None
        key_env = DEFAULT_API_KEY_ENVS.get(new_provider, "")
        if key_env and not os.environ.get(key_env, "").strip():
            print(f"\n  \033[31m{key_env} not set — cannot switch to {new_provider}\033[0m\n")
            return None
        new_model = KNOWN_MODELS[new_provider][0]
        new_fn = RAW_FNS[new_provider]
        config["provider"] = new_provider
        config["api_key_env"] = key_env
        config["chat_model"] = new_model
        config["model"] = new_model
        print(f"\n  \033[1;32mSwitched to {new_provider}/{new_model}\033[0m\n")
        return (new_provider, new_model, new_fn)

    if command == "/tools" and all_tools is not None and tool_map is not None:
        prefs = _load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))
        groups = _group_tools(all_tools, tool_map)
        active_tools = _apply_filter(all_tools, tool_map, prefs)
        print(f"\n  \033[1;36mTool groups ({len(active_tools)}/{len(all_tools)} active):\033[0m")
        for grp in sorted(groups.keys()):
            names = groups[grp]
            is_disabled = grp in disabled
            status = "\033[31m OFF\033[0m" if is_disabled else "\033[32m ON \033[0m"
            print(f"    {status}  \033[1m{grp:<20}\033[0m \033[2m{len(names)} tools\033[0m")
        print(f"\n  \033[2mTip: /disable <group> or /enable <group>\033[0m\n")
        return None

    if command == "/enable" and all_tools is not None:
        if not arg:
            print("\n  \033[2mUsage: /enable <group>  (see /tools for groups)\033[0m\n")
            return None
        prefs = _load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))
        target = arg.lower()
        if target == "all":
            disabled.clear()
            print(f"\n  \033[1;32m✓ All tool groups enabled\033[0m\n")
        elif target in disabled:
            disabled.discard(target)
            print(f"\n  \033[1;32m✓ Enabled {target}\033[0m\n")
        else:
            print(f"\n  \033[2m{target} is already enabled\033[0m\n")
            return None
        prefs["disabled_groups"] = sorted(disabled)
        _save_tool_prefs(prefs)
        return "reload_tools"

    if command == "/disable" and all_tools is not None and tool_map is not None:
        if not arg:
            print("\n  \033[2mUsage: /disable <group>  or  /disable all\033[0m\n")
            return None
        prefs = _load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))
        target = arg.lower()
        groups = _group_tools(all_tools, tool_map)
        if target == "all":
            disabled = set(groups.keys()) - {"local_shell", "manage_tools"}
            prefs["disabled_groups"] = sorted(disabled)
            _save_tool_prefs(prefs)
            total = sum(len(v) for k, v in groups.items() if k in disabled)
            print(f"\n  \033[1;32m✓ Disabled all tool groups ({total} tools)\033[0m\n")
            return "reload_tools"
        if target not in groups:
            print(f"\n  \033[31mUnknown group '{target}'. Use /tools to see groups.\033[0m\n")
            return None
        disabled.add(target)
        prefs["disabled_groups"] = sorted(disabled)
        _save_tool_prefs(prefs)
        count = len(groups[target])
        print(f"\n  \033[1;32m✓ Disabled {target} ({count} tools)\033[0m\n")
        return "reload_tools"

    if command == "/apps":
        if not composio_mod.is_available():
            print("\n  \033[31mCOMPOSIO_API_KEY not set. Add it to your .env or run install.sh.\033[0m\n")
            return None
        apps = composio_mod.list_apps()
        print(f"\n  \033[1;36mConnectable services ({len(apps)}):\033[0m")
        for slug, desc in apps:
            print(f"    \033[1m{slug:<20}\033[0m \033[2m{desc}\033[0m")
        print(f"\n  \033[2mTip: /connect <app> to authenticate\033[0m\n")
        return None

    if command == "/reload":
        return "reload_tools"

    if command == "/connect":
        if not composio_mod.is_available():
            print("\n  \033[31mCOMPOSIO_API_KEY not set. Add it to your .env or run install.sh.\033[0m\n")
            return None
        if not arg:
            print("\n  \033[2mUsage: /connect <app>  (e.g. /connect gmail)\033[0m\n"
                  "  \033[2mSee /apps for available services.\033[0m\n")
            return None
        app_slug = arg.lower().replace(" ", "_")
        print(f"\n  \033[2mConnecting {app_slug}...\033[0m")
        success, message = composio_mod.connect(app_slug)
        if success:
            print(f"  \033[1;32m✓ {message}\033[0m\n")
        else:
            print(f"  \033[31m✗ {message}\033[0m\n")
        return None

    return None


# ---------------------------------------------------------------------------
# Tool-call helpers — append results in the format each provider expects.
# ---------------------------------------------------------------------------

def _append_results_openai(messages: List[dict], response: dict,
                           results: List[dict]):
    assistant_msg: Dict[str, Any] = {
        "role": "assistant",
        "content": response.get("content") or None,
    }
    if response.get("tool_calls"):
        assistant_msg["tool_calls"] = response["tool_calls"]
    messages.append(assistant_msg)

    for r in results:
        messages.append({
            "role": "tool",
            "tool_call_id": r["id"],
            "content": r["content"],
        })


def _append_results_anthropic(messages: List[dict], response: dict,
                              results: List[dict]):
    messages.append({
        "role": "assistant",
        "content": response.get("_anthropic_content",
                                [{"type": "text", "text": response.get("content", "")}]),
    })
    messages.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
            for r in results
        ],
    })


# ---------------------------------------------------------------------------
# Built-in local shell tool — lets the LLM run commands on the host machine.
# ---------------------------------------------------------------------------

LOCAL_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "local_shell",
        "description": (
            "Execute a shell command on the user's local machine and return "
            "stdout + stderr. Use this for tasks that require running commands "
            "locally: nmap scans, file operations, git, docker, kubectl, etc. "
            "The user will be asked to confirm before the command runs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait (default 60)",
                },
            },
            "required": ["command"],
        },
    },
}


class _LocalShellClient:
    """Pseudo-MCP client that runs commands on the local machine."""

    name = "local_shell"

    def call_tool(self, name: str, arguments: dict) -> dict:
        import subprocess as _sp

        cmd = arguments.get("command", "")
        timeout = arguments.get("timeout", 60)
        if not cmd:
            return {"content": [{"type": "text", "text": "Error: empty command"}]}

        print(f"\n  \033[1;33m⚠ Run locally:\033[0m \033[1m{cmd}\033[0m")
        try:
            answer = input("  \033[1;33mExecute? [y/N]\033[0m ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            return {"content": [{"type": "text", "text": "User declined to execute the command."}]}

        try:
            result = _sp.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout,
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
            if not output:
                output = f"(no output, exit code {result.returncode})"
            elif result.returncode != 0:
                output += f"\n(exit code {result.returncode})"
            # Cap output to avoid blowing up context
            if len(output) > 15000:
                output = output[:15000] + "\n... (truncated)"
            return {"content": [{"type": "text", "text": output}]}
        except _sp.TimeoutExpired:
            return {"content": [{"type": "text", "text": f"Command timed out after {timeout}s"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}]}


_local_shell_client = _LocalShellClient()


# ---------------------------------------------------------------------------
# Built-in tool management — lets the LLM enable/disable tool groups.
# ---------------------------------------------------------------------------

MANAGE_TOOLS_TOOL = {
    "type": "function",
    "function": {
        "name": "manage_tools",
        "description": (
            "Search for and selectively load the tools you need. "
            "PREFERRED WORKFLOW for large groups like github (800+ tools):\n"
            "  1. action='search', query='repos org members' — find specific tools by keyword\n"
            "  2. action='enable_tools', tools=['GITHUB_LIST_REPOS', ...] — load only those\n"
            "This avoids loading 800+ tools when you only need 3-5.\n\n"
            "Other actions:\n"
            "  action='list' — show all tool groups and their ON/OFF status\n"
            "  action='enable', group='gmail' — enable an entire small group\n"
            "  action='disable', group='gmail' — disable a group"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "search", "enable_tools", "enable", "disable"],
                    "description": "The action to perform",
                },
                "group": {
                    "type": "string",
                    "description": "Tool group name for enable/disable (e.g. github, gmail)",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for action='search' — matches tool names and descriptions",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific tool names for action='enable_tools'",
                },
            },
            "required": ["action"],
        },
    },
}


class _ManageToolsClient:
    """Pseudo-MCP client that manages tool groups at runtime."""

    name = "manage_tools"

    def __init__(self):
        self._chat_state = None

    def bind(self, state: dict):
        """Bind to the chat loop's mutable state dict."""
        self._chat_state = state

    def call_tool(self, name: str, arguments: dict) -> dict:
        action = arguments.get("action", "list")
        group = arguments.get("group", "").lower()

        s = self._chat_state
        if not s:
            return {"content": [{"type": "text", "text": "Error: tool manager not initialized"}]}

        all_tools = s["all_tools"]
        tool_map = s["tool_map"]
        groups = _group_tools(all_tools, tool_map)
        prefs = _load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))

        if action == "list":
            lines = ["Tool groups:"]
            for grp in sorted(groups.keys()):
                status = "OFF" if grp in disabled else "ON"
                lines.append(f"  [{status}] {grp} ({len(groups[grp])} tools)")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        if action == "enable":
            if not group:
                return {"content": [{"type": "text", "text": "Error: specify a group name"}]}
            disabled.discard(group)
            prefs["disabled_groups"] = sorted(disabled)
            _save_tool_prefs(prefs)
            tools = _apply_filter(all_tools, tool_map, prefs)
            s["tools"] = tools
            s["needs_tool_refresh"] = True
            count = len(groups.get(group, []))
            return {"content": [{"type": "text", "text":
                f"Enabled {group} ({count} tools). {len(tools)} tools now active."}]}

        if action == "disable":
            if not group:
                return {"content": [{"type": "text", "text": "Error: specify a group name"}]}
            disabled.add(group)
            prefs["disabled_groups"] = sorted(disabled)
            _save_tool_prefs(prefs)
            tools = _apply_filter(all_tools, tool_map, prefs)
            s["tools"] = tools
            s["needs_tool_refresh"] = True
            count = len(groups.get(group, []))
            return {"content": [{"type": "text", "text":
                f"Disabled {group} ({count} tools removed). {len(tools)} tools now active."}]}

        if action == "search":
            query = arguments.get("query", "").lower()
            if not query:
                return {"content": [{"type": "text", "text": "Error: specify a search query"}]}
            keywords = query.split()
            scored: List[tuple] = []
            for t in all_tools:
                fn = t["function"]
                tname = fn["name"]
                name_lower = tname.lower()
                desc_lower = fn.get("description", "").lower()
                # Score: keyword hits in name count double
                name_hits = sum(1 for kw in keywords if kw in name_lower)
                desc_hits = sum(1 for kw in keywords if kw in desc_lower)
                if name_hits == 0 and desc_hits == 0:
                    continue
                score = (name_hits * 2 + desc_hits) / len(keywords)
                # Boost disabled groups (likely what the user needs)
                grp = _tool_group(tname, tool_map)
                if grp in disabled:
                    score += 1.0
                scored.append((tname, score, fn.get("description", "")[:80]))
            scored.sort(key=lambda x: x[1], reverse=True)
            matches = scored[:20]
            if not matches:
                return {"content": [{"type": "text", "text":
                    f"No tools found matching '{query}'. Try broader keywords."}]}
            lines = [f"Found {len(matches)} tools matching '{query}':"]
            for tname, score, desc in matches:
                lines.append(f"  {tname} — {desc}")
            lines.append("\nUse action='enable_tools' with the tool names you need.")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        if action == "enable_tools":
            tool_names = arguments.get("tools", [])
            if not tool_names:
                return {"content": [{"type": "text", "text": "Error: specify tool names"}]}
            valid = {t["function"]["name"] for t in all_tools}
            valid_lower = {n.lower(): n for n in valid}
            picked = set(prefs.get("picked_tools", []))
            added = []
            for tn in tool_names:
                clean = tn.split(" —")[0].split(" [")[0].strip()
                if clean in valid:
                    picked.add(clean)
                    added.append(clean)
                elif clean.lower() in valid_lower:
                    real = valid_lower[clean.lower()]
                    picked.add(real)
                    added.append(real)
            prefs["picked_tools"] = sorted(picked)
            _save_tool_prefs(prefs)
            tools = _apply_filter(all_tools, tool_map, prefs)
            s["tools"] = tools
            s["needs_tool_refresh"] = True
            return {"content": [{"type": "text", "text":
                f"Loaded {len(added)} tools: {', '.join(added)}. {len(tools)} tools now active."}]}

        return {"content": [{"type": "text", "text": f"Unknown action: {action}"}]}


_manage_tools_client = _ManageToolsClient()


# ---------------------------------------------------------------------------
# Core turn logic — calls the LLM, executes tools, loops until text reply.
# ---------------------------------------------------------------------------

def _chat_turn(config: dict, provider: str, raw_fn, messages: List[dict],
               tools: Optional[List[dict]], tool_map: Dict[str, Any],
               chat_state: Optional[dict] = None) -> str:
    for _ in range(MAX_TOOL_ROUNDS):
        # Pick up refreshed tools if manage_tools changed them
        if chat_state and chat_state.get("needs_tool_refresh"):
            tools = chat_state["tools"]
            chat_state["needs_tool_refresh"] = False

        with Spinner("Thinking"):
            response = raw_fn(config, messages, tools if tools else None)

        tool_calls = response.get("tool_calls")
        if not tool_calls:
            return response.get("content", "")

        results: List[dict] = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "unknown")
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            print(f"  \033[2m⚡ {name}\033[0m", file=sys.stderr)
            if name in ("local_shell", "manage_tools"):
                client = _local_shell_client if name == "local_shell" else _manage_tools_client
                raw_result = client.call_tool(name, arguments)
                result_text = raw_result.get("content", [{}])[0].get("text", "")
            else:
                with Spinner(f"Running {name}"):
                    result_text = mcp_mod.execute_tool(tool_map, name, arguments)
            # Cap individual tool results to prevent context blowup
            if len(result_text) > 8000:
                result_text = result_text[:8000] + "\n... (truncated — result too large)"
            results.append({"id": tc.get("id", ""), "content": result_text})

        if provider == "anthropic":
            _append_results_anthropic(messages, response, results)
        else:
            _append_results_openai(messages, response, results)

    return "[max tool call rounds reached]"


# ---------------------------------------------------------------------------
# Interactive loop and one-shot entry point.
# ---------------------------------------------------------------------------

def chat_loop():
    """Interactive multi-turn chat with optional MCP tool support."""
    config = load_config()
    provider = (config.get("provider") or "openai").lower()
    system_prompt = config.get("chat_system_prompt", CHAT_SYSTEM_PROMPT)
    now = datetime.datetime.now()
    system_prompt += (
        f"\n\nCurrent date and time: {now.strftime('%A, %B %d, %Y %I:%M %p')} "
        f"(timezone: {datetime.datetime.now(datetime.timezone.utc).astimezone().tzname()}). "
        f"Use this for any time-sensitive requests."
    )
    model_name = config.get("chat_model", config.get("model", ""))

    raw_fn = RAW_FNS.get(provider)
    if not raw_fn:
        print(f"conch: unknown provider {provider}", file=sys.stderr)
        sys.exit(1)

    mcp_clients = mcp_mod.create_clients()
    all_tools, tool_map = mcp_mod.collect_tools(mcp_clients)

    # Inject built-in tools
    all_tools.append(LOCAL_SHELL_TOOL)
    all_tools.append(MANAGE_TOOLS_TOOL)
    tool_map["local_shell"] = _local_shell_client
    tool_map["manage_tools"] = _manage_tools_client

    # Apply user's enable/disable preferences, auto-trim oversized groups
    tool_prefs = _load_tool_prefs()
    MAX_GROUP = 200
    MAX_TOOLS = 300
    groups = _group_tools(all_tools, tool_map)
    disabled = set(tool_prefs.get("disabled_groups", []))
    auto_disabled = []
    for grp, names in groups.items():
        if grp in disabled:
            continue
        if len(names) > MAX_GROUP:
            disabled.add(grp)
            auto_disabled.append((grp, len(names)))
    if auto_disabled:
        tool_prefs["disabled_groups"] = sorted(disabled)
        _save_tool_prefs(tool_prefs)
        for grp, cnt in auto_disabled:
            print(f"\033[33m  Auto-disabled {grp} ({cnt} tools — exceeds {MAX_GROUP} limit). "
                  f"Use /enable {grp} to override.\033[0m", file=sys.stderr)
    tools = _apply_filter(all_tools, tool_map, tool_prefs)
    if len(tools) > MAX_TOOLS:
        tools = tools[:MAX_TOOLS]

    # Shared state so manage_tools can update the active tool list mid-turn
    chat_state = {"all_tools": all_tools, "tool_map": tool_map,
                  "tools": tools, "needs_tool_refresh": False}
    _manage_tools_client.bind(chat_state)

    memory = MemoryStore()

    messages: List[dict] = [{"role": "system", "content": system_prompt}]

    # Set up readline for input history (up/down arrows) within this session.
    # Save and restore any pre-existing readline state so we don't clobber
    # the parent shell's history.
    _prev_history_len = readline.get_current_history_length()
    _history_file = os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
        "conch", "chat_history")
    os.makedirs(os.path.dirname(_history_file), exist_ok=True)
    try:
        readline.read_history_file(_history_file)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(500)

    print(f"\033[1;36mConch chat\033[0m \033[2m({provider}/{model_name})\033[0m")
    if all_tools:
        if len(tools) < len(all_tools):
            print(f"\033[2m{len(tools)}/{len(all_tools)} tools active (/tools to manage)\033[0m")
        else:
            print(f"\033[2m{len(tools)} tools available\033[0m")
    mem_count = len(memory.get_all())
    if mem_count:
        print(f"\033[2m{mem_count} memor{'y' if mem_count == 1 else 'ies'} loaded\033[0m")
    print(f"\033[2mType 'exit' or Ctrl+D to quit. /help for commands.\033[0m\n")

    try:
        while True:
            try:
                user_input = input("\033[1;33myou:\033[0m ")
            except (EOFError, KeyboardInterrupt):
                print("\n")
                break
            if not user_input.strip():
                continue
            if user_input.strip().lower() in ("exit", "quit", "/q"):
                break

            stripped = user_input.strip()

            if stripped.startswith("/"):
                result = _handle_slash_command(
                    stripped, config, provider, model_name, memory=memory,
                    all_tools=all_tools, tool_map=tool_map)
                if result == "reload_tools":
                    print("  \033[2mReloading MCP tools...\033[0m")
                    mcp_mod.close_all(mcp_clients)
                    mcp_clients = mcp_mod.create_clients()
                    all_tools, tool_map = mcp_mod.collect_tools(mcp_clients)
                    all_tools.append(LOCAL_SHELL_TOOL)
                    all_tools.append(MANAGE_TOOLS_TOOL)
                    tool_map["local_shell"] = _local_shell_client
                    tool_map["manage_tools"] = _manage_tools_client
                    tool_prefs = _load_tool_prefs()
                    tools = _apply_filter(all_tools, tool_map, tool_prefs)
                    chat_state.update({"all_tools": all_tools, "tool_map": tool_map,
                                       "tools": tools, "needs_tool_refresh": False})
                    print(f"  \033[1;32m{len(tools)}/{len(all_tools)} tools active\033[0m\n")
                elif result is not None:
                    provider, model_name, raw_fn = result
                continue

            # Inject relevant persistent memories into the system prompt
            mem_context = memory.build_context(user_input)
            if mem_context:
                messages[0]["content"] = system_prompt + "\n\n" + mem_context
            else:
                messages[0]["content"] = system_prompt

            messages.append({"role": "user", "content": user_input})
            reply = _chat_turn(config, provider, raw_fn, messages, tools, tool_map,
                               chat_state=chat_state)
            # Pick up any tools changes made by manage_tools during the turn
            tools = chat_state["tools"]
            if reply:
                messages.append({"role": "assistant", "content": reply})
                print(f"\n\033[1;36massistant:\033[0m\n{highlight(reply)}\n")
            else:
                print("\n\033[2m[no response]\033[0m\n")
    finally:
        try:
            readline.write_history_file(_history_file)
        except OSError:
            pass
        mcp_mod.close_all(mcp_clients)


def main():
    """Entry point for bin/conch-chat."""
    if len(sys.argv) > 1:
        config = load_config()
        provider = (config.get("provider") or "openai").lower()
        raw_fn = RAW_FNS.get(provider)
        if not raw_fn:
            print(f"conch: unknown provider {provider}", file=sys.stderr)
            sys.exit(1)

        mcp_clients = mcp_mod.create_clients()
        tools, tool_map = mcp_mod.collect_tools(mcp_clients)
        tools.append(LOCAL_SHELL_TOOL)
        tool_map["local_shell"] = _local_shell_client

        system_prompt = config.get("chat_system_prompt", CHAT_SYSTEM_PROMPT)
        now = datetime.datetime.now()
        system_prompt += (
            f"\n\nCurrent date and time: {now.strftime('%A, %B %d, %Y %I:%M %p')} "
            f"(timezone: {datetime.datetime.now(datetime.timezone.utc).astimezone().tzname()}). "
            f"Use this for any time-sensitive requests."
        )
        user_text = " ".join(sys.argv[1:])
        memory = MemoryStore()
        mem_context = memory.build_context(user_text)
        if mem_context:
            system_prompt += "\n\n" + mem_context
        messages: List[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        try:
            reply = _chat_turn(config, provider, raw_fn, messages, tools, tool_map)
            if reply:
                print(highlight(reply))
            else:
                print("[no response]", file=sys.stderr)
                sys.exit(1)
        finally:
            mcp_mod.close_all(mcp_clients)
    else:
        chat_loop()
