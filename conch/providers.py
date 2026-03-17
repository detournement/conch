"""Provider adapters for OpenAI-compatible, Anthropic, Cerebras, and Ollama backends."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional


KNOWN_MODELS = {
    "cerebras": [
        "zai-glm-4.7",
    ],
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

DEFAULT_API_KEY_ENVS = {
    "cerebras": "CEREBRAS_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": "",
}


CROSS_PROVIDER_FALLBACK_ORDER = ["cerebras", "anthropic", "openai", "ollama"]


def _has_key(provider: str) -> bool:
    key_env = DEFAULT_API_KEY_ENVS.get(provider, "")
    if not key_env:
        return provider == "ollama"
    return bool(os.environ.get(key_env, "").strip())


def get_fallback_chain(current_provider: str, current_model: str) -> list:
    """Return ordered list of (provider, model, needs_context_switch) fallback candidates.

    Strategy:
    1. Same provider, next model in KNOWN_MODELS list (no context switch needed)
    2. Other providers with valid API keys (context switch required)
    """
    chain = []

    # Step 1: same-provider model fallbacks
    same_models = KNOWN_MODELS.get(current_provider, [])
    try:
        idx = same_models.index(current_model)
        for alt_model in same_models[idx + 1:]:
            chain.append((current_provider, alt_model, False))
    except ValueError:
        # current model not in list; try all others in the provider
        for alt_model in same_models:
            if alt_model != current_model:
                chain.append((current_provider, alt_model, False))

    # Step 2: cross-provider fallbacks
    for provider in CROSS_PROVIDER_FALLBACK_ORDER:
        if provider == current_provider:
            continue
        if not _has_key(provider):
            continue
        models = KNOWN_MODELS.get(provider, [])
        if models:
            chain.append((provider, models[0], True))

    return chain


def get_fallback_model(provider: str) -> str:
    """Return the default model for a provider."""
    models = KNOWN_MODELS.get(provider, [])
    return models[0] if models else ""


def raw_cerebras(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    api_key = os.environ.get(config.get("api_key_env", "CEREBRAS_API_KEY"), "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    base_url = (config.get("base_url") or os.environ.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")).rstrip("/")
    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "zai-glm-4.7")),
        "messages": messages,
        "temperature": 0.7,
        "max_completion_tokens": 16384,
        # Preserve prior thinking/tool context for agentic flows.
        "clear_thinking": False,
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "conch/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        return {"content": f"[API error: {exc}]", "tool_calls": None}
    message = (data.get("choices") or [{}])[0].get("message", {})
    content = (message.get("content") or "").strip()
    if not content and message.get("reasoning"):
        content = message["reasoning"].strip()
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": message.get("tool_calls"),
    }


def raw_openai(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    api_key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "gpt-4o-mini")),
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 16384,
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
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        return {"content": f"[API error: {exc}]", "tool_calls": None}
    message = (data.get("choices") or [{}])[0].get("message", {})
    return {
        "role": "assistant",
        "content": (message.get("content") or "").strip(),
        "tool_calls": message.get("tool_calls"),
    }


def raw_anthropic(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    api_key = os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"), "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    system = ""
    user_messages: List[dict] = []
    for message in messages:
        if message["role"] == "system":
            system = message["content"] if isinstance(message["content"], str) else str(message["content"])
        else:
            user_messages.append(message)
    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "claude-sonnet-4-6")),
        "max_tokens": 16384,
        "system": system,
        "messages": user_messages,
    }
    if tools:
        body["tools"] = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "input_schema": tool["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for tool in tools
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
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        return {"content": f"[API error: {exc}]", "tool_calls": None}

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


def raw_ollama(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    base_url = (config.get("base_url") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "llama3.3")),
        "messages": messages,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        return {"content": f"[Ollama error: {exc}]", "tool_calls": None}
    message = data.get("message", {})
    raw_tool_calls = message.get("tool_calls")
    tool_calls = None
    if raw_tool_calls:
        tool_calls = []
        for i, tool_call in enumerate(raw_tool_calls):
            fn = tool_call.get("function", {})
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
        "content": message.get("content", "").strip(),
        "tool_calls": tool_calls,
    }


RAW_FNS = {
    "cerebras": raw_cerebras,
    "openai": raw_openai,
    "anthropic": raw_anthropic,
    "ollama": raw_ollama,
}

