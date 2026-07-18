"""Provider adapters for OpenAI-compatible, Anthropic, Cerebras, and Ollama backends."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


KNOWN_MODELS = {
    "cerebras": [
        "zai-glm-4.7",
    ],
    # Conch requires tool calling, so only tool-capable models are listed
    # (e.g. o1-mini is excluded: it supports neither tools nor system messages).
    "openai": [
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4o",
        "gpt-4o-mini",
        "o4-mini",
        "o3",
        "o3-mini",
        "o1",
        "o3-pro",
        "o1-pro",
        "gpt-5.4-pro",
    ],
    "anthropic": [
        "claude-opus-4-8",
        "claude-sonnet-4-7",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
    ],
    # Ollama models are discovered live from the server's /api/tags
    # (see list_ollama_models); no hardcoded list.
    "ollama": [],
}

DEFAULT_API_KEY_ENVS = {
    "cerebras": "CEREBRAS_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": "",
}

PROVIDER_TOOL_LIMITS = {
    "openai": 128,
    "cerebras": 128,
    # Local models drown in large tool lists: cap hard and select the most
    # relevant tools per turn (see tooling.select_relevant_tools).
    "ollama": 12,
}

# Used for `/provider` and tool `set_provider` — stable defaults, not KNOWN_MODELS[0].
# The ollama entry is only a *preference*: it is used when the model actually
# exists on the configured server (see get_fallback_model).
DEFAULT_CHAT_MODEL_BY_PROVIDER = {
    "cerebras": "zai-glm-4.7",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-6",
    "ollama": "llama3.3",
}


# Known context windows (tokens) for cloud models. Ollama windows are
# discovered live from the server instead (see get_ollama_context_length).
MODEL_CONTEXT_WINDOWS = {
    # cerebras
    "zai-glm-4.7": 131072,
    # openai
    "gpt-5.4": 400000,
    "gpt-5.4-pro": 400000,
    "gpt-5.4-mini": 400000,
    "gpt-5.4-nano": 400000,
    "gpt-5-mini": 400000,
    "gpt-5-nano": 400000,
    "gpt-4.1": 1047576,
    "gpt-4.1-mini": 1047576,
    "gpt-4.1-nano": 1047576,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "o4-mini": 200000,
    "o3": 200000,
    "o3-mini": 200000,
    "o3-pro": 200000,
    "o1": 200000,
    "o1-mini": 128000,
    "o1-pro": 200000,
    # anthropic
    "claude-opus-4-8": 200000,
    "claude-sonnet-4-7": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-opus-4-6": 200000,
    "claude-haiku-4-5": 200000,
    "claude-sonnet-4-5-20250929": 200000,
}

# Conservative fallbacks when a model isn't in the table. (Ollama windows
# derive from the num_ctx sent on requests instead — see get_ollama_num_ctx;
# the entry here documents the server's own default when num_ctx is omitted.)
PROVIDER_DEFAULT_CONTEXT_WINDOWS = {
    "cerebras": 131072,
    "openai": 128000,
    "anthropic": 200000,
    "ollama": 4096,
}


# ---------------------------------------------------------------------------
# Ollama model discovery (live from the server's /api/tags)
# ---------------------------------------------------------------------------

OLLAMA_TAGS_TIMEOUT = 2.0  # short so the UI never hangs on an unreachable server
_OLLAMA_TAGS_TTL_OK = 30.0
_OLLAMA_TAGS_TTL_FAIL = 5.0
_ollama_tags_cache: Dict[str, tuple] = {}  # base_url -> (fetched_at, models-or-None)
_ollama_caps_cache: Dict[tuple, tuple] = {}  # (base_url, model) -> (checked_at, supports_tools-or-None)
_ollama_ctx_cache: Dict[tuple, tuple] = {}  # (base_url, model) -> (checked_at, context_length-or-None)


def get_ollama_base_url(config: Optional[dict] = None) -> str:
    """Resolve the Ollama base URL: config base_url (when provider is ollama),
    then OLLAMA_HOST, then localhost."""
    config = config or {}
    base = (config.get("ollama_base_url") or "").strip()
    if not base and (config.get("provider") or "").lower() == "ollama":
        # base_url is a shared config key; only trust it when it belongs to ollama
        base = (config.get("base_url") or "").strip()
    if not base:
        base = os.environ.get("OLLAMA_HOST", "").strip() or "http://localhost:11434"
    if "://" not in base:
        base = "http://" + base
    return base.rstrip("/")


def ollama_model_supports_tools(
    model: str,
    config: Optional[dict] = None,
    *,
    timeout: float = OLLAMA_TAGS_TIMEOUT,
) -> Optional[bool]:
    """Check via POST /api/show whether *model* advertises the "tools" capability.

    Returns None when the server can't be asked. Positive/negative answers are
    cached for the session (capabilities don't change for an installed model);
    failures are retried after a short TTL.
    """
    base_url = get_ollama_base_url(config)
    key = (base_url, model)
    now = time.monotonic()
    cached = _ollama_caps_cache.get(key)
    if cached is not None:
        checked_at, supports = cached
        if supports is not None or now - checked_at < _OLLAMA_TAGS_TTL_FAIL:
            return supports
    req = urllib.request.Request(
        f"{base_url}/api/show",
        data=json.dumps({"model": model}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode())
        caps = data.get("capabilities")
        if isinstance(caps, list):
            supports = "tools" in caps
        else:
            # Older Ollama servers don't report capabilities; fall back to
            # checking whether the model's template renders tools.
            supports = ".Tools" in (data.get("template") or "")
    except Exception:
        supports = None
    _ollama_caps_cache[key] = (now, supports)
    return supports


def get_ollama_context_length(
    model: str,
    config: Optional[dict] = None,
    *,
    timeout: float = OLLAMA_TAGS_TIMEOUT,
) -> Optional[int]:
    """Return the model's maximum context length via POST /api/show.

    The value lives in model_info under "<arch>.context_length" (e.g.
    "llama.context_length"). Returns None when the server can't be asked.
    Successful answers are cached for the session; failures retry after a
    short TTL, mirroring ollama_model_supports_tools.
    """
    base_url = get_ollama_base_url(config)
    key = (base_url, model)
    now = time.monotonic()
    cached = _ollama_ctx_cache.get(key)
    if cached is not None:
        checked_at, ctx = cached
        if ctx is not None or now - checked_at < _OLLAMA_TAGS_TTL_FAIL:
            return ctx
    req = urllib.request.Request(
        f"{base_url}/api/show",
        data=json.dumps({"model": model}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode())
        model_info = data.get("model_info") or {}
        for info_key, value in model_info.items():
            if info_key.endswith(".context_length") and isinstance(value, int) and value > 0:
                ctx = value
                break
    except Exception:
        ctx = None
    _ollama_ctx_cache[key] = (now, ctx)
    return ctx


# Default num_ctx sent on every Ollama request. Without an explicit num_ctx
# Ollama silently defaults to a small window (4k under 24 GiB VRAM) and
# truncates from the top, evicting the system prompt and tool schemas.
DEFAULT_OLLAMA_NUM_CTX = 32768
# keep_alive keeps the model loaded between turns so the KV cache survives.
DEFAULT_OLLAMA_KEEP_ALIVE = "10m"


def get_ollama_num_ctx(model: str, config: Optional[dict] = None) -> int:
    """The num_ctx actually sent on Ollama requests: config ``ollama_num_ctx``
    (default 32768), clamped to the model's max context when the server can
    report it. The runtime context window derives from this same number so
    the token budget always matches what requests run with."""
    try:
        num_ctx = int((config or {}).get("ollama_num_ctx", 0) or 0)
    except (TypeError, ValueError):
        num_ctx = 0
    if num_ctx <= 0:
        num_ctx = DEFAULT_OLLAMA_NUM_CTX
    model_max = get_ollama_context_length(model, config)
    if model_max:
        num_ctx = min(num_ctx, model_max)
    return num_ctx


def apply_ollama_request_options(body: Dict[str, Any], config: dict, model: str) -> None:
    """Set options.num_ctx and keep_alive on an /api/chat request body."""
    options = body.setdefault("options", {})
    options["num_ctx"] = get_ollama_num_ctx(model, config)
    keep_alive = str((config or {}).get("ollama_keep_alive", "") or "").strip()
    body["keep_alive"] = keep_alive or DEFAULT_OLLAMA_KEEP_ALIVE


def get_context_window(provider: str, model: str, config: Optional[dict] = None) -> int:
    """Best-known context window (tokens) for *model* on *provider*.

    Cloud providers use the static MODEL_CONTEXT_WINDOWS table. For Ollama
    the effective window is whatever num_ctx requests run with (see
    get_ollama_num_ctx), not the model's theoretical max.
    """
    provider = (provider or "").lower()
    if provider == "ollama":
        return get_ollama_num_ctx(model, config)
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]
    return PROVIDER_DEFAULT_CONTEXT_WINDOWS.get(provider, 128000)


def list_ollama_models(
    config: Optional[dict] = None,
    *,
    timeout: float = OLLAMA_TAGS_TIMEOUT,
    force_refresh: bool = False,
    tool_capable_only: bool = True,
) -> Optional[List[str]]:
    """Return model names installed on the Ollama server, or None if unreachable.

    By default only models that support tool calling (per /api/show
    capabilities) are returned, since Conch requires tool support. Results
    (including failures) are cached briefly per base URL so repeated UI
    actions don't re-hit the network.
    """
    base_url = get_ollama_base_url(config)
    now = time.monotonic()
    cached = _ollama_tags_cache.get(base_url)
    if cached is not None and not force_refresh:
        fetched_at, models = cached
        ttl = _OLLAMA_TAGS_TTL_OK if models is not None else _OLLAMA_TAGS_TTL_FAIL
        if now - fetched_at >= ttl:
            cached = None
    else:
        cached = None
    if cached is None:
        try:
            with urllib.request.urlopen(f"{base_url}/api/tags", timeout=timeout) as response:
                data = json.loads(response.read().decode())
            models = [m["name"] for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]
        except Exception:
            models = None
        _ollama_tags_cache[base_url] = (now, models)
    else:
        models = cached[1]
    if models is None:
        return None
    if tool_capable_only:
        models = [
            m for m in models
            if ollama_model_supports_tools(m, config, timeout=timeout) is True
        ]
    return models


def ollama_model_matches(model: str, available: List[str]) -> bool:
    """True if *model* refers to one of the server's models.

    Server names carry tags ("llama3.3:latest"); a bare name like "llama3.3"
    matches any tag of that model, mirroring Ollama's own resolution.
    """
    if not model:
        return False
    if model in available:
        return True
    if ":" not in model:
        return any(name.split(":", 1)[0] == model for name in available)
    return False


def ollama_model_available(model: str, config: Optional[dict] = None) -> Optional[bool]:
    """True/False if the server is reachable, None if it isn't.

    A model counts as available only if it is installed AND supports tools.
    """
    models = list_ollama_models(config)
    if models is None:
        return None
    return ollama_model_matches(model, models)


def validate_ollama_model(model: str, config: Optional[dict] = None) -> tuple:
    """Validate a proposed Ollama model switch.

    Returns (ok, reason): ok is True when the model is usable, False when it
    must be rejected, None when the server is unreachable. *reason* explains
    rejections.
    """
    installed = list_ollama_models(config, tool_capable_only=False)
    if installed is None:
        return None, f"Ollama server unreachable at {get_ollama_base_url(config)}"
    if not ollama_model_matches(model, installed):
        return False, f"model '{model}' is not installed on the Ollama server"
    resolved = model if model in installed else next(
        (name for name in installed if name.split(":", 1)[0] == model), model
    )
    if ollama_model_supports_tools(resolved, config) is False:
        return False, f"model '{model}' doesn't support tool calling"
    return True, ""


def error_response(message: str) -> dict:
    """Uniform provider error result.

    Every provider (including Ollama) signals failure the same way: a single
    ``[API error: ...]`` content prefix plus a structured ``_error`` flag so
    the runtime can detect errors without string matching — and never persist
    the text into history or memory.
    """
    return {
        "role": "assistant",
        "content": f"[API error: {message}]",
        "tool_calls": None,
        "_error": True,
    }


def format_http_api_error(exc: BaseException) -> str:
    """Pull OpenAI-style JSON error.message from HTTPError bodies when present."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or raw[:500]
                typ = err.get("type")
                code = err.get("param") or err.get("code")
                parts = [str(msg).strip()]
                if typ:
                    parts.append(f"type={typ}")
                if code:
                    parts.append(f"code={code}")
                return " — ".join(parts)
            if err is not None:
                return str(err)
        except Exception:
            pass
        return str(exc)
    return str(exc)


# Per-1M-token pricing (input, output). $0 = free tier.
MODEL_PRICING = {
    "zai-glm-4.7":                 (0.00, 0.00),
    "gpt-5.4":                     (2.50, 15.00),
    "gpt-5.4-pro":                 (15.00, 120.00),
    "gpt-5.4-mini":                (0.75, 4.50),
    "gpt-5.4-nano":                (0.20, 1.25),
    "gpt-5-mini":                  (0.75, 4.50),
    "gpt-5-nano":                  (0.20, 1.25),
    "gpt-4.1":                     (2.00, 8.00),
    "gpt-4.1-mini":                (0.40, 1.60),
    "gpt-4.1-nano":                (0.10, 0.40),
    "gpt-4o":                      (2.50, 10.00),
    "gpt-4o-mini":                 (0.15, 0.60),
    "o4-mini":                     (1.10, 4.40),
    "o3":                          (2.00, 8.00),
    "o3-mini":                     (1.10, 4.40),
    "o3-pro":                      (20.00, 80.00),
    "o1":                          (15.00, 60.00),
    "o1-mini":                     (1.10, 4.40),
    "o1-pro":                      (150.00, 600.00),
    "claude-sonnet-4-6":           (3.00, 15.00),
    "claude-opus-4-6":             (15.00, 75.00),
    "claude-haiku-4-5":            (0.80, 4.00),
    "claude-sonnet-4-5-20250929":  (3.00, 15.00),
    "claude-opus-4-8":             (15.00, 75.00),
    "claude-sonnet-4-7":           (3.00, 15.00),
}


def _openai_is_strict_reasoning_model(model: str) -> bool:
    """OpenAI o-series models reject custom sampling params; use max_completion_tokens only."""
    return bool(re.match(r"^o\d", model.lower().strip()))


def _fix_tool_schema(schema: Any) -> Any:
    """Recursively patch JSON Schema issues that OpenAI rejects.

    Known fixes:
    - array types missing ``items`` (OpenAI requires it; Anthropic does not).
    """
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "array" and "items" not in schema:
        schema["items"] = {}
    for val in schema.values():
        if isinstance(val, dict):
            _fix_tool_schema(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    _fix_tool_schema(item)
    return schema


def _sanitize_tools_for_openai(tools: List[dict]) -> List[dict]:
    """Return a copy of *tools* with schemas fixed for OpenAI's stricter validation."""
    import copy
    sanitized = copy.deepcopy(tools)
    for tool in sanitized:
        params = tool.get("function", {}).get("parameters")
        if params:
            _fix_tool_schema(params)
    return sanitized


def build_openai_chat_request_body(
    model: str,
    messages: List[dict],
    *,
    temperature: float,
    max_completion_tokens: int,
    tools: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Build a Chat Completions body that works across GPT and o-series models."""
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
    }
    if not _openai_is_strict_reasoning_model(model):
        body["temperature"] = temperature
    if tools:
        body["tools"] = _sanitize_tools_for_openai(tools)
    return body


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


def _provider_models(provider: str, config: Optional[dict] = None) -> List[str]:
    """Models known to actually exist for *provider* (live list for ollama)."""
    if provider == "ollama":
        return list_ollama_models(config) or []
    return KNOWN_MODELS.get(provider, [])


def get_fallback_chain(current_provider: str, current_model: str, config: Optional[dict] = None) -> list:
    """Return ordered list of (provider, model, needs_context_switch) fallback candidates.

    Strategy:
    1. Same provider, next model in its model list (no context switch needed)
    2. Other providers with valid API keys (context switch required)

    Ollama models come from the live server list (using the configured base
    URL when *config* is given), so an unreachable server (or one with no
    tool-capable models) simply contributes no candidates.
    """
    chain = []

    # Step 1: same-provider model fallbacks
    same_models = _provider_models(current_provider, config)
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
        fb_model = get_fallback_model(provider, config)
        if fb_model:
            chain.append((provider, fb_model, True))

    return chain


def get_fallback_model(provider: str, config: Optional[dict] = None) -> str:
    """Return the default model for a provider.

    For Ollama the preferred default is only used when it actually exists on
    the server; otherwise the first available (tool-capable) model is used,
    or "" when the server is unreachable/empty.
    """
    if provider == "ollama":
        available = list_ollama_models(config) or []
        preferred = DEFAULT_CHAT_MODEL_BY_PROVIDER.get("ollama", "")
        if preferred and ollama_model_matches(preferred, available):
            return preferred
        return available[0] if available else ""
    if provider in DEFAULT_CHAT_MODEL_BY_PROVIDER:
        return DEFAULT_CHAT_MODEL_BY_PROVIDER[provider]
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
        return error_response(str(exc))
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
    api_key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    model = config.get("chat_model", config.get("model", "gpt-4o-mini"))
    body = build_openai_chat_request_body(
        model,
        messages,
        temperature=0.7,
        max_completion_tokens=16384,
        tools=tools,
    )
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
    except urllib.error.HTTPError as exc:
        return error_response(format_http_api_error(exc))
    except Exception as exc:
        return error_response(str(exc))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message", json.dumps(err)) if isinstance(err, dict) else str(err)
        return error_response(msg)
    message = (data.get("choices") or [{}])[0].get("message", {})
    content = (message.get("content") or "").strip()
    if not content and message.get("reasoning"):
        content = str(message["reasoning"]).strip()
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": message.get("tool_calls"),
        "_usage": _normalize_usage(data, "openai"),
        "_model": body["model"],
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
        import copy
        body["tools"] = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "input_schema": _fix_tool_schema(copy.deepcopy(
                    tool["function"].get("parameters", {"type": "object", "properties": {}})
                )),
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
    except urllib.error.HTTPError as exc:
        return error_response(format_http_api_error(exc))
    except Exception as exc:
        return error_response(str(exc))

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


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks that qwen3 (and other
    reasoning models) embed in message content."""
    if not text or "<think>" not in text:
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # Unterminated <think> (stream cut off mid-thought): drop the tail.
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip()


def _convert_ollama_tool_calls(raw_tool_calls: list) -> Optional[List[dict]]:
    """Convert Ollama-native tool_calls to the OpenAI shape used internally.

    Ollama returns ``function.arguments`` as a dict; some models/versions
    return a JSON string instead — handle both without double-encoding.
    """
    if not raw_tool_calls:
        return None
    tool_calls = []
    for i, tool_call in enumerate(raw_tool_calls):
        fn = (tool_call or {}).get("function", {})
        arguments = fn.get("arguments", {})
        if isinstance(arguments, str):
            args_str = arguments if arguments.strip() else "{}"
        else:
            args_str = json.dumps(arguments)
        tool_calls.append({
            "id": f"ollama_{i}",
            "type": "function",
            "function": {
                "name": fn.get("name", ""),
                "arguments": args_str,
            },
        })
    return tool_calls or None


def raw_ollama(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    base_url = get_ollama_base_url(config)
    model = config.get("chat_model", config.get("model", "llama3.3"))
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    apply_ollama_request_options(body, config, model)
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
        return error_response(str(exc))
    message = data.get("message", {})
    return {
        "role": "assistant",
        "content": strip_think_blocks((message.get("content") or "").strip()),
        "tool_calls": _convert_ollama_tool_calls(message.get("tool_calls")),
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
    reasoning_parts: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            for chunk in _iter_sse(response):
                if chunk.get("error"):
                    err = chunk["error"]
                    if isinstance(err, dict):
                        msg = err.get("message") or json.dumps(err)
                    else:
                        msg = str(err)
                    return error_response(msg)
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})

                text = delta.get("content") or ""
                reasoning = delta.get("reasoning") or ""
                if reasoning:
                    reasoning_parts.append(reasoning)

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
    except urllib.error.HTTPError as exc:
        return error_response(format_http_api_error(exc))
    except Exception as exc:
        return error_response(str(exc))

    full_text = "".join(content_parts).strip()
    if not full_text and reasoning_parts:
        full_text = "".join(reasoning_parts).strip()
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
    api_key = os.environ.get(
        config.get("api_key_env", "CEREBRAS_API_KEY"), ""
    ).strip()
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
    api_key = os.environ.get(
        config.get("api_key_env", "OPENAI_API_KEY"), ""
    ).strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    model = config.get("chat_model", config.get("model", "gpt-4o-mini"))
    body = build_openai_chat_request_body(
        model,
        messages,
        temperature=0.7,
        max_completion_tokens=16384,
        tools=tools,
    )
    body["stream_options"] = {"include_usage": True}
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
    api_key = os.environ.get(
        config.get("api_key_env", "ANTHROPIC_API_KEY"), ""
    ).strip()
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
        import copy
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": _fix_tool_schema(copy.deepcopy(
                    t["function"].get("parameters", {"type": "object", "properties": {}})
                )),
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
                    return error_response(err.get("message", "unknown stream error"))

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
    except urllib.error.HTTPError as exc:
        return error_response(format_http_api_error(exc))
    except Exception as exc:
        return error_response(str(exc))

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
    base_url = get_ollama_base_url(config)
    model = config.get("chat_model", config.get("model", "llama3.3"))
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    apply_ollama_request_options(body, config, model)
    if tools:
        body["tools"] = tools

    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    content_parts: list[str] = []
    raw_tool_calls: list = []
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

                if data.get("error"):
                    return error_response(str(data['error']))

                msg = data.get("message", {})
                # Tool calls arrive in intermediate chunks (done:false), NOT in
                # the final done chunk — accumulate them across the stream.
                if msg.get("tool_calls"):
                    raw_tool_calls.extend(msg["tool_calls"])
                # Skip msg.get("thinking") tokens (qwen3 et al.) — reasoning is
                # not part of the reply.
                if msg.get("content"):
                    content_parts.append(msg["content"])
                    if on_token:
                        on_token(msg["content"])

                if data.get("done"):
                    final_data = data
                    break
    except Exception as exc:
        return error_response(str(exc))

    full_text = strip_think_blocks("".join(content_parts).strip())

    return {
        "role": "assistant",
        "content": full_text,
        "tool_calls": _convert_ollama_tool_calls(raw_tool_calls),
        "_usage": _normalize_usage(final_data, "ollama"),
        "_model": model,
    }


STREAM_FNS = {
    "cerebras": stream_cerebras,
    "openai": stream_openai,
    "anthropic": stream_anthropic,
    "ollama": stream_ollama,
}

