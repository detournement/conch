"""Per-model reasoning effort and context window, where the provider has them.

The model picker sets them (Ctrl+E effort, Ctrl+W context window) and the
request builders read the active model's choice, so every model keeps its
own. Values live in two config keys as ``provider/model=value`` lists:

    model_effort = claude-code/opus=high, ollama/qwen3:32b=on
    model_context = ollama/qwen3:32b=32768

Where each one goes on the wire:

- claude-code seat: ``--effort`` (low, medium, high, xhigh, max)
- codex seat: ``-c model_reasoning_effort`` (low, medium, high, xhigh)
- anthropic: ``output_config.effort``, with the levels each model documents
- openai: ``reasoning_effort`` on reasoning models that tool-call with it
  (GPT-6 and GPT-5.6 must send "none" with tools, so they have no choice)
- openrouter: ``reasoning_effort`` (unset stays "low", see _openrouter_body)
- ollama: ``think`` (off/on, or low/medium/high on gpt-oss) and ``num_ctx``

Every other provider fixes both on the server, so it offers no choice.
A stored value a model does not accept is never sent.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

EFFORT_KEY = "model_effort"
CONTEXT_KEY = "model_context"

_ALL_LEVELS = ("low", "medium", "high", "xhigh", "max")
_CODEX_LEVELS = ("low", "medium", "high", "xhigh")
_BASIC_LEVELS = ("low", "medium", "high")
_THINK_TOGGLE = ("off", "on")
# Anthropic's documented levels per model family (prefix match, first wins).
_ANTHROPIC_LEVELS: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...]], ...] = (
    (("claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-opus-4-8",
      "claude-opus-4-7", "claude-fable-5", "claude-mythos-5"), _ALL_LEVELS),
    (("claude-opus-4-6", "claude-sonnet-4-6"), ("low", "medium", "high", "max")),
    (("claude-opus-4-5",), _BASIC_LEVELS),
)
_OPENAI_REASONING = ("o1", "o3", "o4", "gpt-5")
# Context sizes offered for Ollama's num_ctx, up to the model's maximum.
CONTEXT_LADDER = (4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576)
# When the server cannot say how far a model goes, offer up to this much.
_UNKNOWN_MAX_CONTEXT = 131072


# --- storage ---------------------------------------------------------------------

def _parse(raw) -> List[Tuple[str, str, str]]:
    """``provider/model=value, ...`` as (provider, model, value) triples."""
    found: List[Tuple[str, str, str]] = []
    for item in str(raw or "").split(","):
        pair, equals, value = item.strip().rpartition("=")
        provider, slash, model = pair.strip().partition("/")
        provider, model, value = provider.strip().lower(), model.strip(), value.strip().lower()
        if equals and slash and provider and model and value:
            found = [entry for entry in found if entry[:2] != (provider, model)]
            found.append((provider, model, value))
    return found


def _format(entries: List[Tuple[str, str, str]]) -> str:
    return ", ".join(f"{provider}/{model}={value}" for provider, model, value in entries)


def _same_model(provider: str, stored: str, model: str) -> bool:
    if stored == model:
        return True
    if provider == "ollama":
        from .providers import ollama_model_matches

        return ollama_model_matches(model, [stored]) or ollama_model_matches(stored, [model])
    return False


def _lookup(config: Optional[dict], key: str, provider: str, model: str) -> str:
    provider = (provider or "").lower()
    for stored_provider, stored_model, value in _parse((config or {}).get(key)):
        if stored_provider == provider and _same_model(provider, stored_model, model or ""):
            return value
    return ""


def _store(config: dict, key: str, provider: str, model: str, value: str) -> None:
    from .config import set_config_values

    provider = provider.lower()
    entries = [entry for entry in _parse(config.get(key))
               if not (entry[0] == provider and _same_model(provider, entry[1], model))]
    if value:
        entries.append((provider, model, value))
    text = _format(entries)
    set_config_values({key: text})
    config[key] = text


# --- what each model offers ------------------------------------------------------

def _ollama_think_levels(model: str, config: Optional[dict]) -> Tuple[str, ...]:
    from .providers import _ollama_show_cached

    data, record = _ollama_show_cached(model, config)
    caps = (data or {}).get("capabilities") if record is not None else None
    if isinstance(caps, list) and not any(str(cap).lower() == "thinking" for cap in caps):
        return ()
    if model.lower().startswith("gpt-oss"):
        return _BASIC_LEVELS
    return _THINK_TOGGLE


def effort_levels(provider: str, model: str, config: Optional[dict] = None,
                  *, probe: bool = True) -> Tuple[str, ...]:
    """The effort values *model* accepts on *provider*, () when it has none.
    For Ollama this asks the server whether the model thinks, unless *probe*
    is False (then every Ollama value is accepted)."""
    provider, name = (provider or "").lower(), (model or "").lower()
    if provider == "claude-code":
        return _ALL_LEVELS
    if provider == "codex":
        return _CODEX_LEVELS
    if provider == "anthropic":
        for prefixes, levels in _ANTHROPIC_LEVELS:
            if name.startswith(prefixes):
                return levels
        return ()
    if provider == "openai":
        from .providers import _openai_tools_require_effort_none

        if name.startswith(_OPENAI_REASONING) and not _openai_tools_require_effort_none(name):
            return _BASIC_LEVELS
        return ()
    if provider == "openrouter":
        return _BASIC_LEVELS
    if provider == "ollama":
        if not probe:
            return _THINK_TOGGLE + _BASIC_LEVELS
        return _ollama_think_levels(model, config)
    return ()


def context_sizes(provider: str, model: str, config: Optional[dict] = None) -> Tuple[int, ...]:
    """Context windows Conch can request for *model*, () when the provider
    fixes it. Only Ollama sizes the window per request (num_ctx)."""
    if (provider or "").lower() != "ollama":
        return ()
    from .providers import get_ollama_context_length

    top = get_ollama_context_length(model, config) or _UNKNOWN_MAX_CONTEXT
    sizes = tuple(size for size in CONTEXT_LADDER if size < top)
    return sizes + (top,)


# --- reading and choosing --------------------------------------------------------

def effort_for(config: Optional[dict], provider: str, model: str) -> str:
    """The stored effort for *model*, or "" when unset or not one the model
    accepts. Request builders call this; it never touches the network."""
    value = _lookup(config, EFFORT_KEY, provider, model)
    return value if value in effort_levels(provider, model, config, probe=False) else ""


def context_for(config: Optional[dict], provider: str, model: str) -> int:
    """The stored context window for *model*, or 0 when unset."""
    if (provider or "").lower() != "ollama":
        return 0
    try:
        value = int(_lookup(config, CONTEXT_KEY, provider, model) or 0)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _next(options: Tuple, current) -> object:
    """The value after *current*: default → first → ... → last → default."""
    if current in options:
        index = options.index(current) + 1
        return options[index] if index < len(options) else None
    return options[0]


def cycle_effort(config: dict, provider: str, model: str) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Step *model*'s effort to the next level and save it. Returns (the new
    value or None for the provider default, the levels offered)."""
    levels = effort_levels(provider, model, config)
    if not levels:
        return None, ()
    value = _next(levels, _lookup(config, EFFORT_KEY, provider, model))
    _store(config, EFFORT_KEY, provider, model, value or "")
    return value, levels


def cycle_context(config: dict, provider: str, model: str) -> Tuple[Optional[int], Tuple[int, ...]]:
    """Step *model*'s context window to the next size and save it. Returns
    (the new size or None for the server default, the sizes offered)."""
    sizes = context_sizes(provider, model, config)
    if not sizes:
        return None, ()
    value = _next(sizes, context_for(config, provider, model) or None)
    _store(config, CONTEXT_KEY, provider, model, str(value) if value else "")
    return value, sizes


def context_label(tokens: int) -> str:
    """32768 → 32K, 1048576 → 1M (context sizes are powers of two)."""
    if tokens >= 1048576 and tokens % 1048576 == 0:
        return f"{tokens // 1048576}M"
    if tokens >= 1024 and tokens % 1024 == 0:
        return f"{tokens // 1024}K"
    return f"{tokens:,}"


def describe(config: Optional[dict], provider: str, model: str) -> str:
    """Short label of what is set, e.g. ``high`` or ``on · 32K ctx``; "" when
    the model runs on its defaults."""
    parts = []
    effort = _lookup(config, EFFORT_KEY, provider, model)
    if effort:
        parts.append(effort)
    context = context_for(config, provider, model)
    if context:
        parts.append(f"{context_label(context)} ctx")
    return " · ".join(parts)
