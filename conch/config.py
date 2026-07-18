"""Configuration loading helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


DEFAULT_CONFIG: Dict[str, str] = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "chat_model": "claude-sonnet-4-6",
    "api_key_env": "ANTHROPIC_API_KEY",
    # Agent mode off by default: shell commands require confirmation unless
    # the user opts in with agent_mode=true in the config file.
    "agent_mode": "false",
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


def _find_upwards(filenames, start_dir: str = "") -> Path | None:
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


def find_project_rc(start_dir: str = "") -> Path | None:
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
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    paths = [config_dir / "config", Path.home() / ".conchrc"]
    project_rc = find_project_rc()
    if project_rc is not None and project_rc != (Path.home() / ".conchrc"):
        paths.append(project_rc)
    for path in paths:
        config.update(_parse_config_file(path))

    provider = config.get("provider", DEFAULT_CONFIG["provider"]).lower()
    config["provider"] = provider
    if provider == "cerebras":
        config["api_key_env"] = config.get("api_key_env") or "CEREBRAS_API_KEY"
        config["model"] = config.get("model") or "zai-glm-4.7"
        config["chat_model"] = config.get("chat_model") or config["model"]
    elif provider == "openai":
        config.setdefault("api_key_env", "OPENAI_API_KEY")
        config.setdefault("model", "gpt-4o-mini")
        config.setdefault("chat_model", config["model"])
    elif provider == "ollama":
        config.setdefault("api_key_env", "")
        config.setdefault("model", "llama3.3")
        config.setdefault("chat_model", config["model"])
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
        if not model or model == DEFAULT_CONFIG["model"]:
            # model inherited from defaults → the endpoint's custom_model wins
            model = (config.get("custom_model") or "").strip()
        config["model"] = model
        config["custom_model"] = (config.get("custom_model") or "").strip() or model
        # chat_model follows the endpoint's model unless explicitly set.
        if config.get("chat_model") in (DEFAULT_CONFIG["chat_model"], "", None):
            config["chat_model"] = model
    else:
        config.setdefault("api_key_env", "ANTHROPIC_API_KEY")
        config.setdefault("model", "claude-sonnet-4-6")
        config.setdefault("chat_model", config["model"])
    return config


def get_bool(cfg: dict, key: str, default: bool = False) -> bool:
    return str(cfg.get(key, str(default))).lower() in ("true", "1", "yes", "on")


def get_int(cfg: dict, key: str, default: int = 0) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default

