"""Provider adapters for OpenAI-compatible, Anthropic, Cerebras, and Ollama backends."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional


def _clean_key(val: str) -> str:
    """Strip smart quotes and non-ASCII chars from API keys."""
    return val.strip().strip("\u201c\u201d\u2018\u2019\u0022\u0027").strip()


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


# Per-1M-token pricing (input, output). $0 = free tier.
MODEL_PRICING = {
    "zai-glm-4.7":                 (0.00, 0.00),
    "gpt-4.1":                     (2.00, 8.00),
    "gpt-4.1-mini":                (0.40, 1.60),
    "gpt-4.1-nano":                (0.10, 0.40),
    "gpt-4o":                      (2.50, 10.00),
    "gpt-4o-mini":                 (0.15, 0.60),
    "o4-mini":                     (1.10, 4.40),
    "o3":                          (2.00, 8.00),
    "o3-mini":                     (1.10, 4.40),
    "claude-sonnet-4-6":           (3.00, 15.00),
    "claude-opus-4-6":             (15.00, 75.00),
    "claude-haiku-4-5":            (0.80, 4.00),
    "claude-sonnet-4-5-20250929":  (3.00, 15.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated cost in USD for a given token count."""
    rate = MODEL_PRICING.get(model, (0.0, 0.0))
    return (input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000


def _normalize_usage(data: dict, provider: str) -> dict:
    """Extract a uniform usage dict from any provider's raw API response."""
    if provider in ("cerebras", "openai"):
        usage = data.get("usage", {})
        return {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }
    if provider == "anthropic":
        usage = data.get("usage", {})
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
    if provider == "ollama":
        return {
            "input_tokens": data.get("prompt_eval_count", 0),
            "output_tokens": data.get("eval_count", 0),
        }
    return {"input_tokens": 0, "output_tokens": 0}


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
    api_key = _clean_key(os.environ.get(config.get("api_key_env", "CEREBRAS_API_KEY"), ""))
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
        "_usage": _normalize_usage(data, "cerebras"),
        "_model": body["model"],
    }


def raw_openai(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    api_key = _clean_key(os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), ""))
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
        "_usage": _normalize_usage(data, "openai"),
        "_model": body["model"],
    }


def raw_anthropic(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    api_key = _clean_key(os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"), ""))
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
        "_usage": _normalize_usage(data, "anthropic"),
        "_model": body["model"],
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
        "_usage": _normalize_usage(data, "ollama"),
        "_model": body["model"],
    }


RAW_FNS = {
    "cerebras": raw_cerebras,
    "openai": raw_openai,
    "anthropic": raw_anthropic,
    "ollama": raw_ollama,
}


# ---------------------------------------------------------------------------
# Streaming provider functions
# ---------------------------------------------------------------------------

def _iter_sse(response):
    """Yield parsed JSON payloads from an SSE response stream."""
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.strip() == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _stream_openai_compat(
    url: str,
    headers: dict,
    body: dict,
    model: str,
    provider: str,
    on_token,
) -> dict:
    """Shared streaming implementation for OpenAI-compatible APIs."""
    body["stream"] = True
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )

    content_parts: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            for chunk in _iter_sse(response):
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})

                text = delta.get("content") or ""
                if not text and provider == "cerebras":
                    text = delta.get("reasoning") or ""

                if text:
                    content_parts.append(text)
                    if on_token:
                        on_token(text)

                for tc in delta.get("tool_calls", []):
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.get("id"):
                        tool_calls_acc[idx]["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        tool_calls_acc[idx]["name"] = fn["name"]
                    if fn.get("arguments") is not None:
                        tool_calls_acc[idx]["arguments"] += fn["arguments"]

                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage["input_tokens"] = u.get("prompt_tokens", 0)
                    usage["output_tokens"] = u.get("completion_tokens", 0)
    except Exception as exc:
        return {"content": f"[API error: {exc}]", "tool_calls": None}

    full_text = "".join(content_parts).strip()
    tool_calls = None
    if tool_calls_acc:
        tool_calls = [
            {
                "id": info["id"],
                "type": "function",
                "function": {"name": info["name"], "arguments": info["arguments"]},
            }
            for info in [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        ]

    return {
        "role": "assistant",
        "content": full_text,
        "tool_calls": tool_calls,
        "_usage": usage,
        "_model": model,
    }


def stream_cerebras(
    config: dict, messages: list, tools=None, on_token=None
) -> dict:
    api_key = _clean_key(os.environ.get(
        config.get("api_key_env", "CEREBRAS_API_KEY"), ""
    ))
    if not api_key:
        return {"content": "", "tool_calls": None}
    base_url = (
        config.get("base_url")
        or os.environ.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
    ).rstrip("/")
    model = config.get("chat_model", config.get("model", "zai-glm-4.7"))
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_completion_tokens": 16384,
        "clear_thinking": False,
    }
    if tools:
        body["tools"] = tools
    return _stream_openai_compat(
        f"{base_url}/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "conch/1.0",
        },
        body,
        model,
        "cerebras",
        on_token,
    )


def stream_openai(
    config: dict, messages: list, tools=None, on_token=None
) -> dict:
    api_key = _clean_key(os.environ.get(
        config.get("api_key_env", "OPENAI_API_KEY"), ""
    ))
    if not api_key:
        return {"content": "", "tool_calls": None}
    model = config.get("chat_model", config.get("model", "gpt-4o-mini"))
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 16384,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
    return _stream_openai_compat(
        "https://api.openai.com/v1/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        body,
        model,
        "openai",
        on_token,
    )


def stream_anthropic(
    config: dict, messages: list, tools=None, on_token=None
) -> dict:
    api_key = _clean_key(os.environ.get(
        config.get("api_key_env", "ANTHROPIC_API_KEY"), ""
    ))
    if not api_key:
        return {"content": "", "tool_calls": None}

    system = ""
    user_messages: list[dict] = []
    for msg in messages:
        if msg["role"] == "system":
            system = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        else:
            user_messages.append(msg)

    model = config.get("chat_model", config.get("model", "claude-sonnet-4-6"))
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": 16384,
        "system": system,
        "messages": user_messages,
        "stream": True,
    }
    if tools:
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get(
                    "parameters", {"type": "object", "properties": {}}
                ),
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

    text_parts: list[str] = []
    anthropic_content: list[dict] = []
    tool_calls: list[dict] = []
    cur_block_type: Optional[str] = None
    cur_block_meta: dict = {}
    cur_text: list[str] = []
    cur_json = ""
    usage = {"input_tokens": 0, "output_tokens": 0}

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                etype = data.get("type")

                if etype == "error":
                    err = data.get("error", {})
                    return {
                        "content": "[API error: %s]" % err.get("message", "unknown stream error"),
                        "tool_calls": None,
                    }

                if etype == "message_start":
                    mu = data.get("message", {}).get("usage", {})
                    usage["input_tokens"] = mu.get("input_tokens", 0)

                elif etype == "content_block_start":
                    block = data.get("content_block", {})
                    cur_block_type = block.get("type")
                    cur_block_meta = block
                    cur_text = []
                    cur_json = ""

                elif etype == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        t = delta.get("text", "")
                        cur_text.append(t)
                        text_parts.append(t)
                        if on_token:
                            on_token(t)
                    elif delta.get("type") == "input_json_delta":
                        cur_json += delta.get("partial_json", "")

                elif etype == "content_block_stop":
                    if cur_block_type == "text":
                        anthropic_content.append(
                            {"type": "text", "text": "".join(cur_text)}
                        )
                    elif cur_block_type == "tool_use":
                        try:
                            inp = json.loads(cur_json) if cur_json else {}
                        except json.JSONDecodeError:
                            inp = {}
                        anthropic_content.append({
                            "type": "tool_use",
                            "id": cur_block_meta.get("id", ""),
                            "name": cur_block_meta.get("name", ""),
                            "input": inp,
                        })
                        tool_calls.append({
                            "id": cur_block_meta.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": cur_block_meta.get("name", ""),
                                "arguments": json.dumps(inp),
                            },
                        })
                    cur_block_type = None

                elif etype == "message_delta":
                    mu = data.get("usage", {})
                    usage["output_tokens"] = mu.get("output_tokens", 0)
    except Exception as exc:
        return {"content": f"[API error: {exc}]", "tool_calls": None}

    full_text = "".join(text_parts).strip()
    if not anthropic_content and full_text:
        anthropic_content = [{"type": "text", "text": full_text}]

    return {
        "role": "assistant",
        "content": full_text,
        "tool_calls": tool_calls if tool_calls else None,
        "_anthropic_content": anthropic_content,
        "_usage": usage,
        "_model": model,
    }


def stream_ollama(
    config: dict, messages: list, tools=None, on_token=None
) -> dict:
    base_url = (
        config.get("base_url")
        or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    ).rstrip("/")
    model = config.get("chat_model", config.get("model", "llama3.3"))
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if tools:
        body["tools"] = tools

    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    content_parts: list[str] = []
    final_data: dict = {}

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = data.get("message", {})
                if msg.get("content"):
                    content_parts.append(msg["content"])
                    if on_token:
                        on_token(msg["content"])

                if data.get("done"):
                    final_data = data
                    break
    except Exception as exc:
        return {"content": f"[Ollama error: {exc}]", "tool_calls": None}

    full_text = "".join(content_parts).strip()
    raw_tool_calls = final_data.get("message", {}).get("tool_calls")
    tool_calls = None
    if raw_tool_calls:
        tool_calls = []
        for i, tc in enumerate(raw_tool_calls):
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
        "content": full_text,
        "tool_calls": tool_calls,
        "_usage": _normalize_usage(final_data, "ollama"),
        "_model": model,
    }


STREAM_FNS = {
    "cerebras": stream_cerebras,
    "openai": stream_openai,
    "anthropic": stream_anthropic,
    "ollama": stream_ollama,
}

