"""First-run onboarding: the interactive provider/key wizard.

Runs once, on an interactive `conch` launch that has no configuration
at all — no config file, no ~/.conchrc, no project rc, no key file, no
provider key in the environment. It asks for a provider, collects the
API key with :mod:`getpass` (never echoed, never in argv), optionally
verifies it with one live read-only probe, and writes:

- ``~/.config/conch/config`` — provider selection (0600);
- ``~/.config/conch/env`` — the key as ``KEY=value`` (0600), loaded
  into the environment by :func:`conch.config.load_config` so the
  shell, one-shots, and daemons all see it without profile exports.

Non-interactive contexts (pipes, daemons, workers, CI) never see the
wizard: it requires a real TTY on stdin and stdout, and honors
``CONCH_NO_WIZARD=1``. It never overwrites existing configuration —
any configured surface disables it entirely.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from urllib import error, request

from .config import (
    find_project_rc,
    get_config_path,
    get_env_file_path,
    set_config_values,
    set_env_values,
)

CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RST = "\033[0m"

# (provider, key env, label, one-line pitch). The wizard's menu order.
WIZARD_PROVIDERS = (
    ("anthropic", "ANTHROPIC_API_KEY", "Anthropic",
     "Claude models — conch's default"),
    ("openai", "OPENAI_API_KEY", "OpenAI", "GPT models"),
    ("openrouter", "OPENROUTER_API_KEY", "OpenRouter",
     "one key, many models"),
    ("ollama", "", "Ollama", "local models, no API key"),
    ("custom", "", "Custom endpoint", "any OpenAI-compatible server"),
)

CONFIG_HEADER = (
    "# Conch config — written by first-run setup. Edit anytime;\n"
    "# see config.example in the repo for every option."
)


def _http_get(url: str, headers: dict, timeout: float = 12.0) -> Tuple[int, bytes]:
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except error.HTTPError as exc:
        return exc.code, b""


def detect_ollama(timeout: float = 1.0) -> Optional[List[str]]:
    """Model names from a local Ollama server, or None when unreachable."""
    from .providers import get_ollama_base_url

    base = get_ollama_base_url({}).rstrip("/")
    try:
        status, body = _http_get(f"{base}/api/tags", {}, timeout=timeout)
    except Exception:
        return None
    if status != 200:
        return None
    try:
        models = json.loads(body.decode("utf-8", "replace")).get("models") or []
        return [str(m.get("name") or "") for m in models if m.get("name")]
    except (ValueError, AttributeError):
        return None


def probe_provider(provider: str, api_key: str,
                   base_url: str = "") -> Tuple[bool, str]:
    """One read-only reachability/auth probe. The key travels only in a
    request header; nothing about it is ever printed or logged."""
    try:
        if provider == "anthropic":
            status, _ = _http_get(
                "https://api.anthropic.com/v1/models",
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        elif provider == "openai":
            status, _ = _http_get(
                "https://api.openai.com/v1/models",
                {"Authorization": f"Bearer {api_key}"},
            )
        elif provider == "openrouter":
            status, _ = _http_get(
                "https://openrouter.ai/api/v1/key",
                {"Authorization": f"Bearer {api_key}"},
            )
        elif provider == "ollama":
            return (detect_ollama() is not None,
                    "local server reachable" if detect_ollama() is not None
                    else "no server at the Ollama address")
        elif provider == "custom":
            headers = (
                {"Authorization": f"Bearer {api_key}"} if api_key else {}
            )
            status, _ = _http_get(
                f"{base_url.rstrip('/')}/models", headers
            )
        else:
            return False, f"no probe for provider {provider!r}"
    except Exception as exc:
        return False, f"unreachable ({type(exc).__name__})"
    if status in (401, 403):
        return False, "the API rejected this key"
    if 200 <= status < 300:
        return True, "key accepted"
    return False, f"unexpected response (HTTP {status})"


def should_offer_wizard() -> bool:
    """True only for a truly unconfigured, interactive launch."""
    if os.environ.get("CONCH_NO_WIZARD", "").strip():
        return False
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    if Path(get_config_path()).exists():
        return False
    if Path(get_env_file_path()).exists():
        return False
    if (Path.home() / ".conchrc").exists():
        return False
    if find_project_rc() is not None:
        return False
    from .providers import DEFAULT_API_KEY_ENVS

    for env_name in DEFAULT_API_KEY_ENVS.values():
        if env_name and os.environ.get(env_name, "").strip():
            return False
    return True


def _choose_provider() -> Tuple[str, str, Optional[List[str]]]:
    """(provider, key_env, ollama_models) from the interactive menu."""
    ollama_models = detect_ollama()
    print(f"\n{BOLD}{CYAN}  🐚 Welcome to conch{RST}")
    print(f"  {DIM}One-time setup — pick a model provider. Everything can"
          f" be changed later in {get_config_path()}.{RST}\n")
    for index, (provider, _env, label, pitch) in enumerate(
        WIZARD_PROVIDERS, start=1
    ):
        note = pitch
        if provider == "ollama":
            note = (
                f"detected locally, {len(ollama_models)} model(s) — no API"
                " key needed"
                if ollama_models is not None else f"{pitch} (not running)"
            )
        print(f"    {BOLD}{index}{RST}. {label:<16}{DIM}{note}{RST}")
    while True:
        raw = input(f"\n  Provider [1-{len(WIZARD_PROVIDERS)}, Enter=1]: ").strip()
        if not raw:
            raw = "1"
        if raw.isdigit() and 1 <= int(raw) <= len(WIZARD_PROVIDERS):
            provider, env_name, _label, _pitch = WIZARD_PROVIDERS[int(raw) - 1]
            return provider, env_name, ollama_models
        by_name = {p[0]: p for p in WIZARD_PROVIDERS}
        if raw.lower() in by_name:
            provider, env_name, _label, _pitch = by_name[raw.lower()]
            return provider, env_name, ollama_models
        print(f"  {RED}Pick a number between 1 and"
              f" {len(WIZARD_PROVIDERS)}.{RST}")


def _collect_key(env_name: str) -> str:
    """The API key via getpass — hidden input, may be empty (skip)."""
    print(f"\n  {DIM}The key is stored in {get_env_file_path()} (0600),"
          f" never echoed, never logged.{RST}")
    key = getpass.getpass(f"  {env_name} (Enter to skip for now): ").strip()
    return key


def run_first_run_wizard() -> bool:
    """The interactive flow. Returns True when config was written."""
    provider, env_name, _ollama_models = _choose_provider()
    base_url = ""
    key = ""
    if provider == "custom":
        base_url = input(
            "  OpenAI-compatible base URL (e.g."
            " http://localhost:8080/v1): "
        ).strip().rstrip("/")
        optional = getpass.getpass(
            "  API key for it, if any (Enter for none): "
        ).strip()
        key = optional
    elif env_name:
        key = _collect_key(env_name)

    if key or provider in ("ollama", "custom"):
        answer = input("  Verify with one live request? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            ok, detail = probe_provider(provider, key, base_url)
            if ok:
                print(f"  {GREEN}✓ {detail}{RST}")
            else:
                print(f"  {YELLOW}! {detail}{RST}")
                if env_name and input(
                    "  Re-enter the key? [y/N]: "
                ).strip().lower() in ("y", "yes"):
                    key = _collect_key(env_name)
                    ok, detail = probe_provider(provider, key, base_url)
                    print(f"  {GREEN}✓ {detail}{RST}" if ok
                          else f"  {YELLOW}! {detail} — keeping it anyway;"
                               f" fix it later in {get_env_file_path()}{RST}")

    updates = {"provider": provider}
    if provider == "custom" and base_url:
        updates["base_url"] = base_url
    config_path = set_config_values(updates, header=CONFIG_HEADER)
    if key and env_name:
        set_env_values({env_name: key})
    elif key and provider == "custom":
        # Custom endpoints read OPENAI_API_KEY-style bearer via api_key_env;
        # store under a conch-specific name and point config at it.
        set_config_values({"api_key_env": "CONCH_CUSTOM_API_KEY"})
        set_env_values({"CONCH_CUSTOM_API_KEY": key})

    print(f"\n  {GREEN}✓ You're set.{RST} {DIM}config: {config_path}")
    if key:
        print(f"  {DIM}key: {get_env_file_path()} (0600){RST}")
    if not key and env_name:
        print(f"  {YELLOW}No key saved{RST} {DIM}— add a line"
              f" `{env_name}=...` to {get_env_file_path()} or export it"
              f" in your shell before chatting.{RST}")
    print(f"  {DIM}Try: conch \"what can you do?\"  ·  /help lists"
          f" commands  ·  /install lists components{RST}\n")
    return True


def maybe_run_first_run_wizard() -> bool:
    """Gatekeeper called from the CLI entry point: run the wizard when —
    and only when — it should run; never raise into startup."""
    try:
        if not should_offer_wizard():
            return False
    except Exception:
        return False
    try:
        return run_first_run_wizard()
    except (KeyboardInterrupt, EOFError):
        print(f"\n  {DIM}Setup skipped — run `conch` again anytime, or"
              f" create {get_config_path()} yourself.{RST}")
        return False
