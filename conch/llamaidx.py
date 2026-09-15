"""llama-idx registry: one endpoint for the self-hosted fleet.

With ``llamaidx_url`` configured, conch reads the registry's
``GET /v1/inference?tools=true`` catalog and surfaces every up/degraded
provider's tool-verified models as namespaced entries
``llamaidx/{provider}/{display_id}``. Selecting one routes through the
EXISTING adapters by flavor — the ollama adapter with ``ollama_base_url``
pointed at the provider, or the custom adapter with ``custom_base_url`` —
so the registry only ever supplies flavor + base_url + model id; no new
inference code. Unset ``llamaidx_url`` = feature off, zero new traffic.

The registry is also a *data source*, not just discovery plumbing: the
``?status=all`` view (fetch_llamaidx_status) carries the whole fleet —
down boxes with their last error, degraded boxes, per-model context/
quant/loaded/modalities — and feeds the ``llamaidx_registry`` builtin
tool and the ``/llamaidx`` command. Reporting and routing stay separate:
selection/fallback only ever consume the tool-verified catalog view.
Both views fail closed on unsupported ``registry_version`` majors.

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

# The registry schema this client was written against. Anything else is
# treated exactly like an unreachable registry (fail closed): a major bump
# means the payload shape may have changed under us.
_SUPPORTED_SCHEMA_MAJOR = "0"

NAMESPACE_PREFIX = "llamaidx/"

# Bounds for the model/user-facing fleet rendering: the registry is a
# model-facing data source, so its output must stay bounded no matter how
# many boxes and models the fleet grows.
_RENDER_MAX_PROVIDERS = 24
_RENDER_MAX_MODELS = 12
_RENDER_MAX_CHARS = 6000

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
    return _fetch_registry_view(
        config or {},
        query="tools=true",
        parse=_parse_catalog,
        timeout=timeout,
        force_refresh=force_refresh,
    )


def _schema_supported(payload) -> bool:
    """Fail closed on unknown registry schema versions."""
    version = (payload or {}).get("registry_version")
    if not isinstance(version, str) or not version.strip():
        return False
    return version.strip().split(".", 1)[0] == _SUPPORTED_SCHEMA_MAJOR


def _fetch_registry_view(
    config: dict,
    *,
    query: str,
    parse,
    timeout: float,
    force_refresh: bool,
):
    """One cached GET /v1/inference?{query}, parsed with *parse*.

    Returns None when the feature is off, the policy blocks the registry
    URL, the registry is unreachable, or the payload's schema version is
    unsupported — callers cannot tell these apart on purpose: all of them
    mean "no trustworthy registry data".
    """
    url = get_llamaidx_url(config)
    if not url:
        return None
    if _registry_blocked_by_policy(config, url):
        return None
    cache_key = f"{url}?{query}"
    now = time.monotonic()
    with _llamaidx_cache_lock:
        cached = _llamaidx_cache.get(cache_key)
        if cached is not None and not force_refresh:
            fetched_at, value = cached
            ttl = _LLAMAIDX_TTL_OK if value is not None else _LLAMAIDX_TTL_FAIL
            if now - fetched_at < ttl:
                return value
    request = urllib.request.Request(
        f"{url}/v1/inference?{query}",
        headers=_llamaidx_headers(config),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        value = parse(config, payload) if _schema_supported(payload) else None
    except Exception:
        value = None
    with _llamaidx_cache_lock:
        _llamaidx_cache[cache_key] = (now, value)
    return value


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


def probe_llamaidx_registry(
    url: str,
    config: Optional[dict] = None,
    *,
    timeout: float = LLAMAIDX_DISCOVERY_TIMEOUT,
) -> tuple:
    """Validate *url* as a llama-idx registry before committing it.

    One uncached ``GET {url}/v1/inference?status=all`` that distinguishes,
    for the human or agent wiring a registry, exactly what the fetch_*
    helpers deliberately blur: policy-blocked, unreachable, non-JSON, and
    unsupported schema majors all get their own reason. Returns
    ``(ok, reason, status)`` where *status* is the parsed
    fetch_llamaidx_status shape on success and None otherwise.
    """
    config = dict(config or {})
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    if _registry_blocked_by_policy(config, url):
        return (
            False,
            "local_only is enabled and the registry URL is not a"
            " local/tailnet address",
            None,
        )
    request = urllib.request.Request(
        f"{url}/v1/inference?status=all",
        headers=_llamaidx_headers(config),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except json.JSONDecodeError:
        return (
            False,
            "endpoint did not return JSON (not a llama-idx registry?)",
            None,
        )
    except Exception as exc:
        return False, f"unreachable: {exc}", None
    if not isinstance(payload, dict) or not _schema_supported(payload):
        version = (
            payload.get("registry_version") if isinstance(payload, dict) else None
        )
        return (
            False,
            f"unsupported registry_version {version!r} (this conch speaks"
            f" major {_SUPPORTED_SCHEMA_MAJOR}.x)",
            None,
        )
    return True, "", _parse_status(config, payload)


def registry_probe_summary(status: dict) -> str:
    """One line of provider/model counts from a probe_llamaidx_registry
    result, shared by /registry and the conch_config registry actions."""
    providers = status.get("providers") or []
    counts = {"up": 0, "degraded": 0, "down": 0}
    for provider in providers:
        counts[provider["status"]] += 1
    selectable = sum(
        1
        for provider in providers
        if provider["status"] in ("up", "degraded")
        for model in provider["models"]
        if model.get("tools") is True
    )
    version = status.get("registry_version") or "unknown"
    return (
        f"registry_version {version}; {len(providers)} provider(s) —"
        f" {counts['up']} up, {counts['degraded']} degraded,"
        f" {counts['down']} down; {selectable} tool-verified model(s)"
        " selectable"
    )


def fetch_llamaidx_status(
    config: Optional[dict] = None,
    *,
    timeout: float = LLAMAIDX_DISCOVERY_TIMEOUT,
    force_refresh: bool = False,
) -> Optional[dict]:
    """The full fleet view (``?status=all``): every provider the registry
    knows, including down boxes with their last error — the reporting
    surface behind the ``llamaidx_registry`` tool and ``/llamaidx``.

    This is a *data* view, never a routing view: selection and fallback
    keep going through the catalog (tool-verified, up/degraded only).
    Reads serve the registry's stored state and never touch the boxes.
    Returns ``{"generated_at", "registry_version", "providers": [...]}``
    or None when off/unreachable/unsupported-schema.
    """
    return _fetch_registry_view(
        config or {},
        query="status=all",
        parse=_parse_status,
        timeout=timeout,
        force_refresh=force_refresh,
    )


def _parse_status(config: dict, payload: dict) -> dict:
    """Fail-closed parse of the all-statuses view.

    Unknown provider states and flavors are dropped rather than guessed
    at; the same local_only predicate that gates discovery gates
    reporting, so a policy-excluded box never appears anywhere.
    """
    from .config import local_only_enabled
    from .providers import is_local_inference_url

    local_only = local_only_enabled(config, config.get("provider", ""))
    providers = []
    for provider in (payload or {}).get("providers", []) or []:
        if not isinstance(provider, dict):
            continue
        status = str(provider.get("status") or "").lower()
        if status not in ("up", "degraded", "down"):
            continue
        base_url = str(provider.get("base_url") or "").strip().rstrip("/")
        name = str(provider.get("name") or "").strip()
        flavor = str(provider.get("flavor") or "").lower()
        if not base_url or not name or flavor not in ("llamacpp", "ollama", "openai"):
            continue
        if local_only and not is_local_inference_url(base_url):
            continue
        labels = {}
        for key, value in (provider.get("labels") or {}).items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
                continue
            labels[key] = value
            if len(labels) >= 8:
                break
        models = []
        for model in provider.get("models", []) or []:
            if not isinstance(model, dict):
                continue
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
                    "tools": model.get("tools"),
                    "modalities": sorted(
                        key
                        for key, enabled in (model.get("modalities") or {}).items()
                        if enabled is True and isinstance(key, str)
                    ),
                }
            )
        providers.append(
            {
                "name": name,
                "flavor": flavor,
                "base_url": base_url,
                "status": status,
                "last_seen": str(provider.get("last_seen") or ""),
                "last_error": str(provider.get("last_error") or "") or None,
                "server_version": str(provider.get("server_version") or ""),
                "labels": labels,
                "auth_required": bool(provider.get("auth_required")),
                "api_key_env": str(provider.get("api_key_env") or "") or None,
                "models": models,
            }
        )
    return {
        "generated_at": str((payload or {}).get("generated_at") or ""),
        "registry_version": str((payload or {}).get("registry_version") or ""),
        "providers": providers,
    }


def render_fleet_status(status: dict, *, color: bool = False) -> str:
    """Bounded text rendering of a fetch_llamaidx_status result, shared by
    the llamaidx_registry tool (plain) and /llamaidx (color). Secrets never
    appear: auth is reported as the NAME of the provider's api_key_env."""

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    providers = status.get("providers") or []
    counts = {"up": 0, "degraded": 0, "down": 0}
    for provider in providers:
        counts[provider["status"]] += 1
    generated = status.get("generated_at") or ""
    header = (
        f"llama-idx fleet: {len(providers)} provider(s) — "
        f"{counts['up']} up, {counts['degraded']} degraded, {counts['down']} down"
    )
    if generated:
        header += f" (registry snapshot {generated})"
    lines = [header]
    status_paint = {"up": "1;32", "degraded": "33", "down": "31"}
    for provider in providers[:_RENDER_MAX_PROVIDERS]:
        glyph = "●" if provider["status"] in ("up", "degraded") else "○"
        marker = paint(f"{glyph} [{provider['status']}]", status_paint[provider["status"]])
        labels = ""
        if provider["labels"]:
            pairs = ", ".join(
                f"{key}={provider['labels'][key]}" for key in sorted(provider["labels"])
            )
            labels = f"  ({pairs})"
        lines.append(
            f"  {marker} {provider['name']}  {provider['flavor']}"
            f"  {provider['base_url']}{labels}"
        )
        detail = []
        if provider["last_seen"]:
            detail.append(f"last seen {provider['last_seen']}")
        if provider["last_error"]:
            detail.append(f"last error: {provider['last_error']}")
        if provider["auth_required"]:
            env_name = provider["api_key_env"] or "unspecified env var"
            detail.append(f"auth required (key in ${env_name})")
        if detail:
            lines.append(paint(f"      {'; '.join(detail)}", "2"))
        models = provider["models"]
        for model in models[:_RENDER_MAX_MODELS]:
            bits = []
            if model.get("ctx"):
                bits.append(f"ctx={model['ctx']}")
            if model.get("quant"):
                bits.append(f"quant={model['quant']}")
            bits.append("loaded" if model.get("loaded") else "not loaded")
            if model.get("tools") is True:
                bits.append("tools verified")
            elif model.get("tools") is False:
                bits.append("no tool support")
            else:
                bits.append("tools unprobed")
            if model.get("modalities"):
                bits.append("+".join(model["modalities"]))
            lines.append(f"      - {model['display_id']}  ({', '.join(bits)})")
        if len(models) > _RENDER_MAX_MODELS:
            lines.append(f"      (+{len(models) - _RENDER_MAX_MODELS} more models)")
    if len(providers) > _RENDER_MAX_PROVIDERS:
        lines.append(f"  (+{len(providers) - _RENDER_MAX_PROVIDERS} more providers)")
    text = "\n".join(lines)
    if len(text) > _RENDER_MAX_CHARS:
        text = text[:_RENDER_MAX_CHARS].rsplit("\n", 1)[0] + "\n  (truncated)"
    return text


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
