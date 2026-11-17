"""Seat-backed providers: native clients generate; Conch alone executes tools."""

from __future__ import annotations

import json
import math
import sys
import uuid

from .images import has_images, is_image_block, text_only
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


_PRIMITIVES = ("string", "integer", "number", "boolean")
_STRING_ARGUMENTS = {"type": "string", "description": "Arguments encoded as a JSON object"}


def _strict_parameters(schema, depth: int = 0):
    """A tool's parameter schema in the strict structured-output subset both
    native clients enforce while generating, or None when it does not fit.

    Strict objects list every property as required, so an optional property
    becomes nullable; :func:`normalize` drops those nulls again. Value
    constraints (ranges, patterns, defaults) are left to Conch's own
    argument validation before dispatch.
    """
    if not isinstance(schema, dict) or depth > 4:
        return None
    kind = schema.get("type")
    strict = {}
    if isinstance(schema.get("description"), str):
        strict["description"] = schema["description"]
    if kind in _PRIMITIVES:
        strict["type"] = kind
        if isinstance(schema.get("enum"), list):
            strict["enum"] = list(schema["enum"])
        return strict
    if kind == "array":
        items = _strict_parameters(schema.get("items"), depth + 1)
        if items is None:
            return None
        strict.update(type="array", items=items)
        return strict
    properties = schema.get("properties")
    if (kind != "object" or not isinstance(properties, dict)
            # A nested object without declared properties is free-form.
            or (depth and not properties)
            or schema.get("additionalProperties") not in (None, False)
            or any(key in schema for key in ("anyOf", "oneOf", "allOf", "patternProperties"))):
        return None
    required = set(schema.get("required") or ())
    converted = {}
    for name, item in properties.items():
        item = _strict_parameters(item, depth + 1)
        if item is None:
            return None
        if name not in required:
            item["type"] = [item["type"], "null"]
            if "enum" in item:
                item["enum"].append(None)
        converted[name] = item
    strict.update(type="object", properties=converted, required=list(converted),
                  additionalProperties=False)
    return strict


def _call_schema(names, arguments) -> dict:
    name = {"type": "string"}
    if names:
        name["enum"] = names
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"name": name, "arguments": arguments},
        "required": ["name", "arguments"],
    }


def response_schema(tools) -> dict:
    """Response envelope: text plus tool calls whose arguments are objects.

    A model writing arguments as JSON *inside a JSON string* regularly
    mis-escapes quotes in long shell commands, and that string is outside the
    client's schema enforcement. Typing each tool's arguments as an object
    puts them under it. Tools whose parameters do not fit the strict subset
    keep the string form.
    """
    variants, untyped = [], []
    for tool in tools or []:
        function = tool["function"]
        arguments = _strict_parameters(function.get("parameters"))
        if arguments is None:
            untyped.append(function["name"])
        else:
            variants.append(_call_schema([function["name"]], arguments))
    if untyped:
        variants.append(_call_schema(untyped, dict(_STRING_ARGUMENTS)))
    if variants:
        calls = {"type": "array", "items": {"anyOf": variants}}
    else:
        calls = {"type": "array", "maxItems": 0,
                 "items": _call_schema(None, dict(_STRING_ARGUMENTS))}
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"content": {"type": "string"}, "tool_calls": calls},
        "required": ["content", "tool_calls"],
    }


def _without_unused_optionals(value, schema):
    """Drop the nulls strict generation supplies for optional properties."""
    if not isinstance(schema, dict):
        return value
    if isinstance(value, list):
        return [_without_unused_optionals(item, schema.get("items")) for item in value]
    if not isinstance(value, dict):
        return value
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    return {
        key: _without_unused_optionals(item, properties.get(key))
        for key, item in value.items()
        if item is not None or key in required or key not in properties
    }


def normalize(response: dict, tools) -> dict:
    """Turn the envelope into Conch tool calls; unknown tools fail closed.

    Arguments are not judged here: object arguments are serialized, string
    arguments pass through unparsed, and Conch's argument validation rejects
    a bad one before dispatch with a result the model can correct, instead
    of this adapter failing the whole turn.
    """
    if not isinstance(response, dict) or not isinstance(response.get("content"), str):
        raise SeatError("native client returned an invalid structured response")
    calls = response.get("tool_calls")
    if not isinstance(calls, list):
        raise SeatError("native client returned invalid tool calls")
    parameters = {tool["function"]["name"]: tool["function"].get("parameters")
                  for tool in tools or []}
    normalized = []
    for call in calls:
        if not isinstance(call, dict) or call.get("name") not in parameters:
            raise SeatError("native client requested an unavailable tool")
        arguments = call.get("arguments")
        if isinstance(arguments, dict):
            try:
                arguments = json.dumps(
                    _without_unused_optionals(arguments, parameters[call["name"]]),
                    ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise SeatError("native client returned invalid tool arguments") from exc
        elif not isinstance(arguments, str):
            raise SeatError("native client returned invalid tool arguments")
        normalized.append({
            "id": "seat_" + uuid.uuid4().hex, "type": "function",
            "function": {"name": call["name"], "arguments": arguments},
        })
    if not response["content"].strip() and not normalized:
        raise SeatError("native client returned an empty response")
    return {"role": "assistant", "content": response["content"],
            "tool_calls": normalized or None}


SEAT_IMAGE_NOTE = "not sent: subscription seats take text only"
# Images already warned about this process, so a tool loop re-sending the
# same history doesn't repeat the warning on every model call.
_warned_images: set = set()


def _seat_messages(provider: str, messages: list) -> list:
    """Seats receive a text prompt: images become labeled placeholders, and
    the user is told once (stderr) when a newly attached image is dropped."""
    if not has_images(messages):
        return messages
    fresh = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not is_image_block(block):
                continue
            data = (block.get("source") or {}).get("data") or ""
            key = (block.get("name"), len(data), data[:64])
            if key not in _warned_images:
                _warned_images.add(key)
                fresh.append(block.get("name") or "image")
    if fresh:
        print(
            f"  \033[33m⚠ {provider}: {', '.join(fresh)} not sent — "
            "subscription seats take text only\033[0m",
            file=sys.stderr,
        )
    return text_only(messages, SEAT_IMAGE_NOTE)


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
        "action, return a tool_calls entry naming an available Conch tool with its "
        "arguments; give null for an optional argument you are not using. Conch executes "
        "it under its own approval policy and supplies the result on the next request. "
        "Never execute tools yourself. Text alone does nothing: when you say you will "
        "do something, include its tool_calls entry in this same response. Use an empty "
        "tool_calls array only for a final answer.\n"
        + json.dumps({"messages": _seat_messages(provider, messages), "tools": tools or []},
                     ensure_ascii=False)
    )
    try:
        on_detail = None
        if on_token is not None:
            def on_detail(kind, text):
                emit_generation_detail(on_token, kind, text)
        from .model_tuning import effort_for

        # Effort chosen for this model in the picker; omitted when unset so
        # the CLI keeps its own default.
        effort = effort_for(config, provider, model)
        extra = {"effort": effort} if effort else {}
        result = transport(provider).invoke(
            model, prompt, response_schema(tools), timeout, on_detail=on_detail, **extra)
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
