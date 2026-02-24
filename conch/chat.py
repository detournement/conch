"""Multi-turn chat with the configured LLM. Supports MCP tool calling."""
import datetime
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional

from .config import load_config
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
    "- The user can switch models by editing the config file (model = <model-name>).\n"
    "- Install/update: run install.sh from the conch directory.\n"
    "When answering about your capabilities, be specific and helpful."
)

MAX_TOOL_ROUNDS = 10


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
        "model": config.get("chat_model", config.get("model", "claude-3-5-haiku-20241022")),
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

    messages: List[dict] = [{"role": "system", "content": system_prompt}]

    print(f"\033[1;36mConch chat\033[0m \033[2m({provider}/{model_name})\033[0m")
    if tools:
        print(f"\033[2m{len(tools)} MCP tool{'s' if len(tools) != 1 else ''} available\033[0m")
    print(f"\033[2mType 'exit' or Ctrl+D to quit.\033[0m\n")

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

            messages.append({"role": "user", "content": user_input})
            reply = _chat_turn(config, provider, raw_fn, messages, tools, tool_map)
            if reply:
                messages.append({"role": "assistant", "content": reply})
                print(f"\n\033[1;36massistant:\033[0m\n{highlight(reply)}\n")
            else:
                print("\n\033[2m[no response]\033[0m\n")
    finally:
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
        messages: List[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": " ".join(sys.argv[1:])},
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
