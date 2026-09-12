"""Configuration loading helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional


DEFAULT_CONFIG: Dict[str, str] = {
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "chat_model": "claude-sonnet-5",
    "api_key_env": "ANTHROPIC_API_KEY",
    # Agent mode off by default: shell commands require confirmation unless
    # the user opts in with agent_mode=true in the config file.
    "agent_mode": "false",
    # Local providers are isolated from cloud fallback by default. Set false
    # explicitly to permit a configured local session to cross providers.
    "local_only": "auto",
    # Public-IP geolocation is opt-in; startup otherwise makes no such call.
    "detect_location": "false",
}

ENV_CONFIG_KEYS = {
    "CONCH_PROVIDER": "provider",
    "CONCH_MODEL": "model",
    "CONCH_CHAT_MODEL": "chat_model",
    "CONCH_API_KEY_ENV": "api_key_env",
    "CONCH_BASE_URL": "base_url",
    "CONCH_OLLAMA_BASE_URL": "ollama_base_url",
    "CONCH_OLLAMA_NUM_CTX": "ollama_num_ctx",
    "CONCH_OLLAMA_CONTEXT_WINDOW": "ollama_context_window",
    "CONCH_OLLAMA_TEMPERATURE": "ollama_temperature",
    "CONCH_OLLAMA_NUM_PREDICT": "ollama_num_predict",
    "CONCH_OLLAMA_KEEP_ALIVE": "ollama_keep_alive",
    "CONCH_OLLAMA_THINK": "ollama_think",
    "CONCH_OLLAMA_TIMEOUT": "ollama_timeout",
    "CONCH_CUSTOM_BASE_URL": "custom_base_url",
    "CONCH_CUSTOM_MODEL": "custom_model",
    "CONCH_CUSTOM_CONTEXT_WINDOW": "custom_context_window",
    "CONCH_CUSTOM_TEMPERATURE": "custom_temperature",
    "CONCH_CUSTOM_MAX_TOKENS": "custom_max_tokens",
    "CONCH_CUSTOM_TIMEOUT": "custom_timeout",
    "CONCH_MAX_OUTPUT_TOKENS": "max_output_tokens",
    "CONCH_LOCAL_ONLY": "local_only",
    "CONCH_DETECT_LOCATION": "detect_location",
    "CONCH_AGENT_MODE": "agent_mode",
    "CONCH_PERMISSION_MODE": "permission_mode",
    "CONCH_TOOL_PROFILE": "tool_profile",
    "CONCH_SSH_CONTROL_PERSIST": "ssh_control_persist",
}


def _parse_config_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data


def get_config_path() -> str:
    """Path to the primary Conch config file (may not exist yet)."""
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    return str(config_dir / "config")


# ---------------------------------------------------------------------------
# Project-level context and config (plan 1.8)
# ---------------------------------------------------------------------------

PROJECT_CONTEXT_FILES = ("CONCH.md", "AGENTS.md")
# Budget so project instructions can't swamp a small local model (~1.5k tokens)
PROJECT_CONTEXT_MAX_CHARS = 6000


def _find_upwards(filenames, start_dir: str = "") -> Optional[Path]:
    """Return the first existing file among *filenames*, walking from
    *start_dir* (default cwd) up to the git root (inclusive) or fs root."""
    current = Path(start_dir or os.getcwd()).resolve()
    while True:
        for filename in filenames:
            candidate = current / filename
            if candidate.is_file():
                return candidate
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent


def find_project_rc(start_dir: str = "") -> Optional[Path]:
    """Nearest per-project .conchrc between cwd and the git root."""
    return _find_upwards((".conchrc",), start_dir)


def load_project_context(start_dir: str = "") -> str:
    """Contents of the nearest CONCH.md/AGENTS.md, capped for small models.

    These are persistent project instructions that live outside compactable
    history (the CLAUDE.md pattern); injected into the system prompt.
    """
    path = _find_upwards(PROJECT_CONTEXT_FILES, start_dir)
    if path is None:
        return ""
    try:
        text = path.read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > PROJECT_CONTEXT_MAX_CHARS:
        text = text[:PROJECT_CONTEXT_MAX_CHARS] + "\n... [project context truncated]"
    return f"Project instructions (from {path}):\n{text}"


def load_config() -> Dict[str, str]:
    """Load config from the standard Conch locations.

    Precedence (last wins): defaults < ~/.config/conch/config < ~/.conchrc
    < per-project .conchrc (nearest file between cwd and the git root).
    """
    config = dict(DEFAULT_CONFIG)
    explicit: Dict[str, str] = {}
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    paths = [config_dir / "config", Path.home() / ".conchrc"]
    project_rc = find_project_rc()
    if project_rc is not None and project_rc != (Path.home() / ".conchrc"):
        paths.append(project_rc)
    for path in paths:
        explicit.update(_parse_config_file(path))
    for env_name, key in ENV_CONFIG_KEYS.items():
        if env_name in os.environ:
            explicit[key] = os.environ[env_name].strip()
    config.update(explicit)

    provider = config.get("provider", DEFAULT_CONFIG["provider"]).lower()
    config["provider"] = provider
    provider_defaults = {
        "cerebras": ("CEREBRAS_API_KEY", "gpt-oss-120b"),
        "bedrock": ("AWS_BEARER_TOKEN_BEDROCK", "moonshotai.kimi-k2.5"),
        "openrouter": ("OPENROUTER_API_KEY", "moonshotai/kimi-k3"),
        "openai": ("OPENAI_API_KEY", "gpt-4o-mini"),
        "anthropic": ("ANTHROPIC_API_KEY", "claude-sonnet-5"),
        "ollama": ("", "llama3.3"),
    }
    if provider in provider_defaults:
        default_key_env, default_model = provider_defaults[provider]
        if "api_key_env" not in explicit:
            config["api_key_env"] = default_key_env
        if "model" not in explicit or not config.get("model"):
            config["model"] = default_model
        if "chat_model" not in explicit or not config.get("chat_model"):
            config["chat_model"] = config["model"]

    if provider == "ollama":
        # base_url is shared across providers; remember it as the Ollama
        # endpoint so it survives provider switches and fallbacks.
        if config.get("base_url") and not config.get("ollama_base_url"):
            config["ollama_base_url"] = config["base_url"]
    elif provider == "custom":
        # Custom OpenAI-compatible endpoint (plan 2.4): vLLM, LM Studio, etc.
        config.setdefault("api_key_env", "")
        if config.get("base_url") and not config.get("custom_base_url"):
            config["custom_base_url"] = config["base_url"]
        model = (config.get("model") or "").strip()
        if "model" not in explicit or not model:
            # model inherited from defaults → the endpoint's custom_model wins
            model = (config.get("custom_model") or "").strip()
        config["model"] = model
        config["custom_model"] = (config.get("custom_model") or "").strip() or model
        # chat_model follows the endpoint's model unless explicitly set.
        if "chat_model" not in explicit or not config.get("chat_model"):
            config["chat_model"] = model
    elif provider not in provider_defaults:
        config.setdefault("api_key_env", "ANTHROPIC_API_KEY")
        config.setdefault("model", "claude-sonnet-5")
        config.setdefault("chat_model", config["model"])
    return config


def get_bool(cfg: dict, key: str, default: bool = False) -> bool:
    return str(cfg.get(key, str(default))).lower() in ("true", "1", "yes", "on")


def local_only_enabled(cfg: dict, provider: str = "") -> bool:
    value = str((cfg or {}).get("local_only", "auto")).strip().lower()
    if value == "auto":
        return (provider or (cfg or {}).get("provider", "")).lower() in (
            "ollama",
            "custom",
        )
    return value in ("true", "1", "yes", "on")


def get_int(cfg: dict, key: str, default: int = 0) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default

