"""LLM clients: OpenAI, Anthropic, Cerebras, Ollama. Return single command string.

Ask mode uses structured output everywhere (plan 0.6): Ollama gets a JSON
schema via the ``format`` parameter; OpenAI, Anthropic, and Cerebras get a
forced single ``shell_command`` tool call. There is no free-text command
scraping — no regexes, no shell-prefix heuristics.
"""
import datetime
import json
import os
import sys
from typing import List, Optional, Tuple

from .config import load_config, get_bool, get_int


# JSON schema for the one piece of data ask mode needs back.
COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The single shell command to run",
        },
    },
    "required": ["command"],
}

# Forced tool call used on providers with native tool calling.
SHELL_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "shell_command",
        "description": "Return the single shell command that satisfies the user's request.",
        "parameters": COMMAND_SCHEMA,
    },
}


def parse_command_json(text: str) -> str:
    """Extract the command from a structured ``{"command": ...}`` response."""
    if not text or not text.strip():
        return ""
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict) and isinstance(data.get("command"), str):
        return data["command"].strip()
    return ""


def command_from_tool_calls(message: dict) -> str:
    """Extract the command from an OpenAI-shaped forced tool call response."""
    for tc in message.get("tool_calls") or []:
        fn = (tc or {}).get("function", {})
        if fn.get("name") != "shell_command":
            continue
        arguments = fn.get("arguments", "")
        if isinstance(arguments, dict):
            cmd = arguments.get("command", "")
            return cmd.strip() if isinstance(cmd, str) else ""
        return parse_command_json(arguments)
    return ""



DETECTED_TOOLS = [
    # Security / networking
    "nmap", "nikto", "gobuster", "ffuf", "dirb", "sqlmap", "hydra", "medusa",
    "testssl.sh", "testssl", "openssl", "tcpdump", "tshark", "wireshark",
    "netcat", "nc", "ncat", "masscan", "nuclei", "subfinder", "amass",
    "dig", "nslookup", "whois", "host", "searchsploit", "msfconsole",
    "enum4linux", "smbclient", "rpcclient", "arp-scan", "traceroute", "mtr",
    "john", "hashcat", "aircrack-ng", "curl", "wget", "socat",
    # Kubernetes / containers
    "kubectl", "helm", "kustomize", "k9s", "kubectx", "kubens",
    "argocd", "istioctl", "flux", "docker", "docker-compose",
    # IaC / cloud
    "terraform", "aws", "vercel",
    # Node / frontend
    "npm", "node", "npx", "yarn", "pnpm",
    # Git
    "git",
]


def _detect_tools() -> str:
    """Detect which DevOps, security, and dev tools are installed."""
    import shutil
    available = []
    missing = []
    for tool in DETECTED_TOOLS:
        if shutil.which(tool):
            available.append(tool)
        else:
            missing.append(tool)
    if not available:
        return ""
    parts = [f"Available tools: {', '.join(available)}"]
    if missing:
        parts.append(f"Not installed: {', '.join(missing)}")
    return "\n".join(parts)


def build_messages(config: dict, user_request: str, context: dict) -> Tuple[List[dict], str]:
    """Build OpenAI-style messages and system prompt."""
    from .prompts import get_ask_prompt
    provider = (config.get("provider") or "openai").lower()
    model = config.get("model", "")
    system = config.get("system_prompt") or get_ask_prompt(provider, model, config)
    parts = [user_request]
    now = datetime.datetime.now()
    parts.append(f"(Current date/time: {now.strftime('%Y-%m-%d %H:%M %Z').strip()})")
    if context.get("cwd"):
        parts.append(f"(Current directory: {context['cwd']})")
    if context.get("os_shell"):
        parts.append(f"(Environment: {context['os_shell']})")
    if context.get("history"):
        parts.append(f"(Recent commands:\n{context['history']})")
    tools_info = _detect_tools()
    if tools_info:
        parts.append(f"({tools_info})")
    user_content = "\n".join(parts)
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content}], user_content


def call_cerebras(config: dict, messages: list) -> str:
    import urllib.request

    api_key = os.environ.get(config.get("api_key_env", "CEREBRAS_API_KEY"), "").strip()
    if not api_key:
        print("conch: CEREBRAS_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    base = (config.get("base_url") or
            os.environ.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")).rstrip("/")
    body = {
        "model": config.get("model", "gpt-oss-120b"),
        "messages": messages,
        "temperature": 0.2,
        "max_completion_tokens": 2048,
        "clear_thinking": True,
        "tools": [SHELL_COMMAND_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "shell_command"}},
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "conch/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"conch: API error: {e}", file=sys.stderr)
        sys.exit(1)
    msg = (data.get("choices") or [{}])[0].get("message", {})
    return command_from_tool_calls(msg)


def call_bedrock(config: dict, messages: list) -> str:
    import urllib.error
    import urllib.request

    from .providers import format_http_api_error, get_bedrock_base_url

    api_key = os.environ.get(
        config.get("api_key_env", "AWS_BEARER_TOKEN_BEDROCK"), ""
    ).strip()
    if not api_key:
        print("conch: AWS_BEARER_TOKEN_BEDROCK not set", file=sys.stderr)
        sys.exit(1)
    body = {
        "model": config.get("model", "moonshotai.kimi-k2.5"),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 2048,
        "tools": [SHELL_COMMAND_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "shell_command"}},
    }
    req = urllib.request.Request(
        f"{get_bedrock_base_url(config)}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "conch/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"conch: API error: {format_http_api_error(e)}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"conch: API error: {e}", file=sys.stderr)
        sys.exit(1)
    msg = (data.get("choices") or [{}])[0].get("message", {})
    return command_from_tool_calls(msg)


def call_openrouter(config: dict, messages: list) -> str:
    import urllib.error
    import urllib.request

    from .providers import OPENROUTER_BASE_URL, format_http_api_error

    api_key = os.environ.get(
        config.get("api_key_env", "OPENROUTER_API_KEY"), ""
    ).strip()
    if not api_key:
        print("conch: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    body = {
        "model": config.get("model", "moonshotai/kimi-k3"),
        "messages": messages,
        "max_tokens": 2048,
        # Same rationale as chat mode (_openrouter_body): low effort, no
        # temperature keeps Kimi K3 tool-calling reliably.
        "reasoning_effort": "low",
        "tools": [SHELL_COMMAND_TOOL],
        # Kimi K3's upstream 400s on the named {"type":"function",...} form;
        # "required" is equivalent here since only one tool is offered
        # (verified live on both K3 and GLM-5.2).
        "tool_choice": "required",
    }
    req = urllib.request.Request(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "conch/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"conch: API error: {format_http_api_error(e)}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"conch: API error: {e}", file=sys.stderr)
        sys.exit(1)
    msg = (data.get("choices") or [{}])[0].get("message", {})
    return command_from_tool_calls(msg)


def call_openai(config: dict, messages: list) -> str:
    import urllib.error
    import urllib.request

    from .providers import build_openai_chat_request_body, format_http_api_error

    api_key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "").strip()
    if not api_key:
        print("conch: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    url = "https://api.openai.com/v1/chat/completions"
    model = config.get("model", "gpt-4o-mini")
    body = build_openai_chat_request_body(
        model,
        messages,
        temperature=0.2,
        max_completion_tokens=2048,
        tools=[SHELL_COMMAND_TOOL],
    )
    body["tool_choice"] = {"type": "function", "function": {"name": "shell_command"}}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"conch: API error: {format_http_api_error(e)}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"conch: API error: {e}", file=sys.stderr)
        sys.exit(1)
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message", json.dumps(err)) if isinstance(err, dict) else str(err)
        print(f"conch: API error: {msg}", file=sys.stderr)
        sys.exit(1)
    msg = (data.get("choices") or [{}])[0].get("message", {})
    return command_from_tool_calls(msg)


def call_anthropic(config: dict, messages: list) -> str:
    import urllib.request

    api_key = os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"), "").strip()
    if not api_key:
        print("conch: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    from .providers import anthropic_forced_tool_choice_supported

    url = "https://api.anthropic.com/v1/messages"
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_content = next((m["content"] for m in messages if m["role"] == "user"), "")
    model = config.get("model", "claude-sonnet-5")
    body = {
        "model": model,
        "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "tools": [{
            "name": "shell_command",
            "description": SHELL_COMMAND_TOOL["function"]["description"],
            "input_schema": COMMAND_SCHEMA,
        }],
        # Opus 5.5 / Fable 5.1 reject forced tool_choice; "auto" still
        # produces the tool call reliably, and the extraction below returns
        # "" (a clean ask-mode failure) if the model answered with text.
        "tool_choice": (
            {"type": "tool", "name": "shell_command"}
            if anthropic_forced_tool_choice_supported(model)
            else {"type": "auto"}
        ),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"conch: API error: {e}", file=sys.stderr)
        sys.exit(1)
    for b in data.get("content", []):
        if b.get("type") == "tool_use" and b.get("name") == "shell_command":
            cmd = (b.get("input") or {}).get("command", "")
            return cmd.strip() if isinstance(cmd, str) else ""
    return ""


def call_ollama(config: dict, messages: list) -> str:
    import urllib.request

    from .providers import (
        _clamp_output_to_context,
        apply_ollama_request_options,
        get_ollama_base_url,
        get_ollama_effective_context,
        validate_ollama_model,
    )

    base = get_ollama_base_url(config)
    url = f"{base}/api/chat"
    model = config.get("model", "llama3.3")
    ok, reason = validate_ollama_model(model, config)
    if ok is not True:
        print(f"conch: Ollama model rejected: {reason}", file=sys.stderr)
        return ""
    body = {
        "model": model,
        "messages": messages,  # proper system + user roles, not a flattened prompt
        "stream": False,
        "tools": [SHELL_COMMAND_TOOL],
    }
    apply_ollama_request_options(body, config, model)
    if body.get("options", {}).get("num_predict"):
        body["options"]["num_predict"] = _clamp_output_to_context(
            body["options"]["num_predict"],
            get_ollama_effective_context(model, config),
            messages,
            [SHELL_COMMAND_TOOL],
        )
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        timeout = float(config.get("ollama_timeout", 120) or 120)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"conch: Ollama error: {e}", file=sys.stderr)
        return ""
    return command_from_tool_calls(data.get("message") or {})


def call_custom(config: dict, messages: list) -> str:
    import urllib.request

    from .providers import (
        _clamp_output_to_context,
        _custom_headers,
        get_custom_context_window,
        get_custom_base_url,
        validate_custom_model,
    )

    base = get_custom_base_url(config)
    model = (
        config.get("model")
        or config.get("chat_model")
        or config.get("custom_model", "")
    )
    ok, reason = validate_custom_model(model, config)
    if ok is not True:
        print(f"conch: custom model rejected: {reason}", file=sys.stderr)
        return ""
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 2048,
        "tools": [SHELL_COMMAND_TOOL],
        "tool_choice": "required",
    }
    body["max_tokens"] = _clamp_output_to_context(
        body["max_tokens"],
        get_custom_context_window(config, model),
        messages,
        [SHELL_COMMAND_TOOL],
    )
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers=_custom_headers(config),
        method="POST",
    )
    try:
        timeout = float(config.get("custom_timeout", 120) or 120)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        print(f"conch: custom endpoint error: {exc}", file=sys.stderr)
        return ""
    message = (data.get("choices") or [{}])[0].get("message", {})
    return command_from_tool_calls(message)


_ASK_CALLERS = {
    "cerebras": call_cerebras,
    "bedrock": call_bedrock,
    "openrouter": call_openrouter,
    "openai": call_openai,
    "anthropic": call_anthropic,
    "ollama": call_ollama,
    "custom": call_custom,
}

def ask(user_request: str, context: Optional[dict] = None) -> str:
    """Main entry: build context, call configured provider, return one command line."""
    from .providers import (
        DEFAULT_API_KEY_ENVS,
        get_fallback_chain,
        get_fallback_model,
        validate_model_for_provider,
    )

    config = load_config()
    context = context or {}
    if get_bool(config, "send_cwd") and not context.get("cwd"):
        context["cwd"] = os.getcwd()
    if get_bool(config, "send_os_shell") and not context.get("os_shell"):
        context["os_shell"] = f"{os.uname().sysname} / {os.environ.get('SHELL', 'sh')}"
    n = get_int(config, "send_history_count")
    if n and "history" not in context and os.environ.get("CONCH_HISTORY"):
        context["history"] = os.environ["CONCH_HISTORY"]

    provider = (config.get("provider") or "openai").lower()
    current_model = config.get("model") or get_fallback_model(provider, config)
    verified, reason = validate_model_for_provider(
        provider, current_model, config
    )
    if verified is not True:
        replacement = get_fallback_model(provider, config)
        if not replacement:
            print(
                f"conch: no verified model for {provider}: {reason}",
                file=sys.stderr,
            )
            return ""
        print(
            f"conch: model '{current_model}' rejected ({reason}); "
            f"using {provider}/{replacement}",
            file=sys.stderr,
        )
        current_model = replacement
        config["model"] = replacement
        config["chat_model"] = replacement

    caller = _ASK_CALLERS.get(provider)
    if not caller:
        print(f"conch: unknown provider {provider}", file=sys.stderr)
        sys.exit(1)

    messages, _ = build_messages(config, user_request, context)
    result = caller(config, messages)
    if result:
        return result

    # Primary failed -- try fallbacks (same provider/other model first, then cross-provider)
    attempt_provider, attempt_model = provider, current_model
    for fb_provider, fb_model, _needs_ctx in get_fallback_chain(provider, current_model, config):
        fb_caller = _ASK_CALLERS.get(fb_provider)
        if not fb_caller:
            continue
        print(
            f"conch: {attempt_provider}/{attempt_model} failed, trying {fb_provider}/{fb_model}...",
            file=sys.stderr,
        )
        fb_config = dict(config)
        fb_config["provider"] = fb_provider
        fb_config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(fb_provider, "")
        fb_config["model"] = fb_model
        fb_messages, _ = build_messages(fb_config, user_request, context)
        result = fb_caller(fb_config, fb_messages)
        if result:
            return result
        attempt_provider, attempt_model = fb_provider, fb_model
    return ""
