"""LLM clients: OpenAI, Anthropic, Ollama. Return single command string."""
import datetime
import json
import os
import re
import sys
from typing import List, Optional, Tuple

from .config import load_config, get_bool, get_int


def extract_command(text: str) -> str:
    """Extract a single shell command from LLM response. No markdown, one line."""
    if not text or not text.strip():
        return ""
    text = text.strip()
    # Strip markdown code blocks
    for pattern in [
        r"^```(?:bash|sh|zsh)?\s*\n?(.*?)```",
        r"^```\n?(.*?)```",
    ]:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            text = m.group(1).strip()
    # Strip leading $ or % from prompt-style output
    text = re.sub(r"^\s*[$%]\s*", "", text)
    # Take first non-empty line
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[0] if lines else ""


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
    system = config.get("system_prompt", "")
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
        "max_tokens": 512,
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
    return extract_command(content)


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
        "max_tokens": 256,
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
    return extract_command(content)


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
    return extract_command(content)


def ask(user_request: str, context: Optional[dict] = None) -> str:
    """Main entry: build context, call configured provider, return one command line."""
    config = load_config()
    context = context or {}
    if get_bool(config, "send_cwd") and not context.get("cwd"):
        context["cwd"] = os.getcwd()
    if get_bool(config, "send_os_shell") and not context.get("os_shell"):
        context["os_shell"] = f"{os.uname().sysname} / {os.environ.get('SHELL', 'sh')}"
    n = get_int(config, "send_history_count")
    if n and "history" not in context and os.environ.get("CONCH_HISTORY"):
        context["history"] = os.environ["CONCH_HISTORY"]

    messages, _ = build_messages(config, user_request, context)
    provider = (config.get("provider") or "openai").lower()

    if provider == "openai":
        return call_openai(config, messages)
    if provider == "anthropic":
        return call_anthropic(config, messages)
    if provider == "ollama":
        return call_ollama(config, messages)
    print(f"conch: unknown provider {provider}", file=sys.stderr)
    sys.exit(1)
