"""Provider adapters for OpenAI-compatible, Anthropic, Cerebras, and Ollama backends."""

from __future__ import annotations

import concurrent.futures
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


# Every cloud catalog entry must pass the live audit (existence via the
# provider's model-list endpoint + a forced native tool-call probe) before
# it is listed — run `python tools/audit_models.py` and record the date in
# MODEL_VERIFIED below. See tests/test_provider_compat.py
# (TestModelVerificationAnnotations): adding a model without an audit
# annotation fails the suite.
KNOWN_MODELS = {
    # UNVERIFIED as of the 2026-09-14 audit: no CEREBRAS_API_KEY was
    # available on the audit machine, so this catalog is carried forward
    # from earlier live checks rather than re-blessed. Re-audit with a key
    # before relying on it.
    "cerebras": [
        "gpt-oss-120b",
        "gemma-4-31b",
        "zai-glm-4.7",
    ],
    # AWS Bedrock via its OpenAI-compatible endpoint (us-east-2), all three
    # re-verified with forced tool-call probes 2026-09-14. Kimi K3 is still
    # not in Bedrock's managed catalog; use OpenRouter for it.
    "bedrock": [
        "moonshotai.kimi-k2.5",
        "moonshot.kimi-k2-thinking",
        # Z.AI GLM-5 (744B MoE, ~44B active) — biggest GLM on Bedrock,
        # verified live in us-east-2 (also us-east-1/us-west-2).
        "zai.glm-5",
    ],
    # OpenRouter (openrouter.ai) — OpenAI-compatible gateway to frontier
    # models not available on other providers here. All listed models
    # re-verified for native tool calling 2026-09-14.
    "openrouter": [
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
    ],
    # Conch requires native tool calling over /v1/chat/completions, so only
    # models that pass the forced-tool probe there are listed (e.g. o1-mini
    # is excluded: it supports neither tools nor system messages).
    #
    # Pruned 2026-09-14:
    #   gpt-5.6 — does not exist; only the sol/terra/luna variants shipped.
    # Quarantined 2026-09-14 (exist, but are v1/responses-only and reject
    # tool calls on chat completions, which is the only API conch speaks):
    #   gpt-5.3-codex, gpt-5.4-pro, o3-pro, o1-pro
    # Quarantined 2026-09-23 for the same reason:
    #   gpt-6-astra — tool calling requires v1/responses, and it does not
    #   support reasoning_effort="none", so the chat-completions escape
    #   hatch the other GPT-6 models have is unavailable.
    # Re-add them only if/when conch grows a v1/responses adapter.
    "openai": [
        # gpt-6-{sol,luna} and gpt-5.6-{sol,terra,luna} tool-call on chat
        # completions only with reasoning_effort="none" (sent automatically
        # by build_openai_chat_request_body; see
        # _openai_tools_require_effort_none). gpt-6 verified live
        # 2026-09-23: with effort "none" the forced probe passes; without
        # it the API returns 400 pointing at v1/responses.
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
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
    ],
    # Pruned 2026-09-14 (absent from /v1/models AND 404 on a direct probe):
    #   claude-fable-5, claude-sonnet-4-7
    # claude-haiku-4-5 is kept although the list endpoint only shows the
    # dated ID (claude-haiku-4-5-20251001): the alias resolves and passed
    # the tool probe.
    #
    # claude-opus-5-5 (released 2026-09-22) rejects FORCED tool_choice —
    # both {"type": "tool"} and {"type": "any"} return HTTP 400
    # ('tool_choice: type "tool" and "any" are not supported for this
    # model'); {"type": "auto"} works and the model calls the offered tool
    # reliably (verified live 2026-09-23). See
    # anthropic_forced_tool_choice_supported(), honored by ask mode and
    # tools/audit_models.py. Note: Anthropic's "preserved thinking" applies
    # to Opus 5.5 (and Fable 5.1) for API accounts created on or after
    # 2026-08-31 — editing prior assistant context can be rejected on such
    # accounts, which affects history-rewriting flows like compaction.
    "anthropic": [
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
    ],
    # Ollama models are discovered live from the server's /api/tags
    # (see list_ollama_models); no hardcoded list.
    "ollama": [],
    # Custom OpenAI-compatible endpoints define their model in config
    # (custom_model); nothing is hardcoded.
    "custom": [],
}

# Audit trail for every cloud catalog entry: the date the model last passed
# the live existence + forced-tool-call audit (tools/audit_models.py), or
# "unverified YYYY-MM-DD (<reason>)" when the audit could not run. A catalog
# entry without an annotation here fails the test suite.
_CEREBRAS_UNVERIFIED = "unverified 2026-09-23 (no CEREBRAS_API_KEY on audit machine)"
MODEL_VERIFIED = {
    "gpt-oss-120b": _CEREBRAS_UNVERIFIED,
    "gemma-4-31b": _CEREBRAS_UNVERIFIED,
    "zai-glm-4.7": _CEREBRAS_UNVERIFIED,
    "moonshotai.kimi-k2.5": "2026-09-23",
    "moonshot.kimi-k2-thinking": "2026-09-23",
    "zai.glm-5": "2026-09-23",
    "moonshotai/kimi-k3": "2026-09-23",
    "z-ai/glm-5.2": "2026-09-23",
    "deepseek/deepseek-v4-pro": "2026-09-23",
    "deepseek/deepseek-v4-flash": "2026-09-23",
    "gpt-6-sol": "2026-09-23",
    "gpt-6-luna": "2026-09-23",
    "gpt-5.6-sol": "2026-09-23",
    "gpt-5.6-terra": "2026-09-23",
    "gpt-5.6-luna": "2026-09-23",
    "gpt-5.5": "2026-09-23",
    "gpt-5.4": "2026-09-23",
    "gpt-5.4-mini": "2026-09-23",
    "gpt-5.4-nano": "2026-09-23",
    "gpt-5-mini": "2026-09-23",
    "gpt-5-nano": "2026-09-23",
    "gpt-4.1": "2026-09-23",
    "gpt-4.1-mini": "2026-09-23",
    "gpt-4.1-nano": "2026-09-23",
    "gpt-4o": "2026-09-23",
    "gpt-4o-mini": "2026-09-23",
    "o4-mini": "2026-09-23",
    "o3": "2026-09-23",
    "o3-mini": "2026-09-23",
    "o1": "2026-09-23",
    "claude-opus-5-5": "2026-09-23",
    "claude-opus-5": "2026-09-23",
    "claude-sonnet-5": "2026-09-23",
    "claude-opus-4-8": "2026-09-23",
    "claude-opus-4-7": "2026-09-23",
    "claude-sonnet-4-6": "2026-09-23",
    "claude-opus-4-6": "2026-09-23",
    "claude-haiku-4-5": "2026-09-23",
    "claude-sonnet-4-5-20250929": "2026-09-23",
}

DEFAULT_API_KEY_ENVS = {
    "cerebras": "CEREBRAS_API_KEY",
    # Long-term Bedrock API key (IAM service-specific credential); the env
    # var name is AWS's documented standard for Bedrock bearer auth.
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": "",
    "custom": "",
}

PROVIDER_TOOL_LIMITS = {
    "openai": 128,
    "cerebras": 128,
    "bedrock": 128,
    "openrouter": 128,
    # Local models drown in large tool lists: cap hard and select the most
    # relevant tools per turn (see tooling.select_relevant_tools).
    "ollama": 12,
    # Custom endpoints usually front local models too — stay conservative.
    "custom": 12,
}

# Used for `/provider` and tool `set_provider` — stable defaults, not KNOWN_MODELS[0].
# The ollama entry is only a *preference*: it is used when the model actually
# exists on the configured server (see get_fallback_model).
DEFAULT_CHAT_MODEL_BY_PROVIDER = {
    "cerebras": "gpt-oss-120b",
    "bedrock": "moonshotai.kimi-k2.5",
    "openrouter": "moonshotai/kimi-k3",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5",
    "ollama": "llama3.3",
    "custom": "",  # defined entirely by config (custom_model)
}


# Known context windows (tokens) for cloud models. Ollama windows are
# discovered live from the server instead (see get_ollama_context_length).
MODEL_CONTEXT_WINDOWS = {
    # cerebras
    "gpt-oss-120b": 131072,
    "gemma-4-31b": 131072,
    "zai-glm-4.7": 131072,
    # bedrock (Moonshot Kimi, Z.AI GLM)
    "moonshotai.kimi-k2.5": 262144,
    "moonshot.kimi-k2-thinking": 262144,
    "zai.glm-5": 202752,  # Bedrock's listed window (model card says 200K-class)
    # openrouter (windows per openrouter.ai/api/v1/models, verified 2026-07)
    "moonshotai/kimi-k3": 1048576,
    "z-ai/glm-5.2": 1048576,
    "deepseek/deepseek-v4-pro": 1048576,
    "deepseek/deepseek-v4-flash": 1048576,
    # openai
    "gpt-6-sol": 1050000,
    "gpt-6-luna": 1050000,
    "gpt-5.6-sol": 1050000,
    "gpt-5.6-terra": 1050000,
    "gpt-5.6-luna": 1050000,
    "gpt-5.5": 1050000,
    "gpt-5.4": 400000,
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
    "o1": 200000,
    # anthropic (1M is the default window from Sonnet 4.6 / Opus 4.6 onward)
    "claude-opus-5-5": 1000000,
    "claude-opus-5": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-opus-4-8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-opus-4-6": 1000000,
    "claude-haiku-4-5": 200000,
    "claude-sonnet-4-5-20250929": 200000,
}

# Conservative fallbacks when a model isn't in the table. (Ollama windows
# derive from the num_ctx sent on requests instead — see get_ollama_num_ctx;
# the entry here documents the server's own default when num_ctx is omitted.)
PROVIDER_DEFAULT_CONTEXT_WINDOWS = {
    "cerebras": 131072,
    "bedrock": 131072,
    "openrouter": 131072,
    "openai": 128000,
    "anthropic": 200000,
    "ollama": 4096,
    "custom": 32768,  # override with custom_context_window in config
}


# ---------------------------------------------------------------------------
# Ollama model discovery (live from the server's /api/tags)
# ---------------------------------------------------------------------------

OLLAMA_TAGS_TIMEOUT = 2.0  # short so the UI never hangs on an unreachable server
_OLLAMA_TAGS_TTL_OK = 30.0
_OLLAMA_TAGS_TTL_FAIL = 5.0
_ollama_tags_cache: Dict[str, tuple] = {}  # base_url -> (fetched_at, records-or-None)
_ollama_caps_cache: Dict[tuple, tuple] = {}  # (base_url, digest) -> (checked_at, bool-or-None)
_ollama_ctx_cache: Dict[tuple, tuple] = {}  # (base_url, digest) -> (checked_at, context-or-None)
_ollama_show_cache: Dict[tuple, tuple] = {}  # (base_url, digest) -> (checked_at, payload-or-None)
_ollama_ps_cache: Dict[str, tuple] = {}  # base_url -> (checked_at, payload-or-None)
_local_model_cache_lock = threading.RLock()


def is_local_inference_url(url: str) -> bool:
    """Conservative local/LAN URL check used by local_only mode."""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not hostname:
        return False
    if hostname in ("localhost", "host.docker.internal"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
        return not address.is_global
    except ValueError:
        pass
    return (
        "." not in hostname
        or hostname.endswith(
            # .ts.net covers tailnet MagicDNS names; tailnet IPs
            # (100.64.0.0/10 shared space) already pass the is_global
            # check above.
            (".local", ".lan", ".internal", ".home.arpa", ".ts.net")
        )
    )


def local_endpoint_policy_error(
    provider: str, url: str, config: Optional[dict]
) -> str:
    from .config import local_only_enabled

    if local_only_enabled(config or {}, provider) and not is_local_inference_url(url):
        return (
            f"local_only blocks non-local {provider} endpoint {url}; "
            "set local_only=false only if this is intentional"
        )
    return ""


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


def _ollama_show(model: str, base_url: str, timeout: float) -> dict:
    """POST /api/show for *model*. Sends both ``model`` and ``name`` keys:
    newer servers read ``model``, servers from before the name→model rename
    only read ``name`` — sending both works everywhere."""
    req = urllib.request.Request(
        f"{base_url}/api/show",
        data=json.dumps({"model": model, "name": model}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _ollama_model_records(
    config: Optional[dict] = None,
    *,
    timeout: float = OLLAMA_TAGS_TIMEOUT,
    force_refresh: bool = False,
) -> Optional[List[dict]]:
    base_url = get_ollama_base_url(config)
    if local_endpoint_policy_error("ollama", base_url, config):
        return None
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _ollama_tags_cache.get(base_url)
        if cached is not None and not force_refresh:
            fetched_at, records = cached
            ttl = (
                _OLLAMA_TAGS_TTL_OK
                if records is not None
                else _OLLAMA_TAGS_TTL_FAIL
            )
            if now - fetched_at < ttl:
                return records
    try:
        with urllib.request.urlopen(
            f"{base_url}/api/tags", timeout=timeout
        ) as response:
            data = json.loads(response.read().decode())
        records = []
        for item in data.get("models", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                continue
            records.append(
                {
                    "name": name,
                    "digest": str(item.get("digest") or "").strip(),
                    "size": item.get("size"),
                    "modified_at": item.get("modified_at"),
                }
            )
    except Exception:
        records = None
    with _local_model_cache_lock:
        _ollama_tags_cache[base_url] = (now, records)
    return records


def _resolve_ollama_record(model: str, records: List[dict]) -> Optional[dict]:
    for record in records:
        if record["name"] == model:
            return record
    if ":" not in model:
        for record in records:
            if record["name"].split(":", 1)[0] == model:
                return record
    return None


def _ollama_show_cached(
    model: str,
    config: Optional[dict] = None,
    *,
    timeout: float = OLLAMA_TAGS_TIMEOUT,
) -> tuple:
    records = _ollama_model_records(config, timeout=timeout)
    if records is None:
        return None, None
    record = _resolve_ollama_record(model, records)
    if record is None:
        return None, None
    base_url = get_ollama_base_url(config)
    identity = record.get("digest") or record["name"]
    key = (base_url, identity)
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _ollama_show_cache.get(key)
        if cached is not None:
            checked_at, payload = cached
            ttl = (
                _OLLAMA_TAGS_TTL_OK
                if payload is not None
                else _OLLAMA_TAGS_TTL_FAIL
            )
            if now - checked_at < ttl:
                return payload, record
    try:
        payload = _ollama_show(record["name"], base_url, timeout)
        if not isinstance(payload, dict):
            payload = None
    except Exception:
        payload = None
    with _local_model_cache_lock:
        _ollama_show_cache[key] = (now, payload)
    return payload, record


def ollama_model_supports_tools(
    model: str,
    config: Optional[dict] = None,
    *,
    timeout: float = OLLAMA_TAGS_TIMEOUT,
) -> Optional[bool]:
    """Check via POST /api/show whether *model* advertises the "tools" capability.

    Returns True only for an explicit ``tools`` capability. Missing capability
    metadata is not guessed from templates: Conch supports only models the
    configured server positively identifies as native tool callers.
    """
    base_url = get_ollama_base_url(config)
    data, record = _ollama_show_cached(model, config, timeout=timeout)
    if record is None:
        return None
    key = (base_url, record.get("digest") or record["name"])
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _ollama_caps_cache.get(key)
    if cached is not None:
        ttl = (
            _OLLAMA_TAGS_TTL_OK
            if cached[1] is not None
            else _OLLAMA_TAGS_TTL_FAIL
        )
        if now - cached[0] < ttl:
            return cached[1]
    if data is None:
        supports = None
    else:
        caps = data.get("capabilities")
        supports = bool(
            isinstance(caps, list)
            and any(str(cap).lower() == "tools" for cap in caps)
        )
    with _local_model_cache_lock:
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
    data, record = _ollama_show_cached(model, config, timeout=timeout)
    if record is None:
        return None
    key = (base_url, record.get("digest") or record["name"])
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _ollama_ctx_cache.get(key)
    if cached is not None:
        ttl = (
            _OLLAMA_TAGS_TTL_OK
            if cached[1] is not None
            else _OLLAMA_TAGS_TTL_FAIL
        )
        if now - cached[0] < ttl:
            return cached[1]
    ctx = None
    if data is not None:
        # Older servers don't return model_info at all — treated as unknown.
        model_info = data.get("model_info") or {}
        for info_key, value in model_info.items():
            if info_key.endswith(".context_length") and isinstance(value, int) and value > 0:
                ctx = value
                break
    with _local_model_cache_lock:
        _ollama_ctx_cache[key] = (now, ctx)
    return ctx


DEFAULT_OLLAMA_CONTEXT_WINDOW = 4096
# keep_alive keeps the model loaded between turns so the KV cache survives.
DEFAULT_OLLAMA_KEEP_ALIVE = "10m"


def get_ollama_num_ctx(
    model: str, config: Optional[dict] = None
) -> Optional[int]:
    """Return an explicit num_ctx override, or None for server-managed sizing."""
    try:
        num_ctx = int((config or {}).get("ollama_num_ctx", 0) or 0)
    except (TypeError, ValueError):
        num_ctx = 0
    if num_ctx <= 0:
        return None
    model_max = get_ollama_context_length(model, config)
    if model_max:
        num_ctx = min(num_ctx, model_max)
    return num_ctx


def get_ollama_running_context(
    model: str,
    config: Optional[dict] = None,
    *,
    timeout: float = OLLAMA_TAGS_TIMEOUT,
) -> Optional[int]:
    """Read the effective loaded context from /api/ps when available."""
    base_url = get_ollama_base_url(config)
    if local_endpoint_policy_error("ollama", base_url, config):
        return None
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _ollama_ps_cache.get(base_url)
        if cached is not None and now - cached[0] < _OLLAMA_TAGS_TTL_FAIL:
            data = cached[1]
        else:
            data = None
            cached = None
    if cached is None:
        try:
            with urllib.request.urlopen(
                f"{base_url}/api/ps", timeout=timeout
            ) as response:
                data = json.loads(response.read().decode())
        except Exception:
            data = None
        with _local_model_cache_lock:
            _ollama_ps_cache[base_url] = (now, data)
    for item in (data or {}).get("models", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "")
        if ollama_model_matches(model, [name]):
            value = item.get("context_length")
            if isinstance(value, int) and value > 0:
                return value
    return None


def get_ollama_effective_context(
    model: str, config: Optional[dict] = None
) -> int:
    explicit = get_ollama_num_ctx(model, config)
    if explicit:
        return explicit
    running = get_ollama_running_context(model, config)
    if running:
        return running
    try:
        configured = int(
            (config or {}).get("ollama_context_window", 0) or 0
        )
    except (TypeError, ValueError):
        configured = 0
    model_max = get_ollama_context_length(model, config)
    if configured > 0:
        return min(configured, model_max) if model_max else configured
    return min(
        DEFAULT_OLLAMA_CONTEXT_WINDOW,
        model_max or DEFAULT_OLLAMA_CONTEXT_WINDOW,
    )


def _config_float(
    config: Optional[dict], key: str, default: float
) -> float:
    try:
        return float((config or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _estimated_local_prompt_tokens(
    messages: List[dict], tools: Optional[List[dict]]
) -> int:
    payload = json.dumps(
        {"messages": messages, "tools": tools or []},
        ensure_ascii=False,
        default=str,
    )
    # Conservative before model-specific runtime calibration is available.
    return max(1, (len(payload) + 2) // 3)


def _clamp_output_to_context(
    requested: int,
    context_window: int,
    messages: List[dict],
    tools: Optional[List[dict]],
) -> int:
    prompt = _estimated_local_prompt_tokens(messages, tools)
    safety = min(128, max(1, context_window // 20))
    return max(1, min(requested, context_window - prompt - safety))


def apply_ollama_request_options(body: Dict[str, Any], config: dict, model: str) -> None:
    """Apply explicit/local-friendly generation options to /api/chat."""
    options: Dict[str, Any] = {}
    num_ctx = get_ollama_num_ctx(model, config)
    if num_ctx:
        options["num_ctx"] = num_ctx
    options["temperature"] = _config_float(
        config,
        "ollama_temperature",
        _config_float(config, "temperature", 0.2),
    )
    try:
        num_predict = int(
            config.get(
                "ollama_num_predict",
                config.get("max_output_tokens", 0),
            )
            or 0
        )
    except (TypeError, ValueError):
        num_predict = 0
    if num_predict > 0:
        options["num_predict"] = num_predict
    body["options"] = options
    if "ollama_think" in config:
        value = str(config.get("ollama_think", "")).strip().lower()
        if value in ("true", "1", "yes", "on"):
            body["think"] = True
        elif value in ("false", "0", "no", "off"):
            body["think"] = False
        elif value:
            body["think"] = value
    keep_alive = str((config or {}).get("ollama_keep_alive", "") or "").strip()
    body["keep_alive"] = keep_alive or DEFAULT_OLLAMA_KEEP_ALIVE


def get_context_window(provider: str, model: str, config: Optional[dict] = None) -> int:
    """Best-known context window (tokens) for *model* on *provider*.

    Cloud providers use the static MODEL_CONTEXT_WINDOWS table. Ollama uses
    an explicit num_ctx, the loaded /api/ps value, or a conservative pre-load
    fallback. Custom providers use llama.cpp props/config metadata.
    """
    provider = (provider or "").lower()
    if provider == "ollama":
        return get_ollama_effective_context(model, config)
    if provider == "custom":
        return get_custom_context_window(config, model)
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

    The default is fail closed: only models with a positive ``tools``
    capability from /api/show are returned.
    """
    records = _ollama_model_records(
        config, timeout=timeout, force_refresh=force_refresh
    )
    if records is None:
        return None
    models = [record["name"] for record in records]
    if tool_capable_only:
        workers = min(8, max(1, len(models)))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers
        ) as pool:
            support = list(
                pool.map(
                    lambda name: ollama_model_supports_tools(
                        name, config, timeout=timeout
                    ),
                    models,
                )
            )
        models = [
            model
            for model, supports in zip(models, support)
            if supports is True
        ]
    return models


def suggest_models(model: str, candidates: List[str], limit: int = 3) -> List[str]:
    """Close matches for a mistyped model name (difflib-based)."""
    import difflib
    return difflib.get_close_matches(model, candidates, n=limit, cutoff=0.4)


def validate_model_for_provider(provider: str, model: str, config: Optional[dict] = None) -> tuple:
    """Validate that *model* exists for *provider* on a switch.

    Returns (ok, reason): True when valid; False when it must be rejected;
    None only when a live local service cannot be reached.
    """
    provider = (provider or "").lower()
    model = (model or "").strip()
    if not model:
        return False, "no model given"
    if provider == "ollama":
        return validate_ollama_model(model, config)
    if provider == "custom":
        return validate_custom_model(model, config)
    known = KNOWN_MODELS.get(provider)
    if known is None:
        return None, f"unknown provider '{provider}'"
    if model in known:
        return True, ""
    suggestions = suggest_models(model, known)
    if suggestions:
        hint = f"Did you mean: {', '.join(suggestions)}?"
    else:
        shown = ", ".join(known[:8])
        more = ", ..." if len(known) > 8 else ""
        hint = f"Known {provider} models: {shown}{more}"
    return False, f"unknown {provider} model '{model}'. {hint}"


def check_ollama_health(config: Optional[dict] = None) -> bool:
    """Fresh /api/tags ping (bypasses the cache) — used for preflight after
    a failed turn (plan 3.3)."""
    return list_ollama_models(config, force_refresh=True, tool_capable_only=False) is not None


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
    policy_error = local_endpoint_policy_error(
        "ollama", get_ollama_base_url(config), config
    )
    if policy_error:
        return False, policy_error
    installed = list_ollama_models(config, tool_capable_only=False)
    if installed is None:
        return None, f"Ollama server unreachable at {get_ollama_base_url(config)}"
    if not ollama_model_matches(model, installed):
        return False, f"model '{model}' is not installed on the Ollama server"
    resolved = model if model in installed else next(
        (name for name in installed if name.split(":", 1)[0] == model), model
    )
    support = ollama_model_supports_tools(resolved, config)
    if support is not True:
        if support is None:
            return (
                False,
                f"model '{model}' has no verifiable native tool capability",
            )
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
    "gpt-oss-120b":                (0.35, 0.75),
    "gemma-4-31b":                 (2.15, 2.70),
    "zai-glm-4.7":                 (0.00, 0.00),
    "moonshotai.kimi-k2.5":        (0.60, 3.00),
    "moonshot.kimi-k2-thinking":   (0.60, 2.50),
    # Bedrock on-demand US-region rates (aws.amazon.com/bedrock/pricing).
    "zai.glm-5":                   (1.00, 3.20),
    # OpenRouter rates (openrouter.ai/api/v1/models, verified 2026-08).
    "moonshotai/kimi-k3":          (3.00, 15.00),
    "z-ai/glm-5.2":                (0.98, 3.08),
    "deepseek/deepseek-v4-pro":    (0.435, 0.87),
    "deepseek/deepseek-v4-flash":  (0.14, 0.28),
    # GPT-6 rates per developers.openai.com/api/docs/pricing (2026-09-23).
    "gpt-6-sol":                   (2.00, 10.00),
    "gpt-6-luna":                  (0.10, 0.50),
    "gpt-5.6-sol":                 (5.00, 30.00),
    "gpt-5.6-terra":               (2.00, 12.00),
    "gpt-5.6-luna":                (0.20, 1.20),
    "gpt-5.5":                     (5.00, 30.00),
    "gpt-5.4":                     (2.50, 15.00),
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
    "o1":                          (15.00, 60.00),
    # Opus 5.5 per platform.claude.com pricing (2026-09-23).
    "claude-opus-5-5":             (4.00, 20.00),
    "claude-opus-5":               (5.00, 25.00),
    "claude-sonnet-5":             (2.00, 10.00),
    "claude-sonnet-4-6":           (3.00, 15.00),
    "claude-opus-4-6":             (5.00, 25.00),
    "claude-haiku-4-5":            (1.00, 5.00),
    "claude-sonnet-4-5-20250929":  (3.00, 15.00),
    "claude-opus-4-8":             (5.00, 25.00),
    "claude-opus-4-7":             (5.00, 25.00),
}


def _openai_is_strict_reasoning_model(model: str) -> bool:
    """OpenAI reasoning models reject custom sampling params (temperature
    must stay at its default); use max_completion_tokens only.

    Covers the o-series plus the gpt-5/gpt-5.5 reasoning family. Verified
    live 2026-09-14: gpt-5.5, gpt-5-mini, and gpt-5-nano return HTTP 400
    for any non-default temperature, while gpt-5.4* and gpt-5.6* accept it.
    """
    name = model.lower().strip()
    return bool(
        re.match(r"^o\d", name)
        or re.match(r"^gpt-5(\.5)?(-(mini|nano|pro|chat-latest))?$", name)
    )


def _anthropic_max_tokens(model: str) -> int:
    """Gen-5 Claude models think by default; leave room for thinking + reply."""
    name = (model or "").lower()
    if any(tag in name for tag in ("-opus-5", "-sonnet-5", "-fable-5", "-mythos-5")):
        return 32768
    return 16384


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
        if _openai_tools_require_effort_none(model):
            body["reasoning_effort"] = "none"
    return body


def _openai_tools_require_effort_none(model: str) -> bool:
    """Models that reject function tools on chat completions unless
    reasoning_effort is "none" (the API's 400 points at v1/responses
    otherwise).

    gpt-5.6-{sol,terra,luna} verified live 2026-09-14; gpt-6-{sol,luna}
    verified live 2026-09-23 (with "none" the forced probe passes; without
    it: "Function tools with reasoning_effort are not supported ... use
    /v1/responses or set reasoning_effort to 'none'"). gpt-6-astra is NOT
    handled here: it does not support effort "none" at all, so it stays
    out of the catalog until a v1/responses adapter exists.
    """
    name = model.lower().strip()
    return name.startswith("gpt-5.6") or name.startswith(("gpt-6-sol", "gpt-6-luna"))


def anthropic_forced_tool_choice_supported(model: str) -> bool:
    """Whether the model accepts forced tool_choice ({"type": "tool"/"any"}).

    claude-opus-5-5 returns HTTP 400 for both forced forms ('tool_choice:
    type "tool" and "any" are not supported for this model'; verified live
    2026-09-23) — Anthropic documents the same for claude-fable-5-1. For
    these models callers must use {"type": "auto"} and validate that the
    tool_use block actually came back (ask mode and tools/audit_models.py
    both do).
    """
    name = model.lower().strip()
    return not name.startswith(("claude-opus-5-5", "claude-fable-5-1"))


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated cost in USD for a given token count."""
    rate = MODEL_PRICING.get(model, (0.0, 0.0))
    return (input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000


def _attach_server_timings(target: dict, data: dict) -> None:
    """Record server-reported generation time onto a usage dict.

    llama.cpp (and OpenAI-compatible servers that mirror it) return a
    ``timings`` object: ``predicted_ms`` is the generation phase in
    milliseconds, with ``predicted_n``/``predicted_per_second`` as an
    alternative derivation. When present, the turn's tok/s is exact
    rather than a wall-clock estimate.
    """
    timings = data.get("timings") or {}
    if not isinstance(timings, dict):
        return
    seconds = 0.0
    try:
        seconds = float(timings.get("predicted_ms") or 0) / 1000.0
        if seconds <= 0:
            n = float(timings.get("predicted_n") or 0)
            per_second = float(timings.get("predicted_per_second") or 0)
            if n > 0 and per_second > 0:
                seconds = n / per_second
    except (TypeError, ValueError):
        return
    if seconds > 0:
        target["gen_seconds"] = seconds


def _normalize_usage(data: dict, provider: str) -> dict:
    """Extract a uniform usage dict from any provider's raw API response.

    Besides token counts, this carries ``gen_seconds`` — the model's own
    generation time — whenever the backend reports one (Ollama's
    ``eval_duration``, llama.cpp's ``timings``). Turns whose calls all
    have it show an exact tok/s; anything else falls back to a labeled
    wall-clock estimate.
    """
    if provider in ("cerebras", "openai", "bedrock", "openrouter"):
        usage = data.get("usage", {})
        result = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }
        _attach_server_timings(result, data)
        return result
    if provider == "anthropic":
        usage = data.get("usage", {})
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
    if provider == "ollama":
        result = {
            "input_tokens": data.get("prompt_eval_count", 0),
            "output_tokens": data.get("eval_count", 0),
        }
        try:
            eval_ns = float(data.get("eval_duration") or 0)
        except (TypeError, ValueError):
            eval_ns = 0.0
        if eval_ns > 0:
            result["gen_seconds"] = eval_ns / 1e9
        return result
    return {"input_tokens": 0, "output_tokens": 0}


CROSS_PROVIDER_FALLBACK_ORDER = [
    "cerebras",
    "anthropic",
    "openai",
    "bedrock",
    "openrouter",
    "ollama",
    "custom",
]


def _has_key(provider: str) -> bool:
    key_env = DEFAULT_API_KEY_ENVS.get(provider, "")
    if not key_env:
        return provider in ("ollama", "custom")
    return bool(os.environ.get(key_env, "").strip())


def _provider_models(provider: str, config: Optional[dict] = None) -> List[str]:
    """Models known to actually exist for *provider* (live list for ollama)."""
    if provider == "ollama":
        return list_ollama_models(config) or []
    if provider == "custom":
        return list_custom_models(config) or []
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

    # Step 1.5: the llama-idx registry tier — other up, tool-verified
    # boxes from the owned fleet (same-flavor first, largest context
    # first), before any cloud cross-provider hop. Entries keep their
    # namespaced name; the runtime resolves flavor/base_url at use.
    try:
        from .llamaidx import llamaidx_fallback_candidates

        chain.extend(
            llamaidx_fallback_candidates(config, current_provider, current_model)
        )
    except Exception:
        pass  # discovery must never break fallback

    # Step 2: cross-provider fallbacks. Local sessions are isolated from
    # cloud providers by default; opting out requires local_only=false.
    from .config import local_only_enabled

    local_only = local_only_enabled(config or {}, current_provider)
    for provider in CROSS_PROVIDER_FALLBACK_ORDER:
        if provider == current_provider:
            continue
        if local_only and provider not in ("ollama", "custom"):
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
    if provider == "custom":
        available = list_custom_models(config) or []
        configured = (
            (config or {}).get("custom_model")
            or (config or {}).get("chat_model")
            or (config or {}).get("model")
            or ""
        ).strip()
        return configured if configured in available else (
            available[0] if available else ""
        )
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
        "model": config.get("chat_model", config.get("model", "gpt-oss-120b")),
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
    model = config.get("chat_model", config.get("model", "claude-sonnet-5"))
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": _anthropic_max_tokens(model),
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


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>|<reasoning>.*?</reasoning>", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """Remove <think>/<reasoning> blocks that reasoning models (qwen3,
    kimi-k2-thinking, ...) embed in message content."""
    if not text or ("<think>" not in text and "<reasoning>" not in text):
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # Unterminated block (stream cut off mid-thought): drop the tail.
    for tag in ("<think>", "<reasoning>"):
        if tag in cleaned:
            cleaned = cleaned.split(tag, 1)[0]
    return cleaned.strip()


def _convert_ollama_tool_calls(raw_tool_calls: list) -> Optional[List[dict]]:
    """Convert Ollama-native tool_calls to the OpenAI shape used internally.

    Ollama returns ``function.arguments`` as a dict; some models/versions
    return a JSON string instead — handle both without double-encoding.
    """
    if isinstance(raw_tool_calls, dict):
        raw_tool_calls = [raw_tool_calls]
    if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
        return None
    tool_calls = []
    for i, tool_call in enumerate(raw_tool_calls):
        fn = (
            tool_call.get("function", {})
            if isinstance(tool_call, dict)
            else {}
        )
        if not isinstance(fn, dict):
            fn = {}
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
    ok, reason = validate_ollama_model(model, config)
    if ok is not True:
        return error_response(reason or f"Ollama model '{model}' is not verified")
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    apply_ollama_request_options(body, config, model)
    if tools:
        body["tools"] = _sanitize_tools_for_openai(tools)
    if body.get("options", {}).get("num_predict"):
        body["options"]["num_predict"] = _clamp_output_to_context(
            body["options"]["num_predict"],
            get_ollama_effective_context(model, config),
            messages,
            tools,
        )
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=_config_float(config, "ollama_timeout", 120.0)
        ) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        return error_response(str(exc))
    with _local_model_cache_lock:
        _ollama_ps_cache.pop(base_url, None)
    message = data.get("message", {})
    return {
        "role": "assistant",
        "content": strip_think_blocks((message.get("content") or "").strip()),
        "tool_calls": _convert_ollama_tool_calls(message.get("tool_calls")),
        "_usage": _normalize_usage(data, "ollama"),
        "_model": body["model"],
    }


# ---------------------------------------------------------------------------
# AWS Bedrock via its OpenAI-compatible chat completions endpoint.
# Auth is a long-term Bedrock API key (IAM service-specific credential) sent
# as a bearer token — no SigV4/boto3 needed, keeping conch dependency-free.
# Config:
#   provider=bedrock
#   bedrock_region=us-east-2            (or AWS_REGION/AWS_DEFAULT_REGION)
#   api_key_env=AWS_BEARER_TOKEN_BEDROCK (default)
# ---------------------------------------------------------------------------

DEFAULT_BEDROCK_REGION = "us-east-2"


def get_bedrock_base_url(config: Optional[dict] = None) -> str:
    config = config or {}
    region = (config.get("bedrock_region") or "").strip()
    if not region:
        region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        region = DEFAULT_BEDROCK_REGION
    return f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"


def _bedrock_headers(config: dict) -> Dict[str, str]:
    api_key = os.environ.get(config.get("api_key_env") or "AWS_BEARER_TOKEN_BEDROCK", "").strip()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "conch/1.0",
    }


def _bedrock_body(config: dict, messages: List[dict], tools: Optional[List[dict]]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "moonshotai.kimi-k2.5")),
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 16384,
    }
    if tools:
        body["tools"] = _sanitize_tools_for_openai(tools)
    return body


def raw_bedrock(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    api_key = os.environ.get(config.get("api_key_env") or "AWS_BEARER_TOKEN_BEDROCK", "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    body = _bedrock_body(config, messages, tools)
    req = urllib.request.Request(
        f"{get_bedrock_base_url(config)}/chat/completions",
        data=json.dumps(body).encode(),
        headers=_bedrock_headers(config),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
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
    # kimi-k2-thinking embeds <reasoning> blocks in content — strip them.
    content = strip_think_blocks((message.get("content") or "").strip())
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": message.get("tool_calls"),
        "_usage": _normalize_usage(data, "bedrock"),
        "_model": body["model"],
    }


class _ReasoningStreamFilter:
    """Suppress <reasoning>...</reasoning> spans from streamed display tokens.

    kimi-k2-thinking (Bedrock) streams its chain of thought as tagged spans
    inside ordinary content deltas; without filtering, the raw reasoning is
    printed live to the terminal. Tags can be split across chunk boundaries,
    so a small tail is buffered until it can't be a tag prefix anymore.
    """

    OPEN = "<reasoning>"
    CLOSE = "</reasoning>"

    def __init__(self, on_token):
        self._on_token = on_token
        self._buffer = ""
        self._inside = False

    def feed(self, text: str):
        self._buffer += text
        emitted: list = []
        while self._buffer:
            tag = self.CLOSE if self._inside else self.OPEN
            idx = self._buffer.find(tag)
            if idx >= 0:
                if not self._inside:
                    emitted.append(self._buffer[:idx])
                self._buffer = self._buffer[idx + len(tag):]
                self._inside = not self._inside
                continue
            if self._inside:
                # Drop consumed reasoning, keep only a possible split close tag.
                self._buffer = self._buffer[-(len(tag) - 1):]
            else:
                # Emit everything except a possible split open tag at the end.
                keep = 0
                for k in range(min(len(tag) - 1, len(self._buffer)), 0, -1):
                    if tag.startswith(self._buffer[-k:]):
                        keep = k
                        break
                emitted.append(self._buffer[:-keep] if keep else self._buffer)
                self._buffer = self._buffer[-keep:] if keep else ""
            break
        out = "".join(emitted)
        if out:
            self._on_token(out)

    def finish(self):
        if not self._inside and self._buffer:
            self._on_token(self._buffer)
            self._buffer = ""


def stream_bedrock(config: dict, messages: list, tools=None, on_token=None) -> dict:
    api_key = os.environ.get(config.get("api_key_env") or "AWS_BEARER_TOKEN_BEDROCK", "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    body = _bedrock_body(config, messages, tools)
    reasoning_filter = _ReasoningStreamFilter(on_token) if on_token else None
    result = _stream_openai_compat(
        f"{get_bedrock_base_url(config)}/chat/completions",
        _bedrock_headers(config),
        body,
        body["model"],
        "bedrock",
        reasoning_filter.feed if reasoning_filter else None,
    )
    if reasoning_filter:
        reasoning_filter.finish()
    if result.get("content"):
        result["content"] = strip_think_blocks(result["content"])
    return result


# ---------------------------------------------------------------------------
# OpenRouter (openrouter.ai) via its OpenAI-compatible endpoint. First-class
# home for frontier models conch can't get elsewhere (Kimi K3, GLM-5.2).
# Config:
#   provider=openrouter
#   api_key_env=OPENROUTER_API_KEY (default)
# ---------------------------------------------------------------------------

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _openrouter_headers(config: dict) -> Dict[str, str]:
    api_key = os.environ.get(config.get("api_key_env") or "OPENROUTER_API_KEY", "").strip()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "conch/1.0",
    }


def _openrouter_body(config: dict, messages: List[dict], tools: Optional[List[dict]]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": config.get("chat_model", config.get("model", "moonshotai/kimi-k3")),
        "messages": messages,
        "max_tokens": 16384,
        # Low reasoning effort and NO temperature: at the default (max)
        # effort with temperature set, Kimi K3 sometimes answers in prose
        # instead of emitting tool_calls; with effort=low it tool-calls
        # reliably. OpenRouter normalizes the value for models with other
        # effort scales (GLM-5.2 verified live with this exact body).
        "reasoning_effort": "low",
    }
    if tools:
        body["tools"] = _sanitize_tools_for_openai(tools)
    return body


def raw_openrouter(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    api_key = os.environ.get(config.get("api_key_env") or "OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    body = _openrouter_body(config, messages, tools)
    req = urllib.request.Request(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers=_openrouter_headers(config),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
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
    # Reasoning arrives in a separate `reasoning` field on OpenRouter, but
    # strip tagged blocks too in case an upstream embeds them in content.
    content = strip_think_blocks((message.get("content") or "").strip())
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": message.get("tool_calls"),
        "_usage": _normalize_usage(data, "openrouter"),
        "_model": body["model"],
    }


def stream_openrouter(config: dict, messages: list, tools=None, on_token=None) -> dict:
    api_key = os.environ.get(config.get("api_key_env") or "OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return {"content": "", "tool_calls": None}
    body = _openrouter_body(config, messages, tools)
    body["stream_options"] = {"include_usage": True}
    reasoning_filter = _ReasoningStreamFilter(on_token) if on_token else None
    result = _stream_openai_compat(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        _openrouter_headers(config),
        body,
        body["model"],
        "openrouter",
        reasoning_filter.feed if reasoning_filter else None,
    )
    if reasoning_filter:
        reasoning_filter.finish()
    if result.get("content"):
        result["content"] = strip_think_blocks(result["content"])
    return result


# ---------------------------------------------------------------------------
# Custom OpenAI-compatible provider (plan 2.4): vLLM, LM Studio, llama.cpp
# server, or a second Ollama box via its OpenAI endpoint. Config:
#   provider=custom
#   custom_base_url=http://host:port/v1   (or base_url when provider=custom)
#   custom_model=<model>                  (also accepts model/chat_model)
#   api_key_env=<ENV VAR>                 (optional)
# Custom endpoints are enumerated and conformance-tested. Only models that
# return a real native forced tool call are selectable.
# ---------------------------------------------------------------------------

CUSTOM_DISCOVERY_TIMEOUT = 3.0
_CUSTOM_CACHE_TTL_OK = 60.0
_CUSTOM_CACHE_TTL_FAIL = 5.0
_custom_models_cache: Dict[str, tuple] = {}
_custom_probe_cache: Dict[tuple, tuple] = {}
_custom_props_cache: Dict[str, tuple] = {}


def clear_local_model_caches() -> None:
    """Clear live discovery/probe caches (used by refresh and tests)."""
    with _local_model_cache_lock:
        for cache in (
            _ollama_tags_cache,
            _ollama_caps_cache,
            _ollama_ctx_cache,
            _ollama_show_cache,
            _ollama_ps_cache,
            _custom_models_cache,
            _custom_probe_cache,
            _custom_props_cache,
        ):
            cache.clear()
    from .llamaidx import clear_llamaidx_cache

    clear_llamaidx_cache()


def get_custom_base_url(config: Optional[dict] = None) -> str:
    config = config or {}
    base = (config.get("custom_base_url") or "").strip()
    if not base and (config.get("provider") or "").lower() == "custom":
        base = (config.get("base_url") or "").strip()
    if not base:
        return ""
    if "://" not in base:
        base = "http://" + base
    return base.rstrip("/")


def _custom_headers(config: dict) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "conch/1.0"}
    key_env = (config.get("api_key_env") or "").strip()
    api_key = os.environ.get(key_env, "").strip() if key_env else ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _custom_root_url(config: Optional[dict] = None) -> str:
    base = get_custom_base_url(config)
    return base[:-3] if base.endswith("/v1") else base


def _custom_model_records(
    config: Optional[dict] = None,
    *,
    timeout: float = CUSTOM_DISCOVERY_TIMEOUT,
    force_refresh: bool = False,
) -> Optional[List[dict]]:
    config = config or {}
    base_url = get_custom_base_url(config)
    if not base_url:
        return None
    if local_endpoint_policy_error("custom", base_url, config):
        return None
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _custom_models_cache.get(base_url)
        if cached is not None and not force_refresh:
            ttl = (
                _CUSTOM_CACHE_TTL_OK
                if cached[1] is not None
                else _CUSTOM_CACHE_TTL_FAIL
            )
            if now - cached[0] < ttl:
                return cached[1]
    req = urllib.request.Request(
        f"{base_url}/models",
        headers=_custom_headers(config),
        method="GET",
    )
    try:
        from .runtime import serialized_agent_execution

        with serialized_agent_execution():
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        data = (
            payload.get("data", payload.get("models", []))
            if isinstance(payload, dict)
            else []
        )
        records = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, str):
                item = {"id": item}
            if not isinstance(item, dict):
                continue
            model_id = str(
                item.get("id") or item.get("model") or item.get("name") or ""
            ).strip()
            if not model_id:
                continue
            identity = hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode()
            ).hexdigest()
            records.append(
                {
                    "id": model_id,
                    "identity": identity,
                    "context_length": item.get("context_length")
                    or item.get("max_model_len"),
                }
            )
    except Exception:
        records = None
    with _local_model_cache_lock:
        _custom_models_cache[base_url] = (now, records)
    return records


def _custom_probe_key(
    config: dict, model: str, identity: str = ""
) -> tuple:
    return (get_custom_base_url(config), model, identity or model)


def probe_custom_model(
    config: Optional[dict],
    model: str,
    *,
    timeout: float = 10.0,
    identity: str = "",
    force_refresh: bool = False,
) -> tuple:
    """Require a valid forced native tool call from one custom model."""
    config = config or {}
    base_url = get_custom_base_url(config)
    if not base_url:
        return False, "custom_base_url is not configured"
    policy_error = local_endpoint_policy_error("custom", base_url, config)
    if policy_error:
        return False, policy_error
    if not model:
        return False, "no model given"
    key = _custom_probe_key(config, model, identity)
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _custom_probe_cache.get(key)
        if cached is not None and not force_refresh:
            ttl = (
                _CUSTOM_CACHE_TTL_OK
                if cached[1][0]
                else _CUSTOM_CACHE_TTL_FAIL
            )
            if now - cached[0] < ttl:
                return cached[1]
    probe_name = "conch_tool_probe"
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Call conch_tool_probe with token conch-ok. "
                    "Do not answer in text."
                ),
            }
        ],
        "temperature": 0,
        "max_tokens": 64,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": probe_name,
                    "description": "Verify native tool-call support.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "token": {
                                "type": "string",
                                "enum": ["conch-ok"],
                            }
                        },
                        "required": ["token"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        # One tool is offered, so the broadly-supported string form is both
        # forced and compatible with llama.cpp builds that reject named
        # tool_choice objects.
        "tool_choice": "required",
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers=_custom_headers(config),
        method="POST",
    )
    try:
        from .runtime import serialized_agent_execution

        with serialized_agent_execution():
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        message = (payload.get("choices") or [{}])[0].get("message", {})
        verified = False
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            if fn.get("name") != probe_name:
                continue
            arguments = fn.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = None
            if isinstance(arguments, dict) and arguments.get("token") == "conch-ok":
                verified = True
                break
        result = (
            (True, "")
            if verified
            else (
                False,
                "model did not return the required native tool call",
            )
        )
    except urllib.error.HTTPError as exc:
        result = (False, format_http_api_error(exc))
    except Exception as exc:
        result = (False, f"endpoint unreachable at {base_url}: {exc}")
    with _local_model_cache_lock:
        _custom_probe_cache[key] = (now, result)
    return result


def list_custom_models(
    config: Optional[dict] = None,
    *,
    timeout: float = CUSTOM_DISCOVERY_TIMEOUT,
    force_refresh: bool = False,
    tool_capable_only: bool = True,
) -> Optional[List[str]]:
    """Enumerate /v1/models, optionally retaining only conformance passes."""
    records = _custom_model_records(
        config, timeout=timeout, force_refresh=force_refresh
    )
    if records is None:
        return None
    if not tool_capable_only:
        return [record["id"] for record in records]
    # Capability probes generate tokens and may load/swap models. Run them
    # serially so one local GPU is never thrashed by discovery.
    verdicts = [
        probe_custom_model(
            config,
            record["id"],
            timeout=max(timeout, 5.0),
            identity=record["identity"],
            force_refresh=force_refresh,
        )[0]
        for record in records
    ]
    return [
        record["id"]
        for record, ok in zip(records, verdicts)
        if ok is True
    ]


def get_custom_server_props(
    config: Optional[dict] = None,
    *,
    timeout: float = CUSTOM_DISCOVERY_TIMEOUT,
    force_refresh: bool = False,
) -> Optional[dict]:
    """Read llama.cpp-compatible properties from /v1/props then /props."""
    config = config or {}
    root = _custom_root_url(config)
    if not root:
        return None
    if local_endpoint_policy_error(
        "custom", get_custom_base_url(config), config
    ):
        return None
    now = time.monotonic()
    with _local_model_cache_lock:
        cached = _custom_props_cache.get(root)
        if cached is not None and not force_refresh:
            ttl = (
                _CUSTOM_CACHE_TTL_OK
                if cached[1] is not None
                else _CUSTOM_CACHE_TTL_FAIL
            )
            if now - cached[0] < ttl:
                return cached[1]
    props = None
    for url in (f"{root}/v1/props", f"{root}/props"):
        req = urllib.request.Request(
            url, headers=_custom_headers(config), method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                candidate = json.loads(response.read().decode())
            if isinstance(candidate, dict):
                props = candidate
                break
        except Exception:
            continue
    with _local_model_cache_lock:
        _custom_props_cache[root] = (now, props)
    return props


def get_custom_context_window(
    config: Optional[dict] = None, model: str = ""
) -> int:
    config = config or {}
    try:
        configured = int(config.get("custom_context_window", 0) or 0)
    except (TypeError, ValueError):
        configured = 0
    props = get_custom_server_props(config) or {}
    settings = props.get("default_generation_settings") or {}
    candidates = [
        settings.get("n_ctx") if isinstance(settings, dict) else None,
        props.get("n_ctx"),
        props.get("context_length"),
    ]
    discovered = next(
        (
            value
            for value in candidates
            if isinstance(value, int) and value > 0
        ),
        0,
    )
    if not discovered and model:
        records = _custom_model_records(config) or []
        record = next(
            (item for item in records if item["id"] == model), None
        )
        value = (record or {}).get("context_length")
        if isinstance(value, int) and value > 0:
            discovered = value
    if configured and discovered:
        return min(configured, discovered)
    return (
        configured
        or discovered
        or PROVIDER_DEFAULT_CONTEXT_WINDOWS["custom"]
    )


def validate_custom_model(
    model: str, config: Optional[dict] = None
) -> tuple:
    policy_error = local_endpoint_policy_error(
        "custom", get_custom_base_url(config), config
    )
    if policy_error:
        return False, policy_error
    records = _custom_model_records(config)
    if records is None:
        return (
            None,
            f"custom endpoint unreachable at {get_custom_base_url(config)}",
        )
    record = next((item for item in records if item["id"] == model), None)
    if record is None:
        return False, f"model '{model}' is not exposed by /v1/models"
    return probe_custom_model(
        config, model, identity=record["identity"]
    )


def _custom_body(config: dict, messages: List[dict], tools: Optional[List[dict]]) -> Dict[str, Any]:
    model = (
        config.get("chat_model")
        or config.get("model")
        or config.get("custom_model", "")
    )
    context_window = get_custom_context_window(config, model)
    try:
        max_tokens = int(
            config.get(
                "custom_max_tokens",
                config.get(
                    "max_output_tokens",
                    min(4096, max(256, context_window // 4)),
                ),
            )
            or 0
        )
    except (TypeError, ValueError):
        max_tokens = min(4096, max(256, context_window // 4))
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": _config_float(
            config,
            "custom_temperature",
            _config_float(config, "temperature", 0.2),
        ),
        "max_tokens": max(1, max_tokens),
    }
    if tools:
        body["tools"] = _sanitize_tools_for_openai(tools)
    if "custom_parallel_tool_calls" in config:
        body["parallel_tool_calls"] = str(
            config["custom_parallel_tool_calls"]
        ).lower() in ("true", "1", "yes", "on")
    body["max_tokens"] = _clamp_output_to_context(
        body["max_tokens"], context_window, messages, tools
    )
    return body


def raw_custom(config: dict, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
    base_url = get_custom_base_url(config)
    if not base_url:
        return error_response("custom provider requires custom_base_url in config")
    policy_error = local_endpoint_policy_error("custom", base_url, config)
    if policy_error:
        return error_response(policy_error)
    body = _custom_body(config, messages, tools)
    ok, reason = validate_custom_model(body["model"], config)
    if ok is not True:
        return error_response(
            reason or f"custom model '{body['model']}' is not verified"
        )
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers=_custom_headers(config),
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=_config_float(config, "custom_timeout", 120.0)
        ) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return error_response(format_http_api_error(exc))
    except Exception as exc:
        return error_response(str(exc))
    message = (data.get("choices") or [{}])[0].get("message", {})
    content = strip_think_blocks((message.get("content") or "").strip())
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": message.get("tool_calls"),
        "_usage": _normalize_usage(data, "openai"),
        "_model": body["model"],
    }


def stream_custom(config: dict, messages: list, tools=None, on_token=None) -> dict:
    base_url = get_custom_base_url(config)
    if not base_url:
        return error_response("custom provider requires custom_base_url in config")
    policy_error = local_endpoint_policy_error("custom", base_url, config)
    if policy_error:
        return error_response(policy_error)
    body = _custom_body(config, messages, tools)
    ok, reason = validate_custom_model(body["model"], config)
    if ok is not True:
        return error_response(
            reason or f"custom model '{body['model']}' is not verified"
        )
    return _stream_openai_compat(
        f"{base_url}/chat/completions",
        _custom_headers(config),
        body,
        body["model"],
        "custom",
        on_token,
        timeout=_config_float(config, "custom_timeout", 120.0),
    )


def probe_custom_provider(config: Optional[dict] = None, *, timeout: float = 10.0) -> tuple:
    """Verify that the configured model is enumerated and natively calls tools."""
    config = config or {}
    base_url = get_custom_base_url(config)
    if not base_url:
        return False, "custom_base_url is not configured"
    model = config.get("chat_model") or config.get("model") or config.get("custom_model", "")
    if not model:
        return False, "custom_model is not configured"
    records = _custom_model_records(
        config, timeout=min(timeout, 5.0), force_refresh=True
    )
    if records is None:
        return False, f"endpoint unreachable at {base_url}"
    record = next((item for item in records if item["id"] == model), None)
    if record is None:
        return False, f"model '{model}' is not exposed by /v1/models"
    return probe_custom_model(
        config,
        model,
        timeout=timeout,
        identity=record["identity"],
        force_refresh=True,
    )


RAW_FNS = {
    "cerebras": raw_cerebras,
    "bedrock": raw_bedrock,
    "openrouter": raw_openrouter,
    "openai": raw_openai,
    "anthropic": raw_anthropic,
    "ollama": raw_ollama,
    "custom": raw_custom,
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
    timeout: float = 120.0,
) -> dict:
    """Shared streaming implementation for OpenAI-compatible APIs."""
    body["stream"] = True
    # Without this, OpenAI-compatible servers (llama.cpp included) send
    # no usage chunk at all and the per-turn token stats line has
    # nothing to show. Servers that predate stream_options ignore
    # unknown request keys.
    body.setdefault("stream_options", {"include_usage": True})
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
        with urllib.request.urlopen(req, timeout=timeout) as response:
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
                    idx = tc.get("index")
                    if not isinstance(idx, int):
                        incoming_id = str(tc.get("id") or "")
                        matching = next(
                            (
                                key
                                for key, value in tool_calls_acc.items()
                                if incoming_id
                                and value.get("id") == incoming_id
                            ),
                            None,
                        )
                        idx = (
                            matching
                            if matching is not None
                            else (
                                0
                                if not incoming_id and len(tool_calls_acc) <= 1
                                else max(tool_calls_acc, default=-1) + 1
                            )
                        )
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.get("id"):
                        tool_calls_acc[idx]["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        incoming_name = str(fn["name"])
                        current_name = tool_calls_acc[idx]["name"]
                        if (
                            not current_name
                            or incoming_name.startswith(current_name)
                        ):
                            tool_calls_acc[idx]["name"] = incoming_name
                        elif incoming_name != current_name:
                            tool_calls_acc[idx]["name"] += incoming_name
                    if fn.get("arguments") is not None:
                        incoming_args = fn["arguments"]
                        if not isinstance(incoming_args, str):
                            incoming_args = json.dumps(incoming_args)
                        current_args = tool_calls_acc[idx]["arguments"]
                        if not current_args:
                            tool_calls_acc[idx]["arguments"] = incoming_args
                        elif incoming_args == current_args:
                            pass
                        elif incoming_args.startswith(current_args):
                            # Some servers send cumulative snapshots rather
                            # than OpenAI-style deltas.
                            tool_calls_acc[idx]["arguments"] = incoming_args
                        else:
                            tool_calls_acc[idx]["arguments"] += incoming_args

                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage["input_tokens"] = u.get("prompt_tokens", 0)
                    usage["output_tokens"] = u.get("completion_tokens", 0)
                if chunk.get("timings"):
                    # llama.cpp sends timings on the final stream chunk:
                    # exact generation seconds for the tok/s display.
                    _attach_server_timings(usage, chunk)
    except urllib.error.HTTPError as exc:
        return error_response(format_http_api_error(exc))
    except Exception as exc:
        return error_response(str(exc))

    full_text = "".join(content_parts).strip()
    # Reasoning-only replies (no content, no tool calls) surface the
    # reasoning as the answer. On tool-call turns the empty content is
    # correct — promoting reasoning there would persist chain-of-thought
    # (OpenRouter models stream reasoning on every tool call).
    if not full_text and reasoning_parts and not tool_calls_acc:
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
    model = config.get("chat_model", config.get("model", "gpt-oss-120b"))
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

    model = config.get("chat_model", config.get("model", "claude-sonnet-5"))
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": _anthropic_max_tokens(model),
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
                    elif delta.get("type") == "thinking_delta":
                        cur_text.append(delta.get("thinking", ""))
                    elif delta.get("type") == "signature_delta":
                        cur_block_meta["signature"] = delta.get("signature", "")

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
                    elif cur_block_type == "thinking":
                        block = {
                            "type": "thinking",
                            "thinking": "".join(cur_text),
                        }
                        sig = cur_block_meta.get("signature")
                        if sig:
                            block["signature"] = sig
                        anthropic_content.append(block)
                    elif cur_block_type == "redacted_thinking":
                        anthropic_content.append({
                            "type": "redacted_thinking",
                            "data": cur_block_meta.get("data", ""),
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


# Content prefixes that signal a *textual* tool call rather than a reply:
# bare JSON (qwen2.5-coder emits its calls this way on every single tool
# use — measured 24/24 in live batches), <tool_call> tags, Claude XML, and
# fenced json blocks.
_TEXTUAL_TOOL_MARKERS = ("{", "[", "<tool_call", "<function_calls", "```")


class _StreamDisplayGate:
    """Withhold streamed content from the terminal while it might be a
    textual tool call.

    Without this, malformed JSON/XML tool syntax is printed live before
    chat_turn can reject it. Withheld content is never lost: ordinary replies
    are returned and the app prints them after the stream ends.
    """

    def __init__(self, on_token):
        self._on_token = on_token
        self._buffer = ""
        self._verdict: Optional[bool] = None  # True=suppress, False=forward

    def feed(self, text: str):
        if self._verdict is False:
            self._on_token(text)
            return
        if self._verdict is True:
            return
        self._buffer += text
        head = self._buffer.lstrip()
        if not head:
            return
        for marker in _TEXTUAL_TOOL_MARKERS:
            if head.startswith(marker):
                self._verdict = True
                return
        # Still a prefix of a marker (e.g. "<tool_ca", "``")? Keep buffering.
        if any(marker.startswith(head) for marker in _TEXTUAL_TOOL_MARKERS
               if len(head) < len(marker)):
            return
        self._verdict = False
        self._on_token(self._buffer)

    def finish(self):
        """Stream ended. If we're still buffering, the content only ever
        matched a *prefix* of a marker (e.g. a lone "<" or "``") and never
        resolved into a real textual tool call — so it's an ordinary short
        reply. Flush it to the terminal so nothing is lost. (Genuine tool
        calls reach a full marker and are suppressed with verdict=True; a
        declined reply that begins with a full marker stays suppressed and is
        reprinted by app.py's printed-vs-streamed fallback.)
        """
        if self._verdict is None and self._buffer:
            self._verdict = False
            self._on_token(self._buffer)


def stream_ollama(
    config: dict, messages: list, tools=None, on_token=None
) -> dict:
    base_url = get_ollama_base_url(config)
    model = config.get("chat_model", config.get("model", "llama3.3"))
    ok, reason = validate_ollama_model(model, config)
    if ok is not True:
        return error_response(reason or f"Ollama model '{model}' is not verified")
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    apply_ollama_request_options(body, config, model)
    if tools:
        body["tools"] = _sanitize_tools_for_openai(tools)
    if body.get("options", {}).get("num_predict"):
        body["options"]["num_predict"] = _clamp_output_to_context(
            body["options"]["num_predict"],
            get_ollama_effective_context(model, config),
            messages,
            tools,
        )

    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    content_parts: list[str] = []
    raw_tool_calls: list = []
    seen_tool_calls: set[str] = set()
    final_data: dict = {}
    # Route display through the gate so malformed textual JSON/XML calls are
    # withheld from the terminal. The full content is returned to chat_turn,
    # which rejects textual calls without executing them. The gate affects
    # display only.
    gate = _StreamDisplayGate(on_token) if on_token else None

    try:
        with urllib.request.urlopen(
            req, timeout=_config_float(config, "ollama_timeout", 120.0)
        ) as response:
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
                for tool_call in msg.get("tool_calls") or []:
                    fingerprint = json.dumps(
                        tool_call, sort_keys=True, default=str
                    )
                    if fingerprint not in seen_tool_calls:
                        seen_tool_calls.add(fingerprint)
                        raw_tool_calls.append(tool_call)
                # Skip msg.get("thinking") tokens (qwen3 et al.) — reasoning is
                # not part of the reply.
                if msg.get("content"):
                    content_parts.append(msg["content"])
                    if gate:
                        gate.feed(msg["content"])

                if data.get("done"):
                    final_data = data
                    break
    except Exception as exc:
        return error_response(str(exc))

    if gate:
        gate.finish()

    full_text = strip_think_blocks("".join(content_parts).strip())
    with _local_model_cache_lock:
        _ollama_ps_cache.pop(base_url, None)

    return {
        "role": "assistant",
        "content": full_text,
        "tool_calls": _convert_ollama_tool_calls(raw_tool_calls),
        "_usage": _normalize_usage(final_data, "ollama"),
        "_model": model,
    }


STREAM_FNS = {
    "cerebras": stream_cerebras,
    "bedrock": stream_bedrock,
    "openrouter": stream_openrouter,
    "openai": stream_openai,
    "anthropic": stream_anthropic,
    "ollama": stream_ollama,
    "custom": stream_custom,
}

