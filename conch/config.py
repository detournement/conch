"""Configuration loading helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


DEFAULT_CONFIG: Dict[str, str] = {
    "provider": "cerebras",
    "model": "zai-glm-4.7",
    "chat_model": "zai-glm-4.7",
    "api_key_env": "CEREBRAS_API_KEY",
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


def load_config() -> Dict[str, str]:
    """Load config from the standard Conch locations."""
    config = dict(DEFAULT_CONFIG)
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    for path in (config_dir / "config", Path.home() / ".conchrc"):
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

