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
    "- Configuration: ~/.config/conch/config or ~/.conchrc. Supports OpenAI, Anthropic, or Ollama.\n"
    "- MCP tools config: ~/.config/conch/mcp.json.\n"
    "- The user can switch models live in chat with /models and /model <name>.\n"
    "  /provider <name> switches the LLM provider (openai, anthropic, ollama).\n"
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
        "max_tokens": 1024,
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
        "max_tokens": 1024,
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
                          memory: Optional["MemoryStore"] = None) -> Optional[tuple]:
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
            "  \033[1m/connect <app>\033[0m       Connect a service via Composio (e.g. gmail)\n"
            "  \033[1m/apps\033[0m                List connectable services\n"
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
# Core turn logic — calls the LLM, executes tools, loops until text reply.
# ---------------------------------------------------------------------------

def _chat_turn(config: dict, provider: str, raw_fn, messages: List[dict],
               tools: Optional[List[dict]], tool_map: Dict[str, Any]) -> str:
    for _ in range(MAX_TOOL_ROUNDS):
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
            with Spinner(f"Running {name}"):
                result_text = mcp_mod.execute_tool(tool_map, name, arguments)
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
    tools, tool_map = mcp_mod.collect_tools(mcp_clients)

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
    if tools:
        print(f"\033[2m{len(tools)} MCP tool{'s' if len(tools) != 1 else ''} available\033[0m")
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
            slash_input = None
            if stripped.startswith("/"):
                slash_input = stripped
            else:
                first_word = stripped.split()[0].lower() if stripped else ""
                if first_word in ("models", "model", "provider", "help", "ls",
                                  "remember", "memories", "mem", "forget",
                                  "connect", "apps"):
                    slash_input = "/" + stripped

            if slash_input is not None:
                result = _handle_slash_command(
                    slash_input, config, provider, model_name, memory=memory)
                if result is not None:
                    provider, model_name, raw_fn = result
                continue

            # Inject relevant persistent memories into the system prompt
            mem_context = memory.build_context(user_input)
            if mem_context:
                messages[0]["content"] = system_prompt + "\n\n" + mem_context
            else:
                messages[0]["content"] = system_prompt

            messages.append({"role": "user", "content": user_input})
            reply = _chat_turn(config, provider, raw_fn, messages, tools, tool_map)
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
