"""Every model conch can run, as one list: the built-in cloud catalogs,
subscription seats, models the user added (config ``extra_models``), and
what a local server or the llama-idx registry reports.

The model picker (/models) renders it; added models also pass catalog
validation, so ``/model`` accepts them. Switching still runs the usual live
check, so adding a model never bypasses verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

EXTRA_MODELS_KEY = "extra_models"
#: Discovered from their server; nothing to add by hand.
DISCOVERED_PROVIDERS = ("ollama", "custom")
REGISTRY = "llamaidx"
PROVIDER_ORDER = ("claude-code", "codex", "anthropic", "openai", "openrouter",
                  "bedrock", "cerebras", "ollama", "custom", REGISTRY)
_MODEL_ID = re.compile(r"^[A-Za-z0-9][\w.:/@+\[\]-]*$")


@dataclass
class ModelEntry:
    provider: str
    model: str
    source: str = "catalog"  # catalog | added | server | registry
    context: Optional[int] = None
    price: Optional[Tuple[float, float]] = None  # USD per 1M tokens, in/out
    billing: str = "unknown"  # api | seat | local | unknown
    tier: str = "standard"
    note: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return self.provider, self.model

    @property
    def label(self) -> str:
        return self.model if self.provider == REGISTRY else f"{self.provider}/{self.model}"

    def switch_command(self) -> str:
        """The /model command that makes this the chat model, provider exact."""
        if self.provider == REGISTRY:
            return f"/model {self.model}"
        return f"/model {self.model} --provider {self.provider}"


@dataclass
class ProviderStatus:
    available: Optional[bool]  # None while checking
    note: str = ""


def added_models(config: Optional[dict]) -> List[Tuple[str, str]]:
    """``extra_models = provider/model, provider/model`` as (provider, model)."""
    found: List[Tuple[str, str]] = []
    for item in str((config or {}).get(EXTRA_MODELS_KEY, "") or "").split(","):
        provider, slash, model = item.strip().partition("/")
        if slash and provider and model and (provider.lower(), model) not in found:
            found.append((provider.lower(), model))
    return found


def added_for(config: Optional[dict], provider: str) -> List[str]:
    return [model for name, model in added_models(config) if name == provider]


def parse_new_model(text: str) -> Tuple[str, str]:
    """``provider/model-id`` typed into the picker → (provider, model).
    The first segment must name a provider, so OpenRouter ids keep their own
    slash (``openrouter/vendor/model``). Raises ValueError with the reason."""
    from .providers import RAW_FNS

    provider, slash, model = text.strip().partition("/")
    provider, model = provider.strip().lower(), model.strip()
    if not slash or not provider or not model:
        raise ValueError("Type provider/model-id, for example openai/gpt-6-sol.")
    if provider in DISCOVERED_PROVIDERS:
        raise ValueError(f"{provider} models are listed from the server; nothing to add.")
    if provider not in RAW_FNS:
        raise ValueError(f"Unknown provider '{provider}'.")
    if not _MODEL_ID.match(model):
        raise ValueError(f"'{model}' is not a model id.")
    return provider, model


def _save_added(config: dict, models: List[Tuple[str, str]]) -> str:
    from .config import set_config_values

    value = ", ".join(f"{provider}/{model}" for provider, model in models)
    config[EXTRA_MODELS_KEY] = value
    return set_config_values({EXTRA_MODELS_KEY: value})


def add_model(config: dict, provider: str, model: str) -> bool:
    """Record a model the catalog lacks; False when it is already listed."""
    from .providers import KNOWN_MODELS

    if model in KNOWN_MODELS.get(provider, ()) or (provider, model) in added_models(config):
        return False
    _save_added(config, added_models(config) + [(provider, model)])
    return True


def remove_model(config: dict, provider: str, model: str) -> bool:
    """Forget an added model; False when it was not one."""
    models = added_models(config)
    if (provider, model) not in models:
        return False
    _save_added(config, [item for item in models if item != (provider, model)])
    return True


def provider_for(model: str, config: Optional[dict]) -> Optional[str]:
    """The provider whose catalog (built-in or added) lists *model*."""
    from .providers import KNOWN_MODELS

    for provider in PROVIDER_ORDER:
        if provider not in DISCOVERED_PROVIDERS and (
            model in KNOWN_MODELS.get(provider, ()) or model in added_for(config, provider)
        ):
            return provider
    return None


def make_entry(provider: str, model: str, config: dict, source: str) -> ModelEntry:
    from .fanout import model_tier
    from .providers import MODEL_PRICING, _billing_kind, get_context_window

    billing = _billing_kind(provider, model, config)
    context = None
    if provider not in DISCOVERED_PROVIDERS:
        try:
            context = get_context_window(provider, model, config)
        except Exception:
            context = None
    price = MODEL_PRICING.get(model) if billing == "api" else None
    return ModelEntry(provider, model, source, context, price, billing,
                      model_tier(provider, model))


def catalog_entries(config: dict) -> List[ModelEntry]:
    """Built-in and added models: instant, no network."""
    from .providers import KNOWN_MODELS

    entries: List[ModelEntry] = []
    for provider in PROVIDER_ORDER:
        if provider in DISCOVERED_PROVIDERS or provider == REGISTRY:
            continue
        for model in KNOWN_MODELS.get(provider, ()):
            entries.append(make_entry(provider, model, config, "catalog"))
        for model in added_for(config, provider):
            if model not in KNOWN_MODELS.get(provider, ()):
                entries.append(make_entry(provider, model, config, "added"))
    return entries


def static_status(config: dict) -> Dict[str, ProviderStatus]:
    """What can be said about each provider without touching the network."""
    from .config import local_only_enabled
    from .llamaidx import get_llamaidx_url
    from .providers import DEFAULT_API_KEY_ENVS, SEAT_PROVIDERS, _has_key, get_custom_base_url

    local_only = local_only_enabled(config, config.get("provider", ""))
    statuses: Dict[str, ProviderStatus] = {}
    for provider in PROVIDER_ORDER:
        if provider in SEAT_PROVIDERS:
            statuses[provider] = ProviderStatus(None, "checking sign-in…")
        elif provider == "ollama":
            statuses[provider] = ProviderStatus(None, "asking the server…")
        elif provider == "custom":
            configured = bool(get_custom_base_url(config))
            statuses[provider] = (ProviderStatus(None, "asking the endpoint…") if configured
                                  else ProviderStatus(False, "not configured (custom_base_url)"))
        elif provider == REGISTRY:
            statuses[provider] = (ProviderStatus(None, "asking the registry…")
                                  if get_llamaidx_url(config)
                                  else ProviderStatus(False, "no registry (llamaidx_url)"))
        elif _has_key(provider):
            statuses[provider] = ProviderStatus(True, "key set")
        else:
            statuses[provider] = ProviderStatus(
                False, f"{DEFAULT_API_KEY_ENVS.get(provider, 'key')} not set")
        if local_only and provider not in (*DISCOVERED_PROVIDERS, REGISTRY):
            statuses[provider] = ProviderStatus(False, "local_only is on")
    return statuses


LiveResult = Tuple[Tuple[str, ...], List[ModelEntry], Dict[str, ProviderStatus]]


def _seat_source(provider: str):
    """A seat's sign-in state. Its models come from the catalog, so it
    replaces no rows (covers no provider)."""
    def run(config: dict) -> LiveResult:
        from . import auth, seats

        try:
            if not auth.cli_installed(provider):
                return (), [], {provider: ProviderStatus(False, "CLI not installed")}
            ok, _detail = seats.status(provider, timeout=8)
            note = "signed in" if ok else f"not signed in: conch auth login {provider}"
            return (), [], {provider: ProviderStatus(bool(ok), note)}
        except Exception as exc:
            return (), [], {provider: ProviderStatus(False, str(exc)[:80])}
    return run


def _ollama_source(config: dict) -> LiveResult:
    from .providers import get_ollama_base_url, list_ollama_models

    try:
        models = list_ollama_models(config, force_refresh=True)
    except Exception:
        models = None
    url = get_ollama_base_url(config)
    if models is None:
        return ("ollama",), [], {"ollama": ProviderStatus(False, f"unreachable at {url}")}
    status = ProviderStatus(bool(models), f"{len(models)} tool-capable at {url}")
    return ("ollama",), [make_entry("ollama", model, config, "server") for model in models], \
        {"ollama": status}


def _custom_source(config: dict) -> LiveResult:
    from .providers import get_custom_base_url, list_custom_models

    try:
        models = list_custom_models(config, force_refresh=True)
    except Exception:
        models = None
    if models is None:
        return ("custom",), [], {"custom": ProviderStatus(False, "endpoint unreachable")}
    status = ProviderStatus(bool(models), get_custom_base_url(config))
    return ("custom",), [make_entry("custom", model, config, "server") for model in models], \
        {"custom": status}


def _registry_source(config: dict) -> LiveResult:
    from .llamaidx import get_llamaidx_url, list_llamaidx_models

    try:
        registry = list_llamaidx_models(config, force_refresh=True)
    except Exception:
        registry = None
    if registry is None:
        return (REGISTRY,), [], {REGISTRY: ProviderStatus(False, "registry unreachable")}
    entries = [ModelEntry(REGISTRY, item["name"], "registry", item.get("ctx"), None, "local",
                          "standard", "degraded" if item.get("degraded") else "")
               for item in registry]
    return (REGISTRY,), entries, {REGISTRY: ProviderStatus(bool(registry), get_llamaidx_url(config))}


def live_sources(config: dict) -> list:
    """Callables (config → LiveResult) for what needs the network or a CLI:
    seat sign-in, the Ollama server, the custom endpoint, the registry. Run
    them independently so a slow one never holds up the rest; each never
    raises, a failure becomes its status."""
    from .config import local_only_enabled
    from .llamaidx import get_llamaidx_url
    from .providers import SEAT_PROVIDERS, get_custom_base_url

    sources: list = []
    if not local_only_enabled(config, config.get("provider", "")):
        sources += [_seat_source(provider) for provider in SEAT_PROVIDERS]
    sources.append(_ollama_source)
    if get_custom_base_url(config):
        sources.append(_custom_source)
    if get_llamaidx_url(config):
        sources.append(_registry_source)
    return sources
