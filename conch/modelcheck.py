"""Startup model validation: verify the selected model before the first call.

Every surface that is about to talk to a model — the interactive shell,
the one-shot ``conch "…"``, ``conch-ask``, the first-run wizard, and the
edge daemon's mission sessions — first runs one cheap, bounded probe of
the configured provider/model. The probe itself never prompts and never
needs a TTY. What happens when it fails depends on the surface:

* **interactive TTY** — a one-line diagnosis (by failure class), then a
  ranked list of alternatives probed in parallel within a few seconds:
  other verified models of the same provider (only when the failure is
  model-level), other providers whose key variable is present, and the
  local endpoints conch can see (the configured endpoint, Ollama, the
  llama-idx registry). The user picks one; the pick is verified; only
  then is it used — for this session, or, with a second explicit
  approval, saved as the new default. Declining exits cleanly, non-zero.
* **non-interactive** (``--non-interactive``, a pipe, cron, conch-ask,
  the daemon) — never prompts. ``fallback_models`` is the user's
  pre-consent: its entries are tried in order and the one in use is
  announced. With no list, or no working entry, the run fails closed
  with a message naming the problem and the alternatives that answered.

Nothing here switches a model without one of those two forms of
approval, and nothing here prints or logs a credential — keys are only
ever referred to by the name of their environment variable.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import get_config_path, local_only_enabled, set_config_values

# Exit status for a run that fails closed on the model check (sysexits
# EX_UNAVAILABLE: "a service is unavailable"). Distinct from the config
# errors (2) and the daemon's lock/journal codes (75/65/78).
MODEL_CHECK_EXIT_CODE = 69

DEFAULT_TIMEOUT = 4.0
MIN_TIMEOUT, MAX_TIMEOUT = 0.5, 60.0
# Parallel alternative discovery is bounded by this many seconds in total.
MAX_DISCOVERY_BUDGET = 30.0
# Candidate caps keep the probe fan-out (and the menu) small.
SAME_PROVIDER_CANDIDATES = 3
LOCAL_ENDPOINT_CANDIDATES = 2
LLAMAIDX_CANDIDATES = 4

CLOUD_PROVIDERS = ("cerebras", "anthropic", "openai", "bedrock", "openrouter")

# Failure classes.
OK = "ok"
NO_KEY = "no_key"
UNREACHABLE = "unreachable"
AUTH_FAILED = "auth_failed"
MODEL_NOT_FOUND = "model_not_found"
TIMEOUT = "timeout"
CONFORMANCE_FAILED = "conformance_failed"
RATE_LIMITED = "rate_limited"
QUOTA = "quota"
SERVER_ERROR = "server_error"
MISCONFIGURED = "misconfigured"
ERROR = "error"

# Classes where the provider (endpoint, key, account) is at fault, so its
# other models would fail the same way and are not offered.
PROVIDER_LEVEL = frozenset(
    {NO_KEY, UNREACHABLE, AUTH_FAILED, TIMEOUT, QUOTA, SERVER_ERROR, MISCONFIGURED}
)

STATUS_LABELS = {
    OK: "verified",
    NO_KEY: "no API key",
    UNREACHABLE: "unreachable",
    AUTH_FAILED: "authentication failed",
    MODEL_NOT_FOUND: "model not found",
    TIMEOUT: "timed out",
    CONFORMANCE_FAILED: "failed tool-call conformance",
    RATE_LIMITED: "rate limited",
    QUOTA: "quota/billing exhausted",
    SERVER_ERROR: "server error",
    MISCONFIGURED: "misconfigured",
    ERROR: "error",
}


@dataclass
class ProbeResult:
    provider: str
    model: str
    status: str
    detail: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def describe(self) -> str:
        """``<class label>: <detail>`` — the one-line diagnosis."""
        head = STATUS_LABELS.get(self.status, self.status)
        return f"{head} — {self.detail}" if self.detail else head


@dataclass
class Candidate:
    """One alternative the user may switch to."""

    provider: str
    model: str
    source: str  # same-provider | provider | ollama | custom | llamaidx | fallback
    overrides: Dict[str, str] = field(default_factory=dict)
    note: str = ""
    display: str = ""
    result: Optional[ProbeResult] = None

    @property
    def label(self) -> str:
        return self.display or f"{self.provider}/{self.model}"

    def config_updates(self) -> Dict[str, str]:
        """Config keys that route a session through this candidate.

        Also exactly what gets persisted on "save as default": provider,
        model, the key *variable name* (never a key), and the endpoint
        keys the candidate carries (llama-idx entries bring their own
        base URL). Nothing else in the config is touched.
        """
        from .providers import DEFAULT_API_KEY_ENVS

        updates: Dict[str, str] = {
            "provider": self.provider,
            "model": self.model,
            "chat_model": self.model,
        }
        updates.update(self.overrides)
        if self.provider == "custom":
            updates["custom_model"] = self.model
        if "api_key_env" not in updates:
            # Always explicit: the previous provider's key variable must
            # never ride along to a different endpoint. A custom endpoint
            # that needs a key names it in its overrides (the registry
            # does; a same-endpoint candidate inherits the configured one).
            updates["api_key_env"] = (
                DEFAULT_API_KEY_ENVS.get(self.provider, "")
                if self.provider in CLOUD_PROVIDERS else ""
            )
        return updates

    def apply(self, config: dict) -> None:
        config.update(self.config_updates())


@dataclass
class ModelCheckOutcome:
    provider: str
    model: str
    result: Optional[ProbeResult]  # None when the check is disabled
    checked: bool = True
    switched: bool = False
    saved: bool = False
    via_fallback: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


def model_check_enabled(config: dict) -> bool:
    """``model_check`` (default on). ``off``/``false``/``0`` disables the
    startup probe entirely — nothing is probed and nothing is switched."""
    raw = str((config or {}).get("model_check", "on")).strip().lower()
    if raw in ("off", "false", "0", "no", "disabled"):
        return False
    return True


def model_check_timeout(config: dict) -> float:
    """Per-request probe timeout in seconds (``model_check_timeout``,
    default 4), clamped to a sane range."""
    raw = (config or {}).get("model_check_timeout", DEFAULT_TIMEOUT)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return min(MAX_TIMEOUT, max(MIN_TIMEOUT, value))


def discovery_budget(timeout: float) -> float:
    """Wall-clock bound for probing every alternative in parallel."""
    return min(MAX_DISCOVERY_BUDGET, max(5.0, 2.0 * timeout))


def parse_fallback_models(raw: str) -> List[str]:
    """``fallback_models`` entries in order: ``provider/model`` tokens
    separated by commas, whitespace, or newlines."""
    if not raw:
        return []
    entries: List[str] = []
    for chunk in str(raw).replace("\n", ",").replace(";", ",").split(","):
        for token in chunk.split():
            token = token.strip()
            if token and token not in entries:
                entries.append(token)
    return entries


# ---------------------------------------------------------------------------
# HTTP plumbing (bounded, classified)
# ---------------------------------------------------------------------------


class _Unreachable(Exception):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return True
    return "timed out" in str(exc).lower()


def _request(url: str, headers: Dict[str, str], timeout: float,
             body: Optional[dict] = None) -> Tuple[int, object]:
    """One bounded HTTP exchange. Returns ``(status, parsed_json_or_text)``
    for any HTTP response (errors included); raises :class:`_Unreachable`
    (classified ``timeout``/``unreachable``) when no response came back."""
    data = json.dumps(body).encode() if body is not None else None
    merged = {"User-Agent": "conch/1.0"}
    merged.update(headers)
    if data is not None:
        merged.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(
        url, data=data, headers=merged, method="POST" if data is not None else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        finally:
            close = getattr(exc, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        status = exc.code
    except Exception as exc:
        if _is_timeout(exc):
            raise _Unreachable(TIMEOUT, f"timed out after {timeout:g}s") from exc
        reason = getattr(exc, "reason", None) or exc
        raise _Unreachable(UNREACHABLE, _short(str(reason))) from exc
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    try:
        return int(status), json.loads(text) if text.strip() else {}
    except ValueError:
        return int(status), text


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error_message(payload) -> Tuple[str, str]:
    """(message, code) pulled from the usual provider error envelopes."""
    if isinstance(payload, str):
        return _short(payload), ""
    if not isinstance(payload, dict):
        return "", ""
    err = payload.get("error")
    if isinstance(err, dict):
        message = err.get("message") or err.get("msg") or json.dumps(err)[:200]
        code = str(err.get("code") or err.get("type") or "")
        return _short(str(message)), code
    if err is not None:
        return _short(str(err)), ""
    for key in ("message", "detail", "msg"):
        if payload.get(key):
            return _short(str(payload[key])), str(payload.get("code") or "")
    return "", ""


_MODEL_MISSING_MARKERS = (
    "not found", "does not exist", "doesn't exist", "unknown model", "no such",
    "not a valid model", "invalid model", "is not supported", "unsupported model",
    "not available", "could not find", "no model", "is not exposed",
)
_QUOTA_MARKERS = ("quota", "credit", "billing", "insufficient", "balance", "exceeded your")
_PARAM_NIT_MARKERS = (
    "max_tokens", "max_completion_tokens", "thinking", "temperature", "reasoning",
    "unsupported parameter", "unsupported value",
)


def _classify_http(status: int, payload, model: str) -> Tuple[str, str]:
    message, code = _error_message(payload)
    low = f"{message} {code}".lower()
    shown = message or "no error message"
    if status in (401, 403):
        return AUTH_FAILED, f"HTTP {status}: {shown}"
    if status == 402:
        return QUOTA, f"HTTP {status}: {shown}"
    if status == 429:
        if any(marker in low for marker in _QUOTA_MARKERS):
            return QUOTA, f"HTTP 429: {shown}"
        return RATE_LIMITED, f"HTTP 429: {shown}"
    if status == 404:
        return MODEL_NOT_FOUND, f"HTTP 404: {shown}"
    if status == 400 or status == 422:
        if code == "model_not_found" or (
            "model" in low and any(marker in low for marker in _MODEL_MISSING_MARKERS)
        ):
            return MODEL_NOT_FOUND, f"HTTP {status}: {shown}"
        if any(marker in low for marker in _QUOTA_MARKERS):
            return QUOTA, f"HTTP {status}: {shown}"
        if any(marker in low for marker in _PARAM_NIT_MARKERS):
            # The request reached model validation: the model exists and
            # the key works; only the probe's minimal parameters were
            # rejected. That is a pass for a reachability check.
            return OK, f"model accepted the request (probe parameter nit: {shown})"
        return ERROR, f"HTTP {status}: {shown}"
    if status >= 500:
        return SERVER_ERROR, f"HTTP {status}: {shown}"
    return ERROR, f"HTTP {status}: {shown}"


# ---------------------------------------------------------------------------
# Per-provider probes
# ---------------------------------------------------------------------------


def cloud_base_url(provider: str, config: dict) -> str:
    """API root for a cloud provider's models-list / completion endpoints.
    Cerebras and Bedrock honour the same overrides their adapters use."""
    from .providers import OPENROUTER_BASE_URL, get_bedrock_base_url

    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "anthropic":
        return "https://api.anthropic.com/v1"
    if provider == "openrouter":
        return OPENROUTER_BASE_URL
    if provider == "cerebras":
        return (
            (config.get("base_url") or "").strip()
            or os.environ.get("CEREBRAS_BASE_URL", "").strip()
            or "https://api.cerebras.ai/v1"
        ).rstrip("/")
    if provider == "bedrock":
        return get_bedrock_base_url(config)
    return ""


def _cloud_endpoints(provider: str, config: dict, api_key: str):
    base = cloud_base_url(provider, config).rstrip("/")
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        return f"{base}/models?limit=1000", f"{base}/messages", headers
    headers = {"Authorization": f"Bearer {api_key}"}
    return f"{base}/models", f"{base}/chat/completions", headers


def _model_ids(payload) -> List[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload.get("models", []))
    ids: List[str] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            ids.append(item.strip())
        elif isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name")
            if value:
                ids.append(str(value).strip())
    return [value for value in ids if value]


def _listed(model: str, ids: List[str]) -> Optional[str]:
    """The listed id matching *model*: exact, or the dated form
    (``claude-sonnet-5`` ↔ ``claude-sonnet-5-20260101``)."""
    if model in ids:
        return model
    prefix = model + "-"
    for value in ids:
        if value.startswith(prefix) and value[len(prefix):].isdigit():
            return value
    return None


def _minimal_completion_body(provider: str, model: str) -> dict:
    messages = [{"role": "user", "content": "ping"}]
    if provider == "anthropic":
        return {"model": model, "max_tokens": 1, "messages": messages}
    if provider == "openai":
        from .providers import build_openai_chat_request_body

        return build_openai_chat_request_body(
            model, messages, temperature=0, max_completion_tokens=1
        )
    return {"model": model, "messages": messages, "max_tokens": 1}


def _probe_cloud(provider: str, model: str, config: dict, timeout: float) -> ProbeResult:
    from .providers import KNOWN_MODELS, api_key_env_for, suggest_models

    key_env = api_key_env_for(provider, config)
    api_key = os.environ.get(key_env, "").strip() if key_env else ""
    if not api_key:
        from .config import get_env_file_path

        return ProbeResult(
            provider, model, NO_KEY,
            f"{key_env or 'api_key_env'} is not set (set it in"
            f" {get_env_file_path()} or the environment)",
        )
    if not model:
        return ProbeResult(provider, model, MISCONFIGURED, "no model configured")
    known = KNOWN_MODELS.get(provider)
    if known is not None and model not in known:
        suggestions = suggest_models(model, known)
        hint = f" — did you mean {', '.join(suggestions)}?" if suggestions else ""
        return ProbeResult(
            provider, model, MODEL_NOT_FOUND,
            f"'{model}' isn't in conch's {provider} catalog of tool-capable"
            f" models{hint}",
        )
    models_url, completions_url, headers = _cloud_endpoints(provider, config, api_key)
    try:
        status, payload = _request(models_url, headers, timeout)
    except _Unreachable as exc:
        return ProbeResult(provider, model, exc.status, f"{exc.detail} ({models_url})")
    if status == 200:
        match = _listed(model, _model_ids(payload))
        if match:
            detail = "listed by the provider"
            if match != model:
                detail += f" as {match}"
            return ProbeResult(provider, model, OK, detail)
        # Not listed (an alias the list omits, or gone): let a minimal
        # completion decide.
    elif status in (401, 402, 403, 429) or status >= 500:
        klass, detail = _classify_http(status, payload, model)
        return ProbeResult(provider, model, klass, detail)
    # A list endpoint that 404s/400s (gateways without one) also falls
    # through to the completion probe.
    try:
        status, payload = _request(
            completions_url, headers, timeout, body=_minimal_completion_body(provider, model)
        )
    except _Unreachable as exc:
        return ProbeResult(provider, model, exc.status, f"{exc.detail} ({completions_url})")
    if status == 200:
        return ProbeResult(provider, model, OK, "answered a 1-token completion")
    klass, detail = _classify_http(status, payload, model)
    return ProbeResult(provider, model, klass, detail)


def _probe_ollama(model: str, config: dict, timeout: float) -> ProbeResult:
    from .providers import (
        get_ollama_base_url,
        local_endpoint_policy_error,
        ollama_model_matches,
        ollama_model_supports_tools,
    )

    base = get_ollama_base_url(config)
    policy = local_endpoint_policy_error("ollama", base, config)
    if policy:
        return ProbeResult("ollama", model, MISCONFIGURED, policy)
    if not model:
        return ProbeResult("ollama", model, MISCONFIGURED, "no model configured")
    try:
        status, payload = _request(f"{base}/api/tags", {}, timeout)
    except _Unreachable as exc:
        return ProbeResult(
            "ollama", model, exc.status, f"Ollama server {exc.detail} at {base}"
        )
    if status != 200:
        klass, detail = _classify_http(status, payload, model)
        if klass == MODEL_NOT_FOUND:
            klass = SERVER_ERROR  # /api/tags itself missing: not an Ollama server
        return ProbeResult("ollama", model, klass, f"{detail} ({base}/api/tags)")
    names = _model_ids(payload)
    if not names:
        return ProbeResult(
            "ollama", model, MODEL_NOT_FOUND,
            f"no models installed on the Ollama server at {base}"
            " (e.g. `ollama pull qwen2.5`)",
        )
    if not ollama_model_matches(model, names):
        shown = ", ".join(names[:6]) + (", …" if len(names) > 6 else "")
        return ProbeResult(
            "ollama", model, MODEL_NOT_FOUND,
            f"'{model}' is not installed on the Ollama server at {base}"
            f" (installed: {shown})",
        )
    resolved = model if model in names else next(
        (name for name in names if name.split(":", 1)[0] == model), model
    )
    support = ollama_model_supports_tools(resolved, config, timeout=timeout)
    if support is True:
        return ProbeResult("ollama", model, OK, f"installed at {base}, tools capability advertised")
    if support is None:
        return ProbeResult(
            "ollama", model, CONFORMANCE_FAILED,
            f"'{resolved}' has no verifiable native tool capability"
            " (the server did not report capabilities)",
        )
    return ProbeResult(
        "ollama", model, CONFORMANCE_FAILED,
        f"'{resolved}' does not advertise tool calling on the Ollama server",
    )


def _probe_custom(model: str, config: dict, timeout: float) -> ProbeResult:
    from .providers import (
        _custom_headers,
        _custom_model_records,
        get_custom_base_url,
        local_endpoint_policy_error,
        probe_custom_model,
    )

    base = get_custom_base_url(config)
    if not base:
        return ProbeResult(
            "custom", model, MISCONFIGURED,
            "custom_base_url is not configured",
        )
    policy = local_endpoint_policy_error("custom", base, config)
    if policy:
        return ProbeResult("custom", model, MISCONFIGURED, policy)
    if not model:
        return ProbeResult(
            "custom", model, MISCONFIGURED,
            f"custom_model is not configured for {base}",
        )
    headers = {
        key: value for key, value in _custom_headers(config).items()
        if key != "Content-Type"
    }
    try:
        status, payload = _request(f"{base}/models", headers, timeout)
    except _Unreachable as exc:
        return ProbeResult("custom", model, exc.status, f"endpoint {exc.detail} at {base}")
    if status != 200:
        klass, detail = _classify_http(status, payload, model)
        if klass == MODEL_NOT_FOUND:
            klass = SERVER_ERROR  # the list endpoint itself is missing
        return ProbeResult("custom", model, klass, f"{detail} ({base}/models)")
    ids = _model_ids(payload)
    if model not in ids:
        shown = ", ".join(ids[:6]) + (", …" if len(ids) > 6 else "")
        return ProbeResult(
            "custom", model, MODEL_NOT_FOUND,
            f"'{model}' is not exposed by {base}/models"
            + (f" (exposed: {shown})" if ids else " (the endpoint lists no models)"),
        )
    # Prime the adapter's own record cache (identity-keyed probe cache)
    # so the first real turn does not repeat the conformance call.
    records = _custom_model_records(config, timeout=timeout, force_refresh=True) or []
    record = next((item for item in records if item["id"] == model), None)
    ok, reason = probe_custom_model(
        config, model, timeout=timeout,
        identity=record["identity"] if record else "",
        force_refresh=True,
    )
    if ok is True:
        return ProbeResult("custom", model, OK, f"native tool call verified at {base}")
    low = (reason or "").lower()
    if "timed out" in low:
        return ProbeResult("custom", model, TIMEOUT, reason)
    if "unreachable" in low:
        return ProbeResult("custom", model, UNREACHABLE, reason)
    if any(marker in low for marker in ("authentication", "api key", "unauthorized", "forbidden")):
        return ProbeResult("custom", model, AUTH_FAILED, reason)
    return ProbeResult("custom", model, CONFORMANCE_FAILED, reason or "no native tool call returned")


def probe_model(provider: str, model: str, config: dict, *,
                timeout: Optional[float] = None) -> ProbeResult:
    """One bounded probe of *provider*/*model* using *config* for
    endpoints and key variable names. Never raises, never prompts.

    Cloud providers: the catalog gate, then the models-list endpoint
    (the configured id must be listed), then — only when the list does
    not settle it — a 1-token completion. Ollama: ``/api/tags`` plus the
    advertised ``tools`` capability. Custom endpoints: ``/v1/models``
    plus the forced native tool-call conformance probe.
    """
    provider = (provider or "").lower()
    model = (model or "").strip()
    config = config or {}
    timeout = model_check_timeout(config) if timeout is None else float(timeout)
    started = time.monotonic()
    try:
        if provider == "ollama":
            result = _probe_ollama(model, config, timeout)
        elif provider == "custom":
            result = _probe_custom(model, config, timeout)
        elif provider in CLOUD_PROVIDERS:
            result = _probe_cloud(provider, model, config, timeout)
        else:
            result = ProbeResult(provider, model, MISCONFIGURED, f"unknown provider '{provider}'")
    except Exception as exc:  # a probe must never take the shell down
        result = ProbeResult(provider, model, ERROR, f"{type(exc).__name__}: {_short(str(exc))}")
    result.elapsed = time.monotonic() - started
    return result


def probe_candidate(candidate: Candidate, config: dict, timeout: float) -> ProbeResult:
    merged = dict(config)
    merged.update(candidate.config_updates())
    result = probe_model(candidate.provider, candidate.model, merged, timeout=timeout)
    candidate.result = result
    return result


def _routing_key(candidate: Optional[Candidate], config: dict) -> tuple:
    """(provider, model, endpoint) a candidate would route to — or the
    configured routing when *candidate* is None."""
    from .providers import get_custom_base_url, get_ollama_base_url

    merged = dict(config)
    if candidate is not None:
        merged.update(candidate.config_updates())
    provider = (merged.get("provider") or "").lower()
    model = (merged.get("chat_model") or merged.get("model") or "").strip()
    endpoint = ""
    if provider == "custom":
        endpoint = get_custom_base_url(merged)
    elif provider == "ollama":
        endpoint = get_ollama_base_url(merged)
    return provider, model, endpoint


# ---------------------------------------------------------------------------
# Alternatives
# ---------------------------------------------------------------------------


def _same_provider_models(provider: str, model: str, config: dict, timeout: float) -> List[str]:
    from .providers import (
        DEFAULT_CHAT_MODEL_BY_PROVIDER,
        KNOWN_MODELS,
        list_custom_models,
        list_ollama_models,
    )

    if provider == "ollama":
        names = list_ollama_models(config, timeout=timeout) or []
    elif provider == "custom":
        names = list_custom_models(config, timeout=timeout, tool_capable_only=False) or []
    else:
        names = list(KNOWN_MODELS.get(provider, []))
    preferred = DEFAULT_CHAT_MODEL_BY_PROVIDER.get(provider, "")
    ordered: List[str] = []
    if preferred and preferred in names:
        ordered.append(preferred)
    ordered.extend(name for name in names if name not in ordered)
    return [name for name in ordered if name != model][:SAME_PROVIDER_CANDIDATES]


def _same_endpoint_overrides(provider: str, config: dict) -> Dict[str, str]:
    """Routing keys another model on the *configured* endpoint shares with
    the current one (its key variable name and base URL)."""
    if provider == "custom":
        from .providers import get_custom_base_url

        return {
            "custom_base_url": get_custom_base_url(config),
            "api_key_env": str(config.get("api_key_env") or ""),
        }
    if provider == "ollama":
        from .providers import get_ollama_base_url

        return {"ollama_base_url": get_ollama_base_url(config)}
    return {"api_key_env": str(config.get("api_key_env") or "")}


def discover_alternatives(config: dict, provider: str, model: str,
                          failure: ProbeResult, *, timeout: float) -> Tuple[List[Candidate], List[str]]:
    """Ranked, unprobed candidates plus explanatory notes.

    Order: same-provider models (model-level failures only), other
    providers whose key variable is present (policy permitting), then the
    local endpoints — the Ollama server, a configured custom endpoint not
    currently in use, and the llama-idx registry (when configured). Every
    network lookup here is bounded by *timeout*.
    """
    from .providers import (
        CROSS_PROVIDER_FALLBACK_ORDER,
        DEFAULT_API_KEY_ENVS,
        DEFAULT_CHAT_MODEL_BY_PROVIDER,
        _has_key,
        get_custom_base_url,
        get_ollama_base_url,
        list_custom_models,
        list_ollama_models,
    )

    candidates: List[Candidate] = []
    notes: List[str] = []
    provider_level = failure.status in PROVIDER_LEVEL
    local_only = local_only_enabled(config, provider)

    if provider_level:
        notes.append(
            f"other {provider} models skipped: the failure is provider-level"
            f" ({STATUS_LABELS.get(failure.status, failure.status)})"
        )
    else:
        try:
            same = _same_provider_models(provider, model, config, timeout)
        except Exception:
            same = []
        same_overrides = _same_endpoint_overrides(provider, config)
        for name in same:
            candidates.append(Candidate(
                provider, name, "same-provider", overrides=dict(same_overrides),
                note="same provider",
            ))

    excluded: List[str] = []
    for other in CROSS_PROVIDER_FALLBACK_ORDER:
        if other == provider or other not in CLOUD_PROVIDERS:
            continue
        if not _has_key(other):
            continue
        if local_only:
            excluded.append(other)
            continue
        candidates.append(Candidate(
            other, DEFAULT_CHAT_MODEL_BY_PROVIDER.get(other, ""), "provider",
            note=f"key {DEFAULT_API_KEY_ENVS.get(other, '')} present",
        ))
    if excluded:
        notes.append(
            "cloud providers with keys present excluded by local_only="
            f"{config.get('local_only', 'auto')}: {', '.join(excluded)}"
            " (set local_only=false to offer them)"
        )

    if provider != "ollama":
        base = get_ollama_base_url(config)
        try:
            names = list_ollama_models(config, timeout=timeout) or []
        except Exception:
            names = []
        preferred = DEFAULT_CHAT_MODEL_BY_PROVIDER.get("ollama", "")
        ordered = [n for n in names if n.split(":", 1)[0] == preferred or n == preferred]
        ordered += [n for n in names if n not in ordered]
        for name in ordered[:LOCAL_ENDPOINT_CANDIDATES]:
            candidates.append(Candidate("ollama", name, "ollama", note=f"Ollama at {base}"))

    custom_base = get_custom_base_url(config)
    if provider != "custom" and custom_base:
        try:
            ids = list_custom_models(config, timeout=timeout, tool_capable_only=False) or []
        except Exception:
            ids = []
        for name in ids[:LOCAL_ENDPOINT_CANDIDATES]:
            candidates.append(Candidate(
                "custom", name, "custom",
                overrides={"custom_base_url": custom_base},
                note=f"custom endpoint {custom_base}",
            ))

    candidates.extend(_llamaidx_candidates(config, provider, provider_level))

    seen = set()
    unique: List[Candidate] = []
    for candidate in candidates:
        key = (
            candidate.provider, candidate.model,
            candidate.overrides.get("custom_base_url", ""),
            candidate.overrides.get("ollama_base_url", ""),
        )
        if key in seen or not candidate.model:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique, notes


def _llamaidx_candidates(config: dict, provider: str, provider_level: bool) -> List[Candidate]:
    from .llamaidx import get_llamaidx_url, list_llamaidx_models, llamaidx_selection_overrides
    from .providers import get_custom_base_url, get_ollama_base_url

    if not get_llamaidx_url(config):
        return []  # feature off: zero registry traffic
    try:
        entries = list_llamaidx_models(config) or []
    except Exception:
        return []
    failing_bases = set()
    if provider_level and provider in ("ollama", "custom"):
        base = (
            get_ollama_base_url(config) if provider == "ollama"
            else get_custom_base_url(config)
        ).rstrip("/")
        failing_bases.add(base)
        if base.endswith("/v1"):
            failing_bases.add(base[: -len("/v1")])
    usable = [
        entry for entry in entries
        if not entry.get("degraded")
        and entry["base_url"].rstrip("/") not in failing_bases
    ]
    usable.sort(key=lambda entry: (-(entry.get("ctx") or 0), entry["name"]))
    result = []
    for entry in usable[:LLAMAIDX_CANDIDATES]:
        overrides = llamaidx_selection_overrides(entry)
        mapped = overrides.pop("provider")
        for key in ("model", "chat_model", "custom_model"):
            overrides.pop(key, None)
        result.append(Candidate(
            mapped, entry["model_id"], "llamaidx", overrides=overrides,
            note=f"llama-idx {entry['provider_name']} ({entry['base_url']})",
            display=entry["name"],
        ))
    return result


def probe_candidates(candidates: List[Candidate], config: dict, *,
                     timeout: float, budget: Optional[float] = None) -> None:
    """Probe every candidate in parallel, bounded by *budget* seconds in
    total; candidates still unanswered at the deadline are marked
    ``timeout`` (the threads finish on their own socket timeouts)."""
    if not candidates:
        return
    budget = discovery_budget(timeout) if budget is None else budget
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(candidates)))
    futures = {
        pool.submit(probe_candidate, candidate, config, timeout): candidate
        for candidate in candidates
    }
    done, pending = concurrent.futures.wait(futures, timeout=budget)
    for future in pending:
        candidate = futures[future]
        candidate.result = ProbeResult(
            candidate.provider, candidate.model, TIMEOUT,
            f"no answer within the {budget:g}s discovery budget",
        )
    pool.shutdown(wait=False)


def _rank(candidate: Candidate) -> tuple:
    order = {"same-provider": 0, "provider": 1, "ollama": 2, "custom": 3, "llamaidx": 4, "fallback": 5}
    verified = 0 if (candidate.result is not None and candidate.result.ok) else 1
    return (verified, order.get(candidate.source, 9), candidate.label)


# ---------------------------------------------------------------------------
# fallback_models (pre-approved, non-interactive)
# ---------------------------------------------------------------------------


def candidate_from_fallback(entry: str, config: dict) -> Tuple[Optional[Candidate], str]:
    """Resolve one ``fallback_models`` token; ``(None, reason)`` when it
    cannot be used (bad syntax, unknown provider, policy, unresolvable
    registry entry)."""
    from .providers import RAW_FNS

    entry = (entry or "").strip()
    if "/" not in entry:
        return None, f"'{entry}' is not provider/model"
    provider, model = entry.split("/", 1)
    provider = provider.strip().lower()
    model = model.strip()
    if not model:
        return None, f"'{entry}' has no model"
    if provider == "llamaidx":
        from .llamaidx import NAMESPACE_PREFIX, resolve_llamaidx_model, llamaidx_selection_overrides

        resolved = resolve_llamaidx_model(f"{NAMESPACE_PREFIX}{model}", config)
        if resolved is None:
            return None, f"'{entry}' is not in the llama-idx registry (or the registry is unreachable)"
        overrides = llamaidx_selection_overrides(resolved)
        mapped = overrides.pop("provider")
        for key in ("model", "chat_model", "custom_model"):
            overrides.pop(key, None)
        return Candidate(
            mapped, resolved["model_id"], "fallback", overrides=overrides, display=entry
        ), ""
    if provider not in RAW_FNS:
        return None, f"'{entry}' names an unknown provider"
    current = (config.get("provider") or "").lower()
    if provider in CLOUD_PROVIDERS and local_only_enabled(config, current):
        return None, f"'{entry}' skipped by local_only={config.get('local_only', 'auto')}"
    overrides: Dict[str, str] = {}
    if provider == current:
        overrides = _same_endpoint_overrides(provider, config)
    elif provider == "custom":
        from .providers import get_custom_base_url

        base = get_custom_base_url(config)
        if not base:
            return None, f"'{entry}' needs custom_base_url in the config"
        overrides = {"custom_base_url": base}
    return Candidate(provider, model, "fallback", overrides=overrides), ""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_default(candidate: Candidate) -> str:
    """Write the candidate's routing keys to the primary config file (in
    place; keys and every other setting untouched). Returns the path."""
    updates = {key: str(value) for key, value in candidate.config_updates().items()}
    return set_config_values(updates)


def _override_files() -> List[str]:
    """Config files that take precedence over the primary one, so the
    user knows a saved default may be shadowed."""
    from pathlib import Path

    from .config import find_project_rc

    found = []
    home_rc = Path.home() / ".conchrc"
    if home_rc.exists():
        found.append(str(home_rc))
    try:
        project = find_project_rc()
    except Exception:
        project = None
    if project is not None and project != home_rc:
        found.append(str(project))
    return found


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


_YES = ("y", "yes")
_DECLINE = ("q", "quit", "n", "no", "exit", "abort")


def _startup_error(message: str):
    from .bootstrap import StartupError

    return StartupError(message, code=MODEL_CHECK_EXIT_CODE)


def ensure_working_model(config: dict, *, interactive: bool,
                         input_fn: Optional[Callable[[str], str]] = None,
                         out=None,
                         announce: Optional[Callable[[str], None]] = None,
                         persist_mode: str = "ask",
                         discover: bool = True) -> ModelCheckOutcome:
    """Validate the configured model; recover with approval or fail closed.

    Mutates *config* in place when a switch is approved (interactive) or a
    ``fallback_models`` entry takes over (non-interactive) — callers that
    need isolation pass a copy. Raises :class:`conch.bootstrap.StartupError`
    (exit code :data:`MODEL_CHECK_EXIT_CODE`) when no working model is
    selected.

    ``input_fn``/``out`` drive the interactive flow (defaults: builtins
    ``input``/stdout, so scripted-input harnesses work); ``announce``
    receives the non-interactive one-liners (default: stderr).
    ``persist_mode`` is ``ask`` (session or default — the user chooses),
    ``always`` (the wizard: the pick becomes the default), or ``never``.
    """
    provider = (config.get("provider") or "").lower()
    model = (config.get("chat_model") or config.get("model") or "").strip()
    if not model_check_enabled(config):
        return ModelCheckOutcome(provider, model, None, checked=False)
    timeout = model_check_timeout(config)
    result = probe_model(provider, model, config, timeout=timeout)
    if result.ok:
        return ModelCheckOutcome(provider, model, result)
    if interactive:
        return _interactive_recovery(
            config, result, timeout=timeout, input_fn=input_fn, out=out,
            persist_mode=persist_mode,
        )
    return _non_interactive_recovery(
        config, result, timeout=timeout, announce=announce, discover=discover
    )


def _non_interactive_recovery(config: dict, failure: ProbeResult, *, timeout: float,
                              announce: Optional[Callable[[str], None]],
                              discover: bool) -> ModelCheckOutcome:
    say = announce or (lambda line: print(line, file=sys.stderr))
    entries = parse_fallback_models(str(config.get("fallback_models", "")))
    tried: List[str] = []
    for entry in entries:
        candidate, reason = candidate_from_fallback(entry, config)
        if candidate is None:
            tried.append(f"{entry}: {reason}")
            continue
        if _routing_key(candidate, config) == _routing_key(None, config):
            tried.append(f"{candidate.label}: the model that just failed")
            continue
        result = probe_candidate(candidate, config, timeout)
        if result.ok:
            candidate.apply(config)
            say(
                f"conch: {failure.label} unavailable ({failure.describe()});"
                f" using fallback_models entry {candidate.label}"
            )
            return ModelCheckOutcome(
                candidate.provider, candidate.model, result,
                switched=True, via_fallback=True,
            )
        tried.append(f"{candidate.label}: {result.describe()}")

    lines = [f"conch: model check failed — {failure.label} {failure.describe()}"]
    if entries:
        lines.append("  fallback_models exhausted: " + "; ".join(tried))
    else:
        lines.append(
            "  no fallback_models configured (the pre-approved alternatives"
            " a non-interactive run may switch to)"
        )
    if discover:
        candidates, notes = discover_alternatives(
            config, failure.provider, failure.model, failure, timeout=timeout
        )
        probe_candidates(candidates, config, timeout=timeout)
        working = [c.label for c in candidates if c.result is not None and c.result.ok]
        if working:
            lines.append("  alternatives that answered: " + ", ".join(working))
        elif candidates:
            lines.append("  no alternative answered: " + "; ".join(
                f"{c.label} {c.result.describe()}" for c in candidates if c.result is not None
            ))
        lines.extend(f"  {note}" for note in notes)
    lines.append(
        "  fix: run `conch` in a terminal to pick and save a working model,"
        f" or set fallback_models = <provider/model, …> in {get_config_path()}"
    )
    raise _startup_error("\n".join(lines))


def _interactive_recovery(config: dict, failure: ProbeResult, *, timeout: float,
                          input_fn, out, persist_mode: str) -> ModelCheckOutcome:
    import builtins

    ask = input_fn or builtins.input
    stream = out or sys.stdout

    def say(line: str = "") -> None:
        print(line, file=stream, flush=True)

    def read(prompt: str) -> Optional[str]:
        try:
            return ask(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            say()
            return None

    say(f"\033[33m  ⚠ {failure.label} is not usable: {failure.describe()}\033[0m")
    say("\033[2m  Looking for alternatives…\033[0m")
    candidates, notes = discover_alternatives(
        config, failure.provider, failure.model, failure, timeout=timeout
    )
    probe_candidates(candidates, config, timeout=timeout)
    for note in notes:
        say(f"\033[2m  ({note})\033[0m")
    if not candidates:
        say("\033[31m  No alternatives found.\033[0m")
        say(
            f"\033[2m  Fix the configured model, set fallback_models in"
            f" {get_config_path()}, or /provider once an endpoint is up.\033[0m"
        )
        raise _startup_error(
            f"conch: no working model — {failure.label} {failure.describe()} and no alternative"
            " was found"
        )

    remaining = sorted(candidates, key=_rank)
    chosen: Optional[Candidate] = None
    while remaining:
        say("  Alternatives:")
        for index, candidate in enumerate(remaining, start=1):
            result = candidate.result
            if result is not None and result.ok:
                mark = f"\033[1;32m✓ {result.describe()}\033[0m"
            elif result is not None:
                mark = f"\033[31m✗ {result.describe()}\033[0m"
            else:
                mark = "\033[2munverified\033[0m"
            note = f" \033[2m— {candidate.note}\033[0m" if candidate.note else ""
            say(f"    \033[1m{index}\033[0m. {candidate.label}{note}")
            say(f"       {mark}")
        default = next(
            (i for i, c in enumerate(remaining, start=1) if c.result is not None and c.result.ok),
            None,
        )
        hint = f", Enter={default}" if default else ""
        answer = read(f"  Switch to [1-{len(remaining)}{hint}, q=quit]: ")
        if answer is None or answer.lower() in _DECLINE:
            raise _startup_error("conch: no model switch approved — exiting")
        if not answer:
            if default is None:
                say("\033[31m  Pick a number (no verified alternative to default to).\033[0m")
                continue
            answer = str(default)
        pick: Optional[Candidate] = None
        if answer.isdigit() and 1 <= int(answer) <= len(remaining):
            pick = remaining[int(answer) - 1]
        else:
            pick = next((c for c in remaining if c.label == answer), None)
        if pick is None:
            say(f"\033[31m  Pick a number between 1 and {len(remaining)}.\033[0m")
            continue
        if pick.result is None or not pick.result.ok:
            say(f"\033[2m  Verifying {pick.label}…\033[0m")
            probe_candidate(pick, config, timeout)
        if pick.result is not None and pick.result.ok:
            chosen = pick
            break
        say(f"\033[31m  ✗ {pick.label}: {pick.result.describe() if pick.result else 'failed'}\033[0m")
        remaining.remove(pick)
        remaining.sort(key=_rank)
    if chosen is None:
        raise _startup_error(
            f"conch: no working model — every alternative to {failure.label} failed"
        )

    chosen.apply(config)
    say(f"\033[1;32m  ✓ Using {chosen.label} for this session\033[0m")
    saved = False
    if persist_mode == "always":
        path = persist_default(chosen)
        say(f"\033[2m  Saved as the default in {path}\033[0m")
        saved = True
    elif persist_mode == "ask":
        answer = read(
            f"  Save {chosen.label} as the new default in {get_config_path()}?"
            " [y/N] (Enter = this session only): "
        )
        if answer is not None and answer.lower() in _YES:
            path = persist_default(chosen)
            say(f"\033[2m  Saved as the default in {path}\033[0m")
            for shadow in _override_files():
                say(f"\033[33m  note: {shadow} takes precedence over that file\033[0m")
            saved = True
        else:
            say("\033[2m  Session only — the configured default is unchanged.\033[0m")
    return ModelCheckOutcome(
        chosen.provider, chosen.model, chosen.result, switched=True, saved=saved
    )
