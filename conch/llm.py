"""LLM clients: OpenAI, Anthropic, Cerebras, Ollama. Return single command string."""
import datetime
import json
import os
import re
import sys
from typing import List, Optional, Tuple

from .config import load_config, get_bool, get_int


_SHELL_PREFIXES = (
    "nmap","arp","avahi","dns-sd","lpstat","find","grep","ls","cat",
    "curl","wget","docker","kubectl","helm","terraform","aws","git",
    "npm","pip","python","ssh","scp","rsync","ping","traceroute",
    "dig","nc","netstat","ss","lsof","ps","top","kill","chmod",
    "chown","mkdir","rm","cp","mv","tar","zip","unzip","sed",
    "awk","sort","head","tail","wc","du","df","mount","systemctl",
    "brew","apt","yum","dnf","pacman","snap","flatpak","xargs",
    "echo","touch","tee","env","export","source","which","whereis",
)


def _extract_from_fenced_blocks(text: str) -> str:
    """Pull a command out of ```bash ... ``` fenced blocks."""
    for pattern in [r"```(?:bash|sh|zsh)?\s*\n?(.*?)```", r"```\n?(.*?)```"]:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            cmd = m.group(1).strip().splitlines()[0].strip()
            if cmd:
                return re.sub(r"^\s*[$%]\s*", "", cmd)
    return ""


def _extract_from_backticks(text: str) -> str:
    """Pull a shell command from inline `backtick` snippets in reasoning text."""
    backtick_cmds = re.findall(r"`([^`]{4,})`", text)
    shell_cmds = [c for c in backtick_cmds if any(c.startswith(t) for t in _SHELL_PREFIXES)]
    return shell_cmds[-1] if shell_cmds else ""


def _extract_first_line(text: str) -> str:
    """Take the first non-empty line, stripping prompt chars."""
    text = re.sub(r"^\s*[$%]\s*", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[0] if lines else ""


def extract_command(text: str, provider: str = "") -> str:
    """Extract a single shell command from LLM response.

    Strategy varies by provider:
    - anthropic/openai: models follow instructions well, so prefer first-line or fenced block.
    - cerebras: model often puts answer in reasoning with backtick snippets.
    - ollama: local models may wrap in markdown, so try fenced blocks first.
    """
    if not text or not text.strip():
        return ""
    text = text.strip()

    # All providers: try fenced code blocks first
    cmd = _extract_from_fenced_blocks(text)
    if cmd:
        return cmd

    if provider == "cerebras":
        # Cerebras reasoning often contains backtick-quoted commands
        cmd = _extract_from_backticks(text)
        if cmd:
            return cmd

    if provider in ("anthropic", "openai"):
        # These models typically return clean single-line commands.
        # Skip preamble lines like "Here is the command:" that aren't actual commands.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for line in lines:
            clean = re.sub(r"^\s*[$%]\s*", "", line)
            if not clean:
                continue
            # Skip lines that look like English prose rather than commands
            if clean.endswith(":") or clean.lower().startswith(("here ", "the ", "this ", "i ", "sure", "certainly")):
                continue
            # Skip lines that are just backtick-wrapped
            stripped = clean.strip("`").strip()
            if stripped and any(stripped.startswith(t) for t in _SHELL_PREFIXES):
                return stripped
            if not clean[0].isalpha():
                return clean
            # Accept if it looks like a command (starts with a known prefix)
            if any(clean.startswith(t) for t in _SHELL_PREFIXES):
                return clean
        # Nothing matched known prefixes; fall back to first line
        return _extract_first_line(text)

    if provider == "ollama":
        # Local models sometimes wrap in backticks inline
        cmd = _extract_from_backticks(text)
        if cmd:
            return cmd
        return _extract_first_line(text)

    # Unknown provider: try everything
    cmd = _extract_from_backticks(text)
    if cmd:
        return cmd
    return _extract_first_line(text)



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
    system = config.get("system_prompt") or get_ask_prompt(provider, model)
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
        "model": config.get("model", "zai-glm-4.7"),
        "messages": messages,
        "temperature": 0.2,
        "max_completion_tokens": 2048,
        "clear_thinking": True,
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
    content = msg.get("content", "")
    if not content.strip() and msg.get("reasoning"):
        content = msg["reasoning"]
    return extract_command(content, provider="cerebras")


def call_openai(config: dict, messages: list) -> str:
    import urllib.request

    api_key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "").strip()
    if not api_key:
        print("conch: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    url = "https://api.openai.com/v1/chat/completions"
    body = {
        "model": config.get("model", "gpt-4o-mini"),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 2048,
    }
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
    except Exception as e:
        print(f"conch: API error: {e}", file=sys.stderr)
        sys.exit(1)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return extract_command(content, provider="openai")


def call_anthropic(config: dict, messages: list) -> str:
    import urllib.request

    api_key = os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"), "").strip()
    if not api_key:
        print("conch: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    url = "https://api.anthropic.com/v1/messages"
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_content = next((m["content"] for m in messages if m["role"] == "user"), "")
    body = {
        "model": config.get("model", "claude-sonnet-4-6"),
        "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
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
    content = ""
    for b in data.get("content", []):
        if b.get("type") == "text":
            content += b.get("text", "")
    return extract_command(content, provider="anthropic")


def call_ollama(config: dict, messages: list) -> str:
    import urllib.request

    base = (config.get("base_url") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    url = f"{base}/api/chat"
    # Ollama wants prompt; we concatenate system + user
    prompt = "\n\n".join(m["content"] for m in messages)
    body = {
        "model": config.get("model", "llama3.2"),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"conch: Ollama error: {e}", file=sys.stderr)
        sys.exit(1)
    content = (data.get("message") or {}).get("content", "")
    return extract_command(content, provider="ollama")


_ASK_CALLERS = {
    "cerebras": call_cerebras,
    "openai": call_openai,
    "anthropic": call_anthropic,
    "ollama": call_ollama,
}

def ask(user_request: str, context: Optional[dict] = None) -> str:
    """Main entry: build context, call configured provider, return one command line."""
    from .providers import get_fallback_chain, DEFAULT_API_KEY_ENVS, KNOWN_MODELS

    config = load_config()
    context = context or {}
    if get_bool(config, "send_cwd") and not context.get("cwd"):
        context["cwd"] = os.getcwd()
    if get_bool(config, "send_os_shell") and not context.get("os_shell"):
        try:
            sysname = os.uname().sysname
        except AttributeError:
            import platform
            sysname = platform.system()
        context["os_shell"] = sysname + " / " + os.environ.get("SHELL", os.environ.get("COMSPEC", "sh"))
    n = get_int(config, "send_history_count")
    if n and "history" not in context and os.environ.get("CONCH_HISTORY"):
        context["history"] = os.environ["CONCH_HISTORY"]

    provider = (config.get("provider") or "openai").lower()
    current_model = config.get("model", KNOWN_MODELS.get(provider, [""])[0])

    caller = _ASK_CALLERS.get(provider)
    if not caller:
        print(f"conch: unknown provider {provider}", file=sys.stderr)
        sys.exit(1)

    messages, _ = build_messages(config, user_request, context)
    result = caller(config, messages)
    if result:
        return result

    # Primary failed -- try fallbacks (same provider/other model first, then cross-provider)
    for fb_provider, fb_model, _needs_ctx in get_fallback_chain(provider, current_model):
        fb_caller = _ASK_CALLERS.get(fb_provider)
        if not fb_caller:
            continue
        print(f"conch: {provider}/{current_model} failed, trying {fb_provider}/{fb_model}...", file=sys.stderr)
        fb_config = dict(config)
        fb_config["provider"] = fb_provider
        fb_config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(fb_provider, "")
        fb_config["model"] = fb_model
        fb_messages, _ = build_messages(fb_config, user_request, context)
        result = fb_caller(fb_config, fb_messages)
        if result:
            return result
    return ""
