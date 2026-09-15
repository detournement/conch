"""llama-idx registry discovery: one endpoint for the self-hosted fleet.

With ``llamaidx_url`` configured, conch reads the registry's
``GET /v1/inference?tools=true`` catalog and surfaces every up/degraded
provider's tool-verified models as namespaced entries
``llamaidx/{provider}/{display_id}``. Selecting one routes through the
EXISTING adapters by flavor — the ollama adapter with ``ollama_base_url``
pointed at the provider, or the custom adapter with ``custom_base_url`` —
so the registry only ever supplies flavor + base_url + model id; no new
inference code. Unset ``llamaidx_url`` = feature off, zero new traffic.

Trust model, belt and braces: the registry's tools verdict gates
*listing*; conch's own probe-on-select (validate_custom_model /
ollama_model_supports_tools) still runs at *selection*, because the
registry answer can be stale and the tools-only invariant belongs to the
component that talks to the model. Down providers never appear (the
registry's default view already drops them; conch re-drops any that leak
through). Degraded providers stay listed with a marker.

``local_only`` still applies: the registry URL itself and every
discovered provider base_url must pass is_local_inference_url, or they
are excluded — the registry does not get to override the policy.

The optional ``llamaidx_token_env`` names the environment variable
holding the registry's read token (registries running read_auth=true);
the token itself is never configured or logged.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

LLAMAIDX_DISCOVERY_TIMEOUT = 3.0
_LLAMAIDX_TTL_OK = 30.0
_LLAMAIDX_TTL_FAIL = 5.0

NAMESPACE_PREFIX = "llamaidx/"

_llamaidx_cache: Dict[str, tuple] = {}  # url -> (fetched_at, providers-or-None)
_llamaidx_cache_lock = threading.RLock()


def clear_llamaidx_cache() -> None:
    with _llamaidx_cache_lock:
        _llamaidx_cache.clear()


def get_llamaidx_url(config: Optional[dict] = None) -> str:
    base = ((config or {}).get("llamaidx_url") or "").strip()
    if not base:
        return ""
    if "://" not in base:
        base = "http://" + base
    return base.rstrip("/")


def _llamaidx_headers(config: dict) -> Dict[str, str]:
    headers = {"User-Agent": "conch/1.0"}
    token_env = (config.get("llamaidx_token_env") or "").strip()
    token = os.environ.get(token_env, "").strip() if token_env else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _registry_blocked_by_policy(config: dict, url: str) -> bool:
    from .config import local_only_enabled
    from .providers import is_local_inference_url

    return local_only_enabled(
        config, config.get("provider", "")
    ) and not is_local_inference_url(url)


def fetch_llamaidx_catalog(
    config: Optional[dict] = None,
    *,
    timeout: float = LLAMAIDX_DISCOVERY_TIMEOUT,
    force_refresh: bool = False,
) -> Optional[List[dict]]:
    """Provider records from the registry, or None when off/unreachable.

    The registry pre-filters to up+degraded providers and tool-verified
    models (?tools=true). Conch re-applies both rules anyway (fail
    closed against a misbehaving registry) and drops providers whose
    base_url fails the local_only predicate when that policy is active.
    """
    config = config or {}
    url = get_llamaidx_url(config)
    if not url:
        return None
    if _registry_blocked_by_policy(config, url):
        return None
    now = time.monotonic()
    with _llamaidx_cache_lock:
        cached = _llamaidx_cache.get(url)
        if cached is not None and not force_refresh:
            fetched_at, providers = cached
            ttl = _LLAMAIDX_TTL_OK if providers is not None else _LLAMAIDX_TTL_FAIL
            if now - fetched_at < ttl:
                return providers
    request = urllib.request.Request(
        f"{url}/v1/inference?tools=true",
        headers=_llamaidx_headers(config),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        providers = _parse_catalog(config, payload)
    except Exception:
        providers = None
    with _llamaidx_cache_lock:
        _llamaidx_cache[url] = (now, providers)
    return providers


def _parse_catalog(config: dict, payload: dict) -> List[dict]:
    from .config import local_only_enabled
    from .providers import is_local_inference_url

    local_only = local_only_enabled(config, config.get("provider", ""))
    providers = []
    for provider in (payload or {}).get("providers", []) or []:
        if not isinstance(provider, dict):
            continue
        status = str(provider.get("status") or "").lower()
        if status not in ("up", "degraded"):
            continue  # down providers vanish, whatever the registry sent
        base_url = str(provider.get("base_url") or "").strip().rstrip("/")
        name = str(provider.get("name") or "").strip()
        flavor = str(provider.get("flavor") or "").lower()
        if not base_url or not name or flavor not in ("llamacpp", "ollama", "openai"):
            continue
        if local_only and not is_local_inference_url(base_url):
            continue
        models = []
        for model in provider.get("models", []) or []:
            if not isinstance(model, dict):
                continue
            if model.get("tools") is not True:
                continue  # the registry verdict gates listing, fail closed
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            models.append(
                {
                    "model_id": model_id,
                    "display_id": str(model.get("display_id") or model_id),
                    "ctx": model.get("ctx"),
                    "quant": model.get("quant"),
                    "loaded": model.get("loaded"),
                }
            )
        providers.append(
            {
                "name": name,
                "flavor": flavor,
                "base_url": base_url,
                "status": status,
                "auth_required": bool(provider.get("auth_required")),
                "api_key_env": provider.get("api_key_env"),
                "models": models,
            }
        )
    return providers


def list_llamaidx_models(
    config: Optional[dict] = None, *, force_refresh: bool = False
) -> Optional[List[dict]]:
    """Namespaced catalog entries, or None when the registry is
    unconfigured/unreachable. Each entry:
    name (llamaidx/provider/display_id), provider_name, flavor, base_url,
    model_id (verbatim), ctx, degraded, auth_required, api_key_env."""
    providers = fetch_llamaidx_catalog(config, force_refresh=force_refresh)
    if providers is None:
        return None
    entries = []
    for provider in providers:
        for model in provider["models"]:
            entries.append(
                {
                    "name": (
                        f"{NAMESPACE_PREFIX}{provider['name']}/"
                        f"{model['display_id']}"
                    ),
                    "provider_name": provider["name"],
                    "flavor": provider["flavor"],
                    "base_url": provider["base_url"],
                    "model_id": model["model_id"],
                    "display_id": model["display_id"],
                    "ctx": model.get("ctx"),
                    "loaded": model.get("loaded"),
                    "degraded": provider["status"] == "degraded",
                    "auth_required": provider["auth_required"],
                    "api_key_env": provider.get("api_key_env"),
                }
            )
    return entries


def resolve_llamaidx_model(
    name: str, config: Optional[dict] = None, *, force_refresh: bool = False
) -> Optional[dict]:
    """Resolve a namespaced ``llamaidx/provider/model`` entry, matching the
    display id or the verbatim model id after the provider segment."""
    if not name.startswith(NAMESPACE_PREFIX):
        return None
    remainder = name[len(NAMESPACE_PREFIX):]
    if "/" not in remainder:
        return None
    provider_name, model_ref = remainder.split("/", 1)
    entries = list_llamaidx_models(config, force_refresh=force_refresh)
    for entry in entries or []:
        if entry["provider_name"] != provider_name:
            continue
        if model_ref in (entry["display_id"], entry["model_id"]):
            return entry
    return None


def llamaidx_selection_overrides(entry: dict) -> dict:
    """Config mutations that route the entry through the existing
    adapters: ollama flavor -> ollama adapter, llamacpp/openai -> custom
    adapter (llama.cpp serves the OpenAI surface conch's custom adapter
    already speaks)."""
    api_key_env = (
        str(entry.get("api_key_env") or "").strip()
        if entry.get("auth_required")
        else ""
    )
    if entry["flavor"] == "ollama":
        return {
            "provider": "ollama",
            "ollama_base_url": entry["base_url"],
            "model": entry["model_id"],
            "chat_model": entry["model_id"],
            "api_key_env": "",
        }
    return {
        "provider": "custom",
        "custom_base_url": entry["base_url"] + "/v1",
        "custom_model": entry["model_id"],
        "model": entry["model_id"],
        "chat_model": entry["model_id"],
        "api_key_env": api_key_env,
    }


def llamaidx_fallback_candidates(
    config: Optional[dict],
    current_provider: str,
    current_model: str,
) -> list:
    """Registry tier for get_fallback_chain: up, tool-verified models as
    (mapped_provider, namespaced_name, needs_ctx_switch) — same-flavor
    first, then largest context. Skips the current session's own
    provider box (its models are already same-provider candidates) and
    degraded providers (a loading box is a poor rescue)."""
    config = config or {}
    entries = list_llamaidx_models(config)
    if not entries:
        return []
    from .providers import get_custom_base_url, get_ollama_base_url

    current_bases = set()
    if current_provider == "ollama":
        current_bases.add(get_ollama_base_url(config).rstrip("/"))
    elif current_provider == "custom":
        base = get_custom_base_url(config).rstrip("/")
        current_bases.add(base)
        if base.endswith("/v1"):
            current_bases.add(base[: -len("/v1")])

    def mapped_provider(entry) -> str:
        return "ollama" if entry["flavor"] == "ollama" else "custom"

    candidates = []
    for entry in entries:
        if entry["degraded"]:
            continue
        if entry["base_url"].rstrip("/") in current_bases:
            continue  # step-1 (same-provider list) already covers this box
        candidates.append(entry)

    same_flavor_rank = {
        "ollama": 0 if current_provider == "ollama" else 1,
        "custom": 0 if current_provider == "custom" else 1,
    }
    candidates.sort(
        key=lambda entry: (
            same_flavor_rank[mapped_provider(entry)],
            -(entry.get("ctx") or 0),
            entry["name"],
        )
    )
    return [
        (
            mapped_provider(entry),
            entry["name"],
            mapped_provider(entry) != current_provider,
        )
        for entry in candidates
    ]
