"""Seat-backed providers: native clients generate; Conch alone executes tools."""

from __future__ import annotations

import json
import math
import uuid

from .seat_common import SeatError

PROVIDERS = ("claude-code", "codex")
DEFAULT_MODELS = {"claude-code": "sonnet", "codex": "default"}


def transport(provider: str):
    if provider == "claude-code":
        from . import seat_claude
        return seat_claude
    if provider == "codex":
        from . import seat_codex
        return seat_codex
    raise SeatError(f"unknown subscription provider {provider!r}")


def response_schema(tools) -> dict:
    names = [tool["function"]["name"] for tool in tools or []]
    name_schema = {"type": "string"}
    if names:
        name_schema["enum"] = names
    calls = {
        "type": "array",
        "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": name_schema,
                "arguments": {"type": "string", "description": "Arguments encoded as a JSON object"},
            },
            "required": ["name", "arguments"],
        },
    }
    if not names:
        calls["maxItems"] = 0
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"content": {"type": "string"}, "tool_calls": calls},
        "required": ["content", "tool_calls"],
    }


def _reject_constant(value: str):
    raise ValueError("non-JSON numeric constant")


def normalize(response: dict, tools) -> dict:
    """Fail closed on unknown tools or invalid argument shapes before dispatch."""
    if not isinstance(response, dict) or not isinstance(response.get("content"), str):
        raise SeatError("native client returned an invalid structured response")
    calls = response.get("tool_calls")
    if not isinstance(calls, list):
        raise SeatError("native client returned invalid tool calls")
    names = {tool["function"]["name"] for tool in tools or []}
    normalized = []
    for call in calls:
        if not isinstance(call, dict) or call.get("name") not in names:
            raise SeatError("native client requested an unavailable tool")
        arguments = call.get("arguments")
        if not isinstance(arguments, str):
            raise SeatError("native client returned invalid tool arguments")
        try:
            parsed = json.loads(arguments, parse_constant=_reject_constant)
        except (TypeError, ValueError) as exc:
            raise SeatError("native client returned invalid JSON tool arguments") from exc
        if not isinstance(parsed, dict):
            raise SeatError("native client tool arguments must be a JSON object")
        normalized.append({
            "id": "seat_" + uuid.uuid4().hex, "type": "function",
            "function": {"name": call["name"], "arguments": arguments},
        })
    if not response["content"].strip() and not normalized:
        raise SeatError("native client returned an empty response")
    return {"role": "assistant", "content": response["content"],
            "tool_calls": normalized or None}


def complete(config: dict, messages: list, tools=None, on_token=None) -> dict:
    from .providers import error_response
    from .render import emit_generation_detail

    provider = config.get("provider", "")
    model = config.get("chat_model") or config.get("model") or DEFAULT_MODELS.get(provider, "")
    try:
        timeout = float(config.get("seat_timeout", 120))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return error_response("seat_timeout must be a positive finite number")
    prompt = (
        "You are the inference step of Conch. Follow the system instructions in the "
        "conversation below. Return only the required structured response. To take an "
        "action, return a tool_calls entry naming an available Conch tool with arguments "
        "encoded as a JSON object string. Conch executes it under its own approval policy "
        "and supplies the result on the next request. Never execute tools yourself. "
        "Use an empty tool_calls array for a final answer.\n"
        + json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False)
    )
    try:
        on_detail = None
        if on_token is not None:
            def on_detail(kind, text):
                emit_generation_detail(on_token, kind, text)
        result = transport(provider).invoke(
            model, prompt, response_schema(tools), timeout, on_detail=on_detail)
        reply = normalize(result["response"], tools)
    except SeatError as exc:
        return error_response(f"{provider}: {exc}")
    reply["_usage"] = result.get("usage") or {"input_tokens": 0, "output_tokens": 0}
    reply["_model"] = result.get("model") or model
    reply["_provider"] = provider
    # Reasoning/progress use the separate detail channel. Only the validated
    # final content is delivered here, exactly once and never as raw JSON.
    if on_token and reply["content"] and not reply["tool_calls"]:
        on_token(reply["content"])
    return reply


def status(provider: str, timeout: float = 10) -> tuple:
    try:
        return transport(provider).status(timeout)
    except SeatError as exc:
        return False, str(exc)
