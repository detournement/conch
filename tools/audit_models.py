#!/usr/bin/env python3
"""Audit conch's hardcoded cloud model catalogs against live provider APIs.

For every model in conch.providers.KNOWN_MODELS (cloud providers only —
ollama/custom are discovered and probed live at runtime), this script:

  1. checks existence against the provider's model-list endpoint
     (OpenAI /v1/models, Anthropic /v1/models, OpenRouter /api/v1/models,
      Cerebras /v1/models, Bedrock's OpenAI-compatible /openai/v1/models);
  2. runs one minimal forced-tool-call probe (the conch_probe pattern:
     tiny request, tool_choice-forced single tool, capped output tokens,
     one retry on transient errors) to prove native tool calling.

API keys are used by reference only (the env vars in
conch.providers.DEFAULT_API_KEY_ENVS); values are never printed. A provider
without a key is skipped cleanly and reported as unverifiable.

Usage:
    python tools/audit_models.py                     # audit everything
    python tools/audit_models.py --provider openai   # one provider
    python tools/audit_models.py --prune-suggestions # what to remove

Exit status: 0 when every keyed catalog entry passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conch.providers import (  # noqa: E402
    DEFAULT_API_KEY_ENVS,
    DEFAULT_CHAT_MODEL_BY_PROVIDER,
    KNOWN_MODELS,
    anthropic_forced_tool_choice_supported,
    build_openai_chat_request_body,
    get_bedrock_base_url,
)

CLOUD_PROVIDERS = ["cerebras", "bedrock", "openrouter", "openai", "anthropic"]
PROBE_TOOL = "conch_probe"
PROBE_TIMEOUT = 20.0
LIST_TIMEOUT = 10.0
INTER_PROBE_DELAY = 0.5

PROBE_TOOL_SCHEMA = {
    "name": PROBE_TOOL,
    "description": "Verify native tool-call support.",
    "parameters": {
        "type": "object",
        "properties": {"token": {"type": "string", "enum": ["conch-ok"]}},
        "required": ["token"],
        "additionalProperties": False,
    },
}
PROBE_USER_MSG = (
    "Call conch_probe with token conch-ok. Do not answer in text."
)


def _key_for(provider: str) -> str:
    return os.environ.get(DEFAULT_API_KEY_ENVS.get(provider, ""), "").strip()


def _get_json(url: str, headers: dict, timeout: float = LIST_TIMEOUT):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _post_json(url: str, headers: dict, body: dict, timeout: float):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _bearer(provider: str) -> dict:
    return {
        "Authorization": f"Bearer {_key_for(provider)}",
        "User-Agent": "conch-audit/1.0",
    }


def list_models(provider: str) -> "list[str] | None":
    """Return live model ids for *provider*, or None when the endpoint
    can't answer (list failures are non-fatal: the probe still decides)."""
    try:
        if provider == "openai":
            data = _get_json("https://api.openai.com/v1/models", _bearer(provider))
        elif provider == "cerebras":
            data = _get_json("https://api.cerebras.ai/v1/models", _bearer(provider))
        elif provider == "openrouter":
            data = _get_json(
                "https://openrouter.ai/api/v1/models", _bearer(provider)
            )
        elif provider == "bedrock":
            data = _get_json(
                f"{get_bedrock_base_url()}/models", _bearer(provider)
            )
        elif provider == "anthropic":
            data = _get_json(
                "https://api.anthropic.com/v1/models?limit=1000",
                {
                    "x-api-key": _key_for(provider),
                    "anthropic-version": "2023-06-01",
                },
            )
        else:
            return None
    except Exception:
        return None
    items = data.get("data", []) if isinstance(data, dict) else []
    ids = [str(item.get("id", "")).strip() for item in items if isinstance(item, dict)]
    return [model_id for model_id in ids if model_id] or None


def _probe_body(provider: str, model: str) -> "tuple[str, dict, dict]":
    """Return (url, headers, body) for one forced-tool-call probe."""
    if provider == "anthropic":
        return (
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": _key_for(provider),
                "anthropic-version": "2023-06-01",
            },
            {
                "model": model,
                # Adaptive-thinking models (Opus 5.5 family) may spend
                # tokens thinking before the tool call; 64 is enough for
                # forced-choice models but starves them.
                "max_tokens": (
                    64 if anthropic_forced_tool_choice_supported(model) else 512
                ),
                "messages": [{"role": "user", "content": PROBE_USER_MSG}],
                "tools": [
                    {
                        "name": PROBE_TOOL,
                        "description": PROBE_TOOL_SCHEMA["description"],
                        "input_schema": PROBE_TOOL_SCHEMA["parameters"],
                    }
                ],
                # Opus 5.5 / Fable 5.1 reject forced tool_choice ("tool" and
                # "any" both 400, verified live 2026-09-23); "auto" plus the
                # strict tool_use extraction below keeps the probe honest.
                "tool_choice": (
                    {"type": "tool", "name": PROBE_TOOL}
                    if anthropic_forced_tool_choice_supported(model)
                    else {"type": "auto"}
                ),
            },
        )
    urls = {
        "openai": "https://api.openai.com/v1/chat/completions",
        "cerebras": "https://api.cerebras.ai/v1/chat/completions",
        "openrouter": "https://openrouter.ai/api/v1/chat/completions",
        "bedrock": f"{get_bedrock_base_url()}/chat/completions",
    }
    messages = [{"role": "user", "content": PROBE_USER_MSG}]
    tools = [{"type": "function", "function": PROBE_TOOL_SCHEMA}]
    if provider == "openai":
        # Mirror conch's real request builder so quirks it handles
        # (o-series params, gpt-5.6 reasoning_effort) are exercised.
        # 2048 leaves room for reasoning tokens while capping spend.
        body = build_openai_chat_request_body(
            model, messages, temperature=0,
            max_completion_tokens=2048, tools=tools,
        )
    else:
        body = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "temperature": 0,
            # Thinking models (kimi-k2-thinking) burn tokens before the
            # tool call; too small a cap yields false "no tool call".
            "max_tokens": 4096,
        }
    # One tool offered → "required" is forced and broadly compatible.
    body["tool_choice"] = "required"
    return urls[provider], _bearer(provider), body


def _extract_tool_ok(provider: str, payload: dict) -> "tuple[bool, str]":
    if provider == "anthropic":
        for block in payload.get("content") or []:
            if block.get("type") == "tool_use" and block.get("name") == PROBE_TOOL:
                if (block.get("input") or {}).get("token") == "conch-ok":
                    return True, ""
                return True, "tool called (arguments differ)"
        return False, "no native tool_use block returned"
    message = (payload.get("choices") or [{}])[0].get("message", {})
    for call in message.get("tool_calls") or []:
        fn = call.get("function", {}) if isinstance(call, dict) else {}
        if fn.get("name") != PROBE_TOOL:
            continue
        arguments = fn.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if isinstance(arguments, dict) and arguments.get("token") == "conch-ok":
            return True, ""
        return True, "tool called (arguments differ)"
    return False, "no native tool call returned"


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        err = json.loads(raw).get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"HTTP {exc.code}: {str(err['message'])[:160]}"
    except Exception:
        pass
    return f"HTTP {exc.code}"


def probe_model(provider: str, model: str) -> "tuple[bool, str]":
    """One forced-tool-call probe with a single retry on transient errors."""
    url, headers, body = _probe_body(provider, model)
    last_reason = ""
    for attempt in range(2):
        try:
            payload = _post_json(url, headers, body, PROBE_TIMEOUT)
            return _extract_tool_ok(provider, payload)
        except urllib.error.HTTPError as exc:
            last_reason = _http_error_detail(exc)
            if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(2.0)
                continue
            return False, last_reason
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(2.0)
                continue
    return False, last_reason


def audit_provider(provider: str) -> "list[dict]":
    """Audit one provider's catalog; returns one row per model."""
    rows = []
    models = KNOWN_MODELS.get(provider, [])
    if not _key_for(provider):
        key_env = DEFAULT_API_KEY_ENVS.get(provider, "")
        for model in models:
            rows.append(
                {
                    "provider": provider,
                    "model": model,
                    "exists": "no-key",
                    "tools": "no-key",
                    "verdict": "unverifiable",
                    "why": f"{key_env} not set",
                }
            )
        return rows
    live = list_models(provider)
    for model in models:
        exists = "unknown" if live is None else ("yes" if model in live else "NO")
        # Absence from the list endpoint is advisory: aliases (e.g.
        # Anthropic's claude-haiku-4-5 for the dated ID) resolve on the
        # completion endpoint without being listed. The probe decides.
        ok, reason = probe_model(provider, model)
        time.sleep(INTER_PROBE_DELAY)
        if ok:
            verdict = "pass"
            why = reason or "forced tool call verified"
            if exists == "NO":
                exists = "alias"
                why += " (unlisted alias)"
        else:
            verdict = "PRUNE"
            why = reason
            if exists == "NO":
                why = f"absent from live model list; probe: {reason}"
        rows.append(
            {
                "provider": provider,
                "model": model,
                "exists": exists,
                "tools": "yes" if ok else "NO",
                "verdict": verdict,
                "why": why,
            }
        )
    return rows


def print_table(rows: "list[dict]") -> None:
    headers = ["provider", "model", "exists", "tools", "verdict", "why"]
    widths = {
        header: max(len(header), *(len(str(row[header])) for row in rows))
        for header in headers
    } if rows else {header: len(header) for header in headers}
    line = "  ".join(header.ljust(widths[header]) for header in headers)
    print(line)
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print("  ".join(str(row[header]).ljust(widths[header]) for header in headers))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--provider",
        choices=CLOUD_PROVIDERS,
        help="audit only this provider",
    )
    parser.add_argument(
        "--prune-suggestions",
        action="store_true",
        help="print the catalog entries that failed and should be removed",
    )
    args = parser.parse_args(argv)

    providers = [args.provider] if args.provider else CLOUD_PROVIDERS
    rows: list[dict] = []
    for provider in providers:
        rows.extend(audit_provider(provider))

    print_table(rows)

    failures = [row for row in rows if row["verdict"] == "PRUNE"]
    unverifiable = [row for row in rows if row["verdict"] == "unverifiable"]

    for provider in providers:
        default = DEFAULT_CHAT_MODEL_BY_PROVIDER.get(provider, "")
        bad = any(
            row["model"] == default and row["verdict"] == "PRUNE"
            for row in rows
            if row["provider"] == provider
        )
        if bad:
            print(f"\nWARNING: default model for {provider} ('{default}') failed the audit")

    if args.prune_suggestions:
        print()
        if failures:
            print("Prune these catalog entries (failed existence or tool probe):")
            for row in failures:
                print(f"  {row['provider']}: {row['model']}  ({row['why']})")
        else:
            print("No prune suggestions: every keyed catalog entry passed.")
        if unverifiable:
            print("Unverifiable (no key — do not prune blindly):")
            for row in unverifiable:
                print(f"  {row['provider']}: {row['model']}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
