"""Multi-turn chat with the configured LLM. Not for shell commands — general Q&A."""
import json
import os
import sys
import urllib.request
from typing import List

from .config import load_config
from .render import Spinner, highlight

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
    "- Configuration: ~/.config/conch/config or ~/.conchrc. Supports OpenAI, Anthropic, or Ollama.\n"
    "- The user can switch models by editing the config file (model = <model-name>).\n"
    "- Install/update: run install.sh from the conch directory.\n"
    "When answering about your capabilities, be specific and helpful."
)


def _call_openai(config: dict, messages: List[dict]) -> str:
    api_key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "").strip()
    if not api_key:
        print("conch: OPENAI_API_KEY not set", file=sys.stderr)
        return ""
    body = {
        "model": config.get("chat_model", config.get("model", "gpt-3.5-turbo")),
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1024,
    }
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
        return f"[API error: {e}]"
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


def _call_anthropic(config: dict, messages: List[dict]) -> str:
    api_key = os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"), "").strip()
    if not api_key:
        print("conch: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return ""
    system = ""
    user_messages = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            user_messages.append(m)
    body = {
        "model": config.get("chat_model", config.get("model", "claude-3-5-haiku-20241022")),
        "max_tokens": 1024,
        "system": system,
        "messages": user_messages,
    }
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
        return f"[API error: {e}]"
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def _call_ollama(config: dict, messages: List[dict]) -> str:
    base = (config.get("base_url") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    body = {
        "model": config.get("chat_model", config.get("model", "llama3.2")),
        "messages": messages,
        "stream": False,
    }
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
        return f"[Ollama error: {e}]"
    return (data.get("message") or {}).get("content", "").strip()


def chat_loop():
    """Interactive multi-turn chat. Reads from stdin, prints responses."""
    config = load_config()
    provider = (config.get("provider") or "openai").lower()
    system_prompt = config.get("chat_system_prompt", CHAT_SYSTEM_PROMPT)
    model_name = config.get("chat_model", config.get("model", ""))

    call_fn = {"openai": _call_openai, "anthropic": _call_anthropic, "ollama": _call_ollama}.get(provider)
    if not call_fn:
        print(f"conch: unknown provider {provider}", file=sys.stderr)
        sys.exit(1)

    messages: List[dict] = [{"role": "system", "content": system_prompt}]

    print(f"\033[1;36mConch chat\033[0m \033[2m({provider}/{model_name})\033[0m")
    print(f"\033[2mType 'exit' or Ctrl+D to quit.\033[0m\n")

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
        with Spinner("Thinking"):
            reply = call_fn(config, messages)
        if reply:
            messages.append({"role": "assistant", "content": reply})
            print(f"\n\033[1;36massistant:\033[0m\n{highlight(reply)}\n")
        else:
            print("\n\033[2m[no response]\033[0m\n")


def main():
    """Entry point for bin/conch-chat."""
    if len(sys.argv) > 1:
        config = load_config()
        provider = (config.get("provider") or "openai").lower()
        call_fn = {"openai": _call_openai, "anthropic": _call_anthropic, "ollama": _call_ollama}.get(provider)
        if not call_fn:
            print(f"conch: unknown provider {provider}", file=sys.stderr)
            sys.exit(1)
        system_prompt = config.get("chat_system_prompt", CHAT_SYSTEM_PROMPT)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": " ".join(sys.argv[1:])},
        ]
        with Spinner("Thinking"):
            reply = call_fn(config, messages)
        if reply:
            print(highlight(reply))
        else:
            print("[no response]", file=sys.stderr)
            sys.exit(1)
    else:
        chat_loop()
