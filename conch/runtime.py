"""Chat runtime helpers."""

from __future__ import annotations

import contextlib
import functools
import math
import re
import json
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from . import mcp as mcp_mod
from .render import Spinner, StreamPrinter, clear_active_spinners


# ---------------------------------------------------------------------------
# Tool-execution visibility helpers
# ---------------------------------------------------------------------------

_verbose_tools = True


def set_verbose_tools(enabled: bool):
    global _verbose_tools
    _verbose_tools = enabled


def get_verbose_tools() -> bool:
    return _verbose_tools

def _summarize_args(arguments: dict) -> str:
    """One-line summary of tool arguments for display."""
    if not arguments:
        return ""
    if "command" in arguments and len(arguments) == 1 or (
        len(arguments) == 2 and "timeout" in arguments
    ):
        cmd = str(arguments["command"])
        cmd = cmd.replace("\n", " \u23ce ")
        if len(cmd) > 79:
            cmd = cmd[:79] + "\u2026"
        return cmd
    raw = json.dumps(arguments, ensure_ascii=False)
    raw = raw.replace("\n", " \u23ce ")
    if len(raw) > 79:
        raw = raw[:79] + "\u2026"
    return raw


def _summarize_result(
    text: str, *, max_lines: int = 6, max_chars: int = 400
) -> str:
    """Compact summary of a tool result for the user."""
    if not text:
        return "(empty)"
    lines = text.split("\n")
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        remaining = len(lines) - max_lines
        kept.append(f"  ... ({remaining} more lines)")
        text = "\n".join(kept)
    if len(text) > max_chars:
        text = text[:max_chars] + "\u2026"
    return text


def _print_tool_preview(
    name: str, arguments: dict, *, verbose: bool = True
) -> None:
    """Print tool name (and optionally args) to stderr."""
    clear_active_spinners()
    summary = _summarize_args(arguments)
    if verbose and summary:
        print(f"  \033[2m\u26a1 {name}\033[0m \033[2m{summary}\033[0m", file=sys.stderr)
    else:
        print(f"  \033[2m\u26a1 {name}\033[0m", file=sys.stderr)


def _print_tool_result(
    text: str, *, verbose: bool = True, error: bool = False
) -> None:
    """Print a summarized tool result to stderr when verbose or on errors."""
    if error:
        summary = _summarize_result(text, max_lines=3, max_chars=200)
        print(f"    \033[31m\u2717 {summary}\033[0m", file=sys.stderr)
        return
    if not verbose:
        return
    summary = _summarize_result(text)
    for line in summary.split("\n"):
        print(f"    \033[2m{line}\033[0m", file=sys.stderr)


# ---------------------------------------------------------------------------
# Provider error signaling (single [API error: ...] prefix + _error flag)
# ---------------------------------------------------------------------------

_TRANSIENT_ERROR_MARKERS = (
    # rate limits / server-side blips
    "429", "500", "502", "503", "504", "overloaded", "rate",
    # network-level failures (dead/unreachable server, DNS, timeouts)
    "connection refused", "connection reset", "timed out", "timeout",
    "unreachable", "getaddrinfo", "name or service not known",
    # missing model on the server (404) — recoverable via fallback chain
    "404",
)

_STRUCTURAL_ERROR_MARKERS = (
    "invalid_request", "invalid request",
    "authentication", "invalid api key", "invalid x-api-key",
)

_CONNECTION_ERROR_MARKERS = (
    "connection refused", "connection reset", "timed out", "timeout",
    "unreachable", "getaddrinfo", "name or service not known", "errno 61",
    "errno 111",
)


def is_error_response(response: dict) -> bool:
    """True when a provider result signals failure (structured flag first,
    content prefix as fallback for anything that missed the helper)."""
    if response.get("_error"):
        return True
    content = response.get("content", "")
    return isinstance(content, str) and content.startswith("[API error:")


def error_detail(response: dict) -> str:
    """Human-readable error message from a failed provider result."""
    content = response.get("content", "")
    if not isinstance(content, str):
        return str(content)
    if content.startswith("[API error: ") and content.endswith("]"):
        return content[len("[API error: "):-1]
    return content


def is_transient_error(message: str) -> bool:
    lower = message.lower()
    return any(marker in lower for marker in _TRANSIENT_ERROR_MARKERS)


def is_structural_error(message: str) -> bool:
    lower = message.lower()
    return any(marker in lower for marker in _STRUCTURAL_ERROR_MARKERS)


def is_connection_error(message: str) -> bool:
    lower = message.lower()
    return any(marker in lower for marker in _CONNECTION_ERROR_MARKERS)


CHARS_PER_TOKEN = 3.5  # default until calibrated against real usage counts
# Legacy fallback limits, used only when no config is available to look up the
# active model's real context window (see get_context_limit).
CONTEXT_LIMITS = {
    "openai": 120000,
    "anthropic": 180000,
    "ollama": 28000,
}

# Running (chars sent, prompt tokens reported) totals, isolated per
# provider/model so switching tokenizers cannot poison later estimates.
_token_calibration: Dict[str, dict] = {}
_CALIBRATION_MIN_TOKENS = 200  # don't trust tiny samples
_CALIBRATION_CLAMP = (1.5, 8.0)


def calibration_key(provider: str, config: Optional[dict] = None) -> str:
    model = (config or {}).get(
        "chat_model", (config or {}).get("model", "")
    )
    return f"{(provider or '').lower()}:{model}" if provider else "default"


def record_token_calibration(
    char_count: int, token_count: int, key: str = "default"
) -> None:
    if char_count <= 0 or token_count <= 0:
        return
    bucket = _token_calibration.setdefault(
        key, {"chars": 0.0, "tokens": 0}
    )
    bucket["chars"] += char_count
    bucket["tokens"] += token_count


def reset_token_calibration(key: Optional[str] = None) -> None:
    if key is None:
        _token_calibration.clear()
    else:
        _token_calibration.pop(key, None)


def get_chars_per_token(key: str = "default") -> float:
    bucket = _token_calibration.get(key, {"chars": 0.0, "tokens": 0})
    if bucket["tokens"] >= _CALIBRATION_MIN_TOKENS:
        ratio = bucket["chars"] / bucket["tokens"]
        low, high = _CALIBRATION_CLAMP
        return max(low, min(high, ratio))
    return CHARS_PER_TOKEN


def get_context_limit(provider: str, config: Optional[dict] = None) -> int:
    """Token budget for the conversation: the active model's real context
    window (per providers.get_context_window) minus ~10% headroom for the
    model's reply. Falls back to CONTEXT_LIMITS when config is missing."""
    if config is None:
        return CONTEXT_LIMITS.get(provider, 120000)
    from .providers import get_context_window
    model = config.get("chat_model", config.get("model", ""))
    window = get_context_window(provider, model, config)
    return max(1, int(window * 0.9))


def char_count(messages: List[dict], tools: Optional[List[dict]] = None) -> int:
    # Count the complete wire representation. Tool-call names/arguments and
    # role/ID framing consume context too; counting only message.content
    # substantially underestimates long agent loops.
    total = sum(
        len(json.dumps(message, ensure_ascii=False, default=str))
        for message in messages
    )
    if tools:
        total += len(json.dumps(tools, ensure_ascii=False, default=str))
    return total


def estimate_tokens(
    messages: List[dict],
    tools: Optional[List[dict]] = None,
    *,
    key: str = "default",
) -> int:
    return int(char_count(messages, tools) / get_chars_per_token(key))


def format_context_gauge(used_tokens: int, window: int) -> str:
    """Compact colored context-usage gauge for the per-turn usage line."""
    if window <= 0:
        return ""
    pct = used_tokens / window * 100
    color = "\033[31m" if pct >= 80 else "\033[33m" if pct >= 60 else "\033[32m"
    return f"ctx {color}{pct:.0f}%\033[0m"


# ---------------------------------------------------------------------------
# Tool-result truncation: budget scaled to the context window (plan 1.5)
# ---------------------------------------------------------------------------

TOOL_RESULT_BUDGET_FRACTION = 0.10  # ≤10% of the context budget per result
_TOOL_RESULT_MIN_TOKENS = 500


def truncate_middle(text: str, budget_chars: int) -> str:
    """Cap *text* to ~budget_chars keeping head and tail (the useful parts of
    command output are usually at both ends)."""
    if budget_chars <= 0 or len(text) <= budget_chars:
        return text
    marker = "\n... [truncated — head and tail kept] ...\n"
    if budget_chars <= len(marker) + 20:
        return text[:budget_chars]
    usable = budget_chars - len(marker)
    head = int(usable * 0.67)
    tail = max(0, usable - head)
    omitted = len(text) - head - tail
    marker = (
        f"\n... [truncated {omitted:,} chars — head and tail kept] ...\n"
    )
    # The digit count changes marker length, so recalculate once.
    usable = max(1, budget_chars - len(marker))
    head = int(usable * 0.67)
    tail = max(0, usable - head)
    omitted = len(text) - head - tail
    marker = (
        f"\n... [truncated {omitted:,} chars — head and tail kept] ...\n"
    )
    if head + tail + len(marker) > budget_chars:
        tail = max(0, budget_chars - head - len(marker))
    return (
        text[:head]
        + marker
        + (text[-tail:] if tail else "")
    )


def tool_result_char_budget(provider: str, config: Optional[dict] = None) -> int:
    """Char budget for a single tool result, scaled to the model's window."""
    limit = get_context_limit(provider, config)
    tokens = max(_TOOL_RESULT_MIN_TOKENS, int(limit * TOOL_RESULT_BUDGET_FRACTION))
    return int(
        tokens * get_chars_per_token(calibration_key(provider, config))
    )


def truncate_tool_result(text: str, provider: str, config: Optional[dict] = None) -> str:
    return truncate_middle(text, tool_result_char_budget(provider, config))


# ---------------------------------------------------------------------------
# Model-generated compaction (plan 1.4)
# ---------------------------------------------------------------------------

AUTO_COMPACT_THRESHOLD = 0.7  # of the context budget
AUTO_COMPACT_KEEP_RECENT = 6  # messages kept verbatim

_COMPACT_SYSTEM_PROMPT = (
    "You compress conversation history. Summarize the transcript into a "
    "concise brief that preserves: established facts, decisions made, open "
    "tasks, and important file paths, commands, and results. Use short plain "
    "bullet points. No preamble."
)


def _message_as_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        rendered = []
        for block in content:
            if not isinstance(block, dict):
                rendered.append(str(block))
            elif block.get("type") == "text":
                rendered.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                rendered.append(
                    f"tool call {block.get('name', '')}: "
                    f"{json.dumps(block.get('input', {}), ensure_ascii=False)}"
                )
            elif block.get("type") == "tool_result":
                rendered.append(
                    f"tool result {block.get('tool_use_id', '')}: "
                    f"{block.get('content', '')}"
                )
        content = " ".join(rendered)
    if not isinstance(content, str):
        content = str(content)
    parts = [content.strip()] if content.strip() else []
    for tool_call in message.get("tool_calls") or []:
        fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
        parts.append(
            f"tool call {fn.get('name', '')}: "
            f"{str(fn.get('arguments', '{}'))[:2000]}"
        )
    if message.get("role") == "tool" and message.get("tool_call_id"):
        parts.insert(0, f"tool result {message['tool_call_id']}:")
    return " ".join(part for part in parts if part).strip()


def _history_group_spans(messages: List[dict], start: int = 0) -> List[tuple]:
    """Return half-open spans that never split a tool call from its results."""
    spans: List[tuple] = []
    i = start
    while i < len(messages):
        begin = i
        message = messages[i]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                i += 1
        elif has_anthropic_tool_use(message):
            i += 1
            if i < len(messages) and is_anthropic_tool_result(messages[i]):
                i += 1
        else:
            i += 1
        spans.append((begin, i))
    return spans


def _recent_group_start(
    messages: List[dict], start: int, minimum_messages: int
) -> int:
    spans = _history_group_spans(messages, start)
    if not spans:
        return len(messages)
    count = 0
    recent_start = len(messages)
    for begin, end in reversed(spans):
        recent_start = begin
        count += end - begin
        if count >= minimum_messages:
            break
    return recent_start


def _prepend_history_note(recent: List[dict], text: str) -> List[dict]:
    """Insert a compaction note without creating a second system message."""
    note = f"[Earlier conversation context]\n{text}"
    if recent and recent[0].get("role") == "user" and isinstance(
        recent[0].get("content"), str
    ):
        first = dict(recent[0])
        first["content"] = note + "\n\n" + first.get("content", "")
        return [first] + recent[1:]
    return [{"role": "user", "content": note}] + recent


def auto_compact(
    messages: List[dict],
    tools: Optional[List[dict]],
    provider: str,
    config: Optional[dict],
    raw_fn,
) -> bool:
    """LLM-generated compaction: at ~70% of the context budget, replace older
    history with a single model-written summary, keeping the system prompt
    and the last few messages verbatim.

    Returns True when history was compacted. On any failure (summary call
    errors, empty summary) leaves messages untouched — the cheap char-capping
    compress_context still runs afterwards as a backstop.
    """
    limit = get_context_limit(provider, config)
    key = calibration_key(provider, config)
    if estimate_tokens(
        messages, tools, key=key
    ) < limit * AUTO_COMPACT_THRESHOLD:
        return False
    has_system = bool(messages) and messages[0].get("role") == "system"
    start = 1 if has_system else 0
    cut = _recent_group_start(messages, start, AUTO_COMPACT_KEEP_RECENT)
    if cut - start < 4:
        return False  # too little old history to be worth an LLM call
    old = messages[start:cut]

    lines = []
    for message in old:
        text = _message_as_text(message)
        if not text:
            continue
        role = message.get("role", "")
        role = "tool result" if role == "tool" else role
        lines.append(f"{role}: {text[:2000]}")
    transcript = "\n".join(lines)
    if not transcript.strip():
        return False
    # Compaction is a side task: run it on the weak model when configured
    # (plan 2.7) so the main model's KV cache and VRAM stay untouched.
    summary_fn, summary_config = side_task_fn(config, raw_fn, config)
    if summary_fn is None:
        return False
    summary_provider = str(
        (summary_config or {}).get("provider", provider) or provider
    ).lower()
    summary_limit = get_context_limit(summary_provider, summary_config)
    # Size the transcript for the side model, which may have a much smaller
    # context than the main model.
    transcript = truncate_middle(transcript, int(summary_limit * 2))
    summary_messages = [
        {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
        {"role": "user", "content": transcript},
    ]
    try:
        response = summary_fn(summary_config, summary_messages, None)
    except Exception:
        return False
    if is_error_response(response):
        return False
    summary = (response.get("content") or "").strip()
    if not summary:
        return False

    kept_recent = _prepend_history_note(
        list(messages[cut:]), f"Summary:\n{summary}"
    )
    new_messages = ([messages[0]] if has_system else []) + kept_recent
    messages.clear()
    messages.extend(new_messages)
    return True


def summarize_message(message: dict) -> dict:
    content = message.get("content", "")
    if isinstance(content, str) and len(content) > 500:
        return {**message, "content": content[:200] + "\n...[compressed]...\n" + content[-100:]}
    if isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", block.get("content", ""))
                if isinstance(text, str) and len(text) > 500:
                    block = dict(block)
                    key = "text" if "text" in block else "content"
                    block[key] = text[:200] + "\n...[compressed]...\n" + text[-100:]
            blocks.append(block)
        return {**message, "content": blocks}
    return message


def compress_context(
    messages: List[dict],
    tools: Optional[List[dict]],
    provider: str,
    config: Optional[dict] = None,
) -> List[dict]:
    limit = get_context_limit(provider, config)
    key = calibration_key(provider, config)
    if estimate_tokens(messages, tools, key=key) <= limit:
        return messages
    has_system = bool(messages) and messages[0].get("role") == "system"
    start = 1 if has_system else 0
    system = [messages[0]] if has_system else []
    spans = _history_group_spans(messages, start)
    groups = [
        [summarize_message(message) for message in messages[begin:end]]
        for begin, end in spans
    ]
    dropped_messages = 0

    def candidate() -> List[dict]:
        body = [message for group in groups for message in group]
        if dropped_messages:
            body = _prepend_history_note(
                body,
                f"{dropped_messages} older messages were dropped to fit "
                "the active model context window.",
            )
        return system + body

    compressed = candidate()
    while (
        len(groups) > 1
        and estimate_tokens(compressed, tools, key=key) > limit
    ):
        dropped_messages += len(groups.pop(0))
        compressed = candidate()
    return compressed


def canonical_tool_call(raw: dict, index: int) -> dict:
    """Coerce one tool call into the canonical OpenAI history shape."""
    fn = (raw or {}).get("function")
    if not isinstance(fn, dict):
        fn = {}
    name = fn.get("name") or (raw or {}).get("name") or ""
    if "arguments" in fn:
        args = fn["arguments"]
    elif "arguments" in (raw or {}):
        args = (raw or {})["arguments"]
    else:
        args = "{}"
    if isinstance(args, (dict, list)):
        args = json.dumps(args)
    elif args is None or not isinstance(args, str):
        args = json.dumps(args)
    if not args.strip():
        args = "{}"
    return {
        "id": str((raw or {}).get("id") or f"call_{index}"),
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _existing_tool_call_ids(messages: List[dict]) -> set:
    ids = set()
    for message in messages:
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("id"):
                ids.add(str(tool_call["id"]))
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id")
                ):
                    ids.add(str(block["id"]))
    return ids


def canonicalize_tool_calls(
    raw_tool_calls: Any, messages: Optional[List[dict]] = None
) -> List[dict]:
    """Canonicalize calls once, assigning collision-free IDs before execution."""
    if isinstance(raw_tool_calls, dict):
        raw_tool_calls = [raw_tool_calls]
    if not isinstance(raw_tool_calls, list):
        return []
    used = _existing_tool_call_ids(messages or [])
    canonical: List[dict] = []
    for index, raw in enumerate(raw_tool_calls):
        if not isinstance(raw, dict):
            raw = {"name": "", "arguments": raw}
        tool_call = canonical_tool_call(raw, index)
        tool_id = str(raw.get("id") or "")
        if not tool_id or tool_id in used:
            tool_id = f"call_{uuid.uuid4().hex}"
        tool_call["id"] = tool_id
        used.add(tool_id)
        canonical.append(tool_call)
    return canonical


def append_results_openai(messages: List[dict], response: dict, results: List[dict]):
    assistant_message: Dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
    raw_tool_calls = response.get("tool_calls")
    if raw_tool_calls:
        assistant_message["tool_calls"] = [
            canonical_tool_call(tool_call, index)
            for index, tool_call in enumerate(raw_tool_calls)
            if isinstance(tool_call, dict)
        ]
    messages.append(assistant_message)
    for result in results:
        messages.append({"role": "tool", "tool_call_id": result["id"], "content": result["content"]})


def append_results_anthropic(messages: List[dict], response: dict, results: List[dict]):
    messages.append({
        "role": "assistant",
        "content": response.get("_anthropic_content", [{"type": "text", "text": response.get("content", "")}]),
    })
    messages.append({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": result["id"], "content": result["content"]} for result in results],
    })


def has_anthropic_tool_use(message: dict) -> bool:
    return message.get("role") == "assistant" and isinstance(message.get("content"), list) and any(
        isinstance(block, dict) and block.get("type") == "tool_use" for block in message["content"]
    )


def is_anthropic_tool_result(message: dict) -> bool:
    return message.get("role") == "user" and isinstance(message.get("content"), list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in message["content"]
    )


def sanitize_anthropic_messages(messages: List[dict]):
    if not messages:
        return
    cleaned: List[dict] = []
    i = 0
    if messages[0].get("role") == "system":
        cleaned.append(messages[0])
        i = 1
    while i < len(messages):
        message = messages[i]
        if has_anthropic_tool_use(message):
            if i + 1 < len(messages) and is_anthropic_tool_result(messages[i + 1]):
                cleaned.extend([message, messages[i + 1]])
                i += 2
            else:
                i += 1
            continue
        if is_anthropic_tool_result(message):
            i += 1
            continue
        cleaned.append(message)
        i += 1
    if len(cleaned) != len(messages):
        messages.clear()
        messages.extend(cleaned)


def _tool_call_from_json_payload(payload, idx: int) -> Optional[dict]:
    """Build a tool_use block from a {"name": ..., "arguments": ...} dict.

    Returns None unless the payload looks exactly like a tool call (string
    name, only tool-call keys) so ordinary JSON replies aren't executed.
    """
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        return None
    if not ("arguments" in payload or "parameters" in payload):
        return None
    if not set(payload) <= {"name", "arguments", "parameters", "id"}:
        return None
    tool_input = payload.get("arguments", payload.get("parameters", {}))
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            tool_input = {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    return {
        "type": "tool_use",
        "id": str(payload.get("id") or f"json_tool_call_{idx}"),
        "name": name,
        "input": tool_input,
    }


# Claude-style XML tool calls. Observed live from qwen3.6 against an older
# local Ollama server: when the server drops/ignores the request's `tools`
# array, the model knows tool names only from the system prompt and
# improvises the Anthropic XML syntax it saw in training. Plan 0.5
# deliberately deleted the old loose `<tool_called .../>` regex path; this
# is a *different, real-world-observed* format and is recovered under strict
# guards: full well-formed wrapper required, and (at the chat_turn call
# site) the tool name must match a registered tool — prose is never executed.
_FN_CALLS_RE = re.compile(r"<function_calls>\s*(.*?)\s*</function_calls>", re.DOTALL)
_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL)
_PARAM_RE = re.compile(r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', re.DOTALL)


def _coerce_param_value(raw: str):
    """Parameter values arrive as text; keep strings but un-JSON obvious
    numbers/booleans/objects so schemas with typed params still work."""
    value = raw.strip()
    if value and (value[0] in "{[" or value in ("true", "false", "null")
                  or value.lstrip("-").replace(".", "", 1).isdigit()):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def _extract_claude_xml_blocks(raw: str, known_tools: Optional[set]) -> Optional[List[dict]]:
    if "<function_calls>" not in raw or "<invoke" not in raw:
        return None
    normalized: List[dict] = []
    for wrapper in _FN_CALLS_RE.finditer(raw):
        for invoke in _INVOKE_RE.finditer(wrapper.group(1)):
            name = invoke.group(1).strip()
            if not name:
                continue
            if known_tools is not None and name not in known_tools:
                continue  # never execute calls to tools that don't exist
            arguments = {
                param.group(1).strip(): _coerce_param_value(param.group(2))
                for param in _PARAM_RE.finditer(invoke.group(2))
                if param.group(1).strip()
            }
            normalized.append({
                "type": "tool_use",
                "id": f"xml_fn_call_{len(normalized) + 1}",
                "name": name,
                "input": arguments,
            })
    return normalized or None


def extract_textual_tool_use_blocks(
    text: str, known_tools: Optional[set] = None
) -> Optional[List[dict]]:
    """Guarded recovery of tool calls the serving layer failed to parse into
    structured tool_calls.

    Live testing showed qwen2.5-coder via Ollama emitting calls either as
    bare JSON content ({"name": ..., "arguments": {...}}) or inside
    <tool_call>...</tool_call> tags, and qwen3.6 on an older server emitting
    Claude-style <function_calls><invoke name=...> XML. All paths require a
    well-formed, tool-call-shaped payload; when *known_tools* is given, any
    recovered call naming an unregistered tool is rejected. Quoted prose and
    ordinary JSON answers are never executed. The old loose regex/
    ast.literal_eval paths remain removed (plan 0.5).
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

    xml_blocks = _extract_claude_xml_blocks(raw, known_tools)
    if xml_blocks:
        return xml_blocks

    # Bare JSON tool call: qwen2.5 via Ollama often emits the call as the
    # entire message content — {"name": ..., "arguments": {...}} — with no
    # wrapper tags, and Ollama passes it through unparsed.
    bare = raw
    if bare.startswith("```") and bare.endswith("```"):
        bare = bare.strip("`").strip()
        if "\n" in bare and bare.split("\n", 1)[0].strip().lower() in ("json", ""):
            bare = bare.split("\n", 1)[1].strip()
    if bare.startswith("{") and bare.endswith("}") or bare.startswith("[") and bare.endswith("]"):
        try:
            payload = json.loads(bare)
        except json.JSONDecodeError:
            payload = None
        candidates = payload if isinstance(payload, list) else [payload]
        normalized = []
        for candidate in candidates:
            block = _tool_call_from_json_payload(candidate, len(normalized) + 1)
            if block is None:
                normalized = []
                break
            normalized.append(block)
        if known_tools is not None:
            normalized = [b for b in normalized if b["name"] in known_tools]
        if normalized:
            return normalized

    # Parse qwen2.5/qwen3-style <tool_call>{"name": ..., "arguments": ...}</tool_call>
    # blocks. Qwen models emit these as plain text when the serving layer
    # fails to parse them into structured tool calls.
    if "<tool_call>" in raw:
        normalized = []
        for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", raw, re.DOTALL):
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                continue
            tool_input = payload.get("arguments", payload.get("parameters", {}))
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except json.JSONDecodeError:
                    tool_input = {}
            if not isinstance(tool_input, dict):
                tool_input = {}
            normalized.append({
                "type": "tool_use",
                "id": f"qwen_tool_call_{len(normalized) + 1}",
                "name": name,
                "input": tool_input,
            })
        if known_tools is not None:
            normalized = [b for b in normalized if b["name"] in known_tools]
        if normalized:
            return normalized

    return None



def _ollama_wire_tool_calls(tool_calls: list) -> list:
    """Convert internal OpenAI-shaped tool_calls to Ollama's wire format.

    Ollama's /api/chat expects ``function.arguments`` to be a JSON *object*;
    sending the OpenAI-style JSON string makes the request fail to decode
    (or the model re-reads its own calls as garbage on the next round).
    """
    wire = []
    for tc in tool_calls or []:
        fn = (tc or {}).get("function", {})
        arguments = fn.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        wire.append({
            "function": {"name": fn.get("name", ""), "arguments": arguments},
        })
    return wire


def normalize_messages_for_provider(messages: list, provider: str) -> list:
    """Prepare messages for the target provider.

    For Anthropic: pass through as-is (fix None content).
    For OpenAI-compatible: keep native OpenAI tool messages (role=tool,
    assistant+tool_calls) intact so multi-turn tool use works.  Only
    convert Anthropic-style structured content blocks to plain text.
    For Ollama: additionally convert tool_calls arguments back to JSON
    objects and link tool results via tool_name.
    """
    if provider == "anthropic":
        cleaned = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "tool":
                continue
            if role == "assistant" and msg.get("tool_calls") and not str(msg.get("content", "")).strip():
                continue
            if role == "assistant" and msg.get("tool_calls"):
                cleaned.append({"role": "assistant", "content": msg.get("content", "")})
                continue
            out = dict(msg)
            if out.get("content") is None:
                out["content"] = ""
            cleaned.append(out)
        return cleaned

    normalized = []
    tool_names_by_id: dict = {}
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Keep OpenAI-format tool result messages as-is
        if role == "tool":
            if provider == "ollama":
                out = {"role": "tool", "content": msg.get("content") or ""}
                tool_name = tool_names_by_id.get(msg.get("tool_call_id", ""), "")
                if tool_name:
                    out["tool_name"] = tool_name
                normalized.append(out)
            else:
                normalized.append(msg)
            i += 1
            continue

        # Handle Anthropic-style structured content (list of blocks)
        if isinstance(content, list):
            has_tool = any(
                isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                for b in content
            )
            text_parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text_content = "\n".join(p for p in text_parts if p).strip()

            if has_tool and not text_content:
                # Pure Anthropic tool block — skip it and its tool_result pair
                i += 1
                if i < len(messages):
                    nxt = messages[i]
                    nxt_c = nxt.get("content", "")
                    if isinstance(nxt_c, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in nxt_c
                    ):
                        i += 1
                continue
            elif text_content:
                normalized.append({"role": role, "content": text_content})
                i += 1
                continue
            else:
                i += 1
                continue

        # Keep assistant messages with tool_calls (OpenAI native format)
        if role == "assistant" and msg.get("tool_calls"):
            clean = dict(msg)
            if clean.get("content") is None:
                clean["content"] = ""
            for tc in clean["tool_calls"]:
                fn = (tc or {}).get("function", {})
                if tc.get("id") and fn.get("name"):
                    tool_names_by_id[tc["id"]] = fn["name"]
            if provider == "ollama":
                clean["tool_calls"] = _ollama_wire_tool_calls(clean["tool_calls"])
            normalized.append(clean)
            i += 1
            continue

        # Regular text messages
        if content is None:
            content = ""
        if content.strip():
            normalized.append({"role": role, "content": content})
        i += 1
    if provider in ("ollama", "custom"):
        # Local Jinja templates generally require exactly one leading system
        # role. Convert legacy mid-history system notes into ordinary context
        # rather than sending an invalid role sequence.
        leading_system = None
        body = []
        for message in normalized:
            if message.get("role") != "system":
                body.append(message)
                continue
            text = str(message.get("content") or "")
            if leading_system is None and not body:
                leading_system = {"role": "system", "content": text}
            else:
                body.append({
                    "role": "user",
                    "content": f"[conversation context]\n{text}",
                })
        return ([leading_system] if leading_system else []) + body
    return normalized


def normalize_messages_on_switch(messages: list, new_provider: str):
    """Normalize message history in-place when switching providers mid-conversation.

    Drops tool execution pairs entirely so the new provider gets clean text context
    with no provider-specific artifacts the model might echo back.
    """
    system = messages[0] if messages and messages[0].get("role") == "system" else None
    cleaned = []
    if system:
        cleaned.append(system)

    msgs = messages[1 if system else 0:]
    i = 0
    while i < len(msgs):
        msg = msgs[i]
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Drop OpenAI-style tool-role messages entirely
        if role == "tool":
            i += 1
            continue

        # Handle structured content lists
        if isinstance(content, list):
            has_tool = any(
                isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                for b in content
            )
            if has_tool:
                # Extract only text parts
                text_parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(p for p in text_parts if p).strip()
                if not content:
                    # Skip this message and the following tool_result user message if any
                    i += 1
                    if i < len(msgs):
                        nxt = msgs[i]
                        nxt_content = nxt.get("content", "")
                        if isinstance(nxt_content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in nxt_content
                        ):
                            i += 1
                    continue
            else:
                text_parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(p for p in text_parts if p).strip()
                if not content:
                    i += 1
                    continue

        if content is None:
            content = ""
        # Drop assistant messages that were pure tool calls (no text content)
        if role == "assistant" and msg.get("tool_calls") and not content.strip():
            i += 1
            continue

        if not content.strip():
            i += 1
            continue

        cleaned.append({"role": role, "content": content})
        i += 1

    messages.clear()
    messages.extend(cleaned)


# ---------------------------------------------------------------------------
# Tool-call drift control (few-shot anchor + corrective reset)
# ---------------------------------------------------------------------------
#
# Small local models sometimes fall out of native function-calling and start
# emitting tool calls as *text* (bare JSON, <tool_call> tags, Claude XML,
# fenced json). The guarded recovery in chat_turn executes and re-stores the
# well-formed ones as structured tool_calls, so those replay correctly. The
# corrupting case is a *malformed* or unregistered-tool textual call: recovery
# declines it, it becomes the assistant reply, and it is persisted as plain
# prose. On the next turn the model sees its own "tool call" rendered as
# ordinary text with no structured tool_calls — and imitates it, compounding
# the drift ("once it starts it keeps doing it").
#
# Three defenses, all gated to local providers and all transient (never
# persisted into the saved conversation):
#   1. a tiny few-shot EXEMPLAR of a correct native tool call, prepended to
#      every local request so the model always has a good pattern to copy;
#   2. drift DETECTION (count textual tool calls this session) that escalates
#      to a corrective REMINDER injected into subsequent requests;
#   3. a manual reset (reset_tool_calling / the /resettools command) that
#      scrubs already-persisted textual-tool-call prose from history and forces
#      the reminder on the next turn.

LOCAL_PROVIDERS = ("ollama", "custom")

# After this many textual tool calls in a session, start appending the
# corrective reminder to requests (the exemplar is always present for local
# providers regardless).
DRIFT_REMINDER_THRESHOLD = 2

TOOL_CALL_REMINDER = (
    "Reminder: call tools ONLY through the native function-calling mechanism "
    "(structured tool calls). Do NOT write tool calls as text — no JSON "
    "objects, no <tool_call> tags, no <function_calls> XML, no fenced code "
    "blocks. Either call a tool natively or reply in plain prose."
)

TOOL_CALL_EXEMPLAR_INSTRUCTION = (
    "The following short exchange is a format example. Tool use must be sent "
    "through native structured tool_calls, never written as JSON/XML/text."
)

# Prefixes that mark content as a *textual* tool-call attempt rather than an
# ordinary reply (mirrors providers._TEXTUAL_TOOL_MARKERS).
_TEXTUAL_TOOL_CALL_PREFIXES = ("{", "[", "<tool_call", "<function_calls", "```")


def looks_like_textual_tool_call(text: str) -> bool:
    """True when *text* is (or clearly attempts to be) a tool call written as
    plain text rather than a native structured call.

    Conservative on purpose: a well-formed tool-call-shaped payload always
    counts (via extract_textual_tool_use_blocks with no registry filter), and
    a malformed one counts only when it both starts with a tool-call marker
    and carries name+arguments hints — so ordinary JSON answers or prose that
    merely contains braces are not misread as drift.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if extract_textual_tool_use_blocks(stripped, None):
        return True
    if stripped.startswith("<tool_call") or stripped.startswith("<function_calls"):
        return True
    if stripped.startswith(("{", "[", "```")) and '"name"' in stripped and (
        '"arguments"' in stripped or '"parameters"' in stripped
    ):
        return True
    return False


def build_tool_call_exemplar(provider: str) -> List[dict]:
    """A tiny synthetic user→assistant(native tool_call)→tool→assistant
    exchange demonstrating correct tool-calling, in the normalized wire shape
    for *provider*. Returns [] for non-local providers.

    Kept deliberately small (small local context budgets) and clearly framed
    as an example so it is never mistaken for real conversation history. It is
    only ever added to the per-request message list, never persisted.
    """
    if provider not in LOCAL_PROVIDERS:
        return []
    if provider == "ollama":
        # Ollama wire shape: arguments as a JSON object, results linked by
        # tool_name (see _ollama_wire_tool_calls).
        call = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "local_shell",
                        "arguments": {"command": "printf conch-tool-example"},
                    }
                }
            ],
        }
        result = {
            "role": "tool",
            "content": "conch-tool-example",
            "tool_name": "local_shell",
        }
    else:
        # OpenAI wire shape for custom endpoints: arguments MUST be a JSON
        # string and results link via tool_call_id — strict backends (e.g.
        # Moonshot Kimi behind OpenRouter) 400 on object-shaped arguments.
        call = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "exemplar_call_0",
                "type": "function",
                "function": {
                    "name": "local_shell",
                    "arguments": "{\"command\": \"printf conch-tool-example\"}",
                },
            }],
        }
        result = {
            "role": "tool",
            "content": "conch-tool-example",
            "tool_call_id": "exemplar_call_0",
        }
    return [
        {"role": "user", "content": "(format example) print a marker"},
        call,
        result,
        {"role": "assistant", "content": "(format example) Marker printed."},
    ]


def _leading_system_count(messages: List[dict]) -> int:
    return 1 if messages and messages[0].get("role") == "system" else 0


def _append_leading_system_context(messages: List[dict], text: str) -> None:
    """Keep local chat templates to one leading system message."""
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        first["content"] = str(first.get("content") or "") + "\n\n" + text
        messages[0] = first
    else:
        messages.insert(0, {"role": "system", "content": text})


def apply_tool_call_scaffolding(
    send_messages: List[dict],
    provider: str,
    config: Optional[dict],
    chat_state=None,
    available_tool_names: Optional[set] = None,
) -> List[dict]:
    """Inject the few-shot exemplar (always, for local providers) and — when
    the session is drifting — the corrective reminder into a *copy-safe*
    per-request message list. Never touches persisted history.

    Gated to local providers. The exemplar can be disabled with config
    ``local_tool_exemplar = false``.
    """
    if provider not in LOCAL_PROVIDERS:
        return send_messages
    cfg = config or {}
    exemplar_enabled = str(cfg.get("local_tool_exemplar", "true")).strip().lower() not in (
        "false", "0", "no", "off",
    )
    if exemplar_enabled and (
        available_tool_names is None or "local_shell" in available_tool_names
    ):
        _append_leading_system_context(
            send_messages, TOOL_CALL_EXEMPLAR_INSTRUCTION
        )
        insert_at = _leading_system_count(send_messages)
        exemplar = build_tool_call_exemplar(provider)
        if exemplar:
            send_messages[insert_at:insert_at] = exemplar

    drift = 0
    force = False
    if chat_state is not None:
        drift = int(getattr(chat_state, "textual_tool_calls", 0) or 0)
        force = bool(getattr(chat_state, "force_tool_reminder", False))
    if force or drift >= DRIFT_REMINDER_THRESHOLD:
        _append_leading_system_context(send_messages, TOOL_CALL_REMINDER)
        if chat_state is not None:
            # One-shot force flag: consumed once the reminder is sent.
            chat_state.force_tool_reminder = False
    return send_messages


def note_textual_tool_call(chat_state) -> None:
    """Record that the model emitted a tool call as text (drift signal)."""
    if chat_state is None:
        return
    chat_state.textual_tool_calls = int(
        getattr(chat_state, "textual_tool_calls", 0) or 0
    ) + 1


def reset_tool_calling(messages: List[dict]) -> int:
    """Scrub persisted textual-tool-call prose from *messages* in place so it
    stops reinforcing drift on replay. Structured tool_calls (the correct
    form) are left untouched. Returns the number of messages removed."""
    kept: List[dict] = []
    removed = 0
    for msg in messages:
        if (
            msg.get("role") == "assistant"
            and not msg.get("tool_calls")
            and isinstance(msg.get("content"), str)
            and looks_like_textual_tool_call(msg["content"])
        ):
            removed += 1
            continue
        kept.append(msg)
    if removed:
        messages.clear()
        messages.extend(kept)
    return removed


# ---------------------------------------------------------------------------
# Weak-model side tasks (plan 2.7): run summaries/compaction on a small fast
# model (config: weak_model, optional weak_provider) instead of the chat model.
# ---------------------------------------------------------------------------

def weak_model_config(config: Optional[dict]) -> Optional[dict]:
    """Config copy pointing at the configured weak model, or None."""
    config = config or {}
    weak = str(config.get("weak_model", "") or "").strip()
    if not weak:
        return None
    cfg = dict(config)
    provider = str(config.get("weak_provider", "") or config.get("provider", "") or "").lower()
    from .config import local_only_enabled

    if local_only_enabled(config, config.get("provider", "")) and provider not in (
        "ollama",
        "custom",
    ):
        return None
    cfg["provider"] = provider
    cfg["model"] = weak
    cfg["chat_model"] = weak
    from .providers import validate_model_for_provider

    verified, _ = validate_model_for_provider(provider, weak, cfg)
    if verified is not True:
        return None
    return cfg


def side_task_fn(config: Optional[dict], default_fn=None, default_config: Optional[dict] = None):
    """(raw_fn, config) to use for side tasks (titles, summaries, compaction):
    the weak model when configured, otherwise the provided defaults."""
    cfg = weak_model_config(config)
    if cfg is not None:
        from .providers import RAW_FNS
        fn = RAW_FNS.get(cfg["provider"])
        if fn is not None:
            return fn, cfg
    return default_fn, (default_config if default_config is not None else config)


def _graceful_exhaustion(
    config: dict,
    provider: str,
    raw_fn,
    messages: List[dict],
    total_usage: dict,
    reason: str,
) -> str:
    """Budget exhausted (plan 2.6): ask the model to summarize progress
    instead of returning a bare '[max tool call rounds reached]'."""
    fallback = f"[{reason} reached]"
    if raw_fn is None:
        return fallback
    prompt = (
        f"You've reached the {reason} for this turn and cannot call more "
        "tools. Summarize for the user: what you accomplished, key findings "
        "or partial results, and what remains to be done."
    )
    send = normalize_messages_for_provider(messages, provider)
    send.append({"role": "user", "content": prompt})
    try:
        response = raw_fn(config, send, None)
    except Exception:
        return fallback
    if is_error_response(response):
        return fallback
    usage = response.get("_usage", {})
    total_usage["input_tokens"] += usage.get("input_tokens", 0)
    total_usage["output_tokens"] += usage.get("output_tokens", 0)
    content = (response.get("content") or "").strip()
    if not content:
        return fallback
    return f"{content}\n\n({reason} reached — reply to continue)"


def select_request_tools(
    tools: Optional[List[dict]],
    provider: str,
    messages: List[dict],
    provider_tool_limits: Dict[str, int],
) -> Optional[List[dict]]:
    """Select the exact tools offered to the model for this request."""
    send_tools = list(tools or [])
    tool_limit = provider_tool_limits.get(provider)
    if tool_limit and len(send_tools) > tool_limit:
        from .tooling import select_relevant_tools

        user_text = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
                and isinstance(message.get("content"), str)
            ),
            "",
        )
        send_tools = select_relevant_tools(send_tools, user_text, tool_limit)
    return send_tools or None


def _tool_name(tool: dict) -> str:
    fn = tool.get("function", {}) if isinstance(tool, dict) else {}
    return str(fn.get("name") or "")


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _validate_schema_value(
    value: Any, schema: Any, path: str = "arguments", depth: int = 0
) -> Optional[str]:
    """Small dependency-free JSON Schema validator for tool inputs."""
    if not isinstance(schema, dict) or depth > 16:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return f"{path} contains a non-finite number"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']!r}"
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(alternatives, list) and alternatives:
        errors = [
            _validate_schema_value(value, option, path, depth + 1)
            for option in alternatives
        ]
        if all(error is not None for error in errors):
            return errors[0]
        return None
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    expected_types = [item for item in expected_types if isinstance(item, str)]
    if expected_types and not any(
        _schema_type_matches(value, item) for item in expected_types
    ):
        return f"{path} must be {' or '.join(expected_types)}"
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [key for key in required if key not in value]
            if missing:
                return f"{path} is missing required field(s): {', '.join(missing)}"
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, item in value.items():
                if key in properties:
                    error = _validate_schema_value(
                        item, properties[key], f"{path}.{key}", depth + 1
                    )
                    if error:
                        return error
                elif schema.get("additionalProperties") is False:
                    return f"{path} contains unsupported field: {key}"
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = _validate_schema_value(
                item, schema["items"], f"{path}[{index}]", depth + 1
            )
            if error:
                return error
    return None


def parse_and_validate_tool_arguments(
    tool_call: dict, tool_definition: dict
) -> tuple:
    fn = tool_call.get("function", {})
    raw_arguments = fn.get("arguments")
    if isinstance(raw_arguments, str):
        if len(raw_arguments) > 262144:
            return None, "arguments exceed the 262144-character safety limit"

        def reject_constant(value):
            raise ValueError(f"non-JSON numeric constant {value}")

        def reject_duplicate_keys(pairs):
            parsed = {}
            for key, value in pairs:
                if key in parsed:
                    raise ValueError(f"duplicate object key {key!r}")
                parsed[key] = value
            return parsed

        try:
            arguments = json.loads(
                raw_arguments,
                parse_constant=reject_constant,
                object_pairs_hook=reject_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            detail = getattr(exc, "msg", str(exc))
            return None, f"arguments are not valid JSON: {detail}"
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        return None, "arguments must be a JSON object"
    if not isinstance(arguments, dict):
        return None, "arguments must be a JSON object"
    try:
        normalized = json.dumps(
            arguments, ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError, RecursionError) as exc:
        return None, f"arguments are not valid JSON: {exc}"
    if len(normalized) > 262144:
        return None, "arguments exceed the 262144-character safety limit"
    parameters = (
        tool_definition.get("function", {}).get("parameters", {})
        if isinstance(tool_definition, dict)
        else {}
    )
    error = _validate_schema_value(arguments, parameters)
    return (None, error) if error else (arguments, None)


def _tool_batch_fingerprint(tool_calls: List[dict]) -> str:
    items = []
    for tool_call in tool_calls:
        fn = tool_call.get("function", {})
        raw = fn.get("arguments", "{}")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            raw = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError, ValueError):
            raw = str(raw)
        items.append((str(fn.get("name") or ""), raw))
    return json.dumps(items, ensure_ascii=False)


def _sync_anthropic_tool_call_ids(response: dict, tool_calls: List[dict]) -> None:
    blocks = response.get("_anthropic_content")
    if not isinstance(blocks, list):
        blocks = []
        if response.get("content"):
            blocks.append({"type": "text", "text": response["content"]})
        for tool_call in tool_calls:
            fn = tool_call.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_call["id"],
                    "name": fn.get("name", ""),
                    "input": arguments,
                }
            )
    call_index = 0
    synced = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            if call_index >= len(tool_calls):
                continue
            block = dict(block)
            block["id"] = tool_calls[call_index]["id"]
            call_index += 1
        synced.append(block)
    response["_anthropic_content"] = synced


_AGENT_TURN_LOCK = threading.RLock()


@contextlib.contextmanager
def serialized_agent_execution():
    """Serialize inference/agent state that is still process-global."""
    with _AGENT_TURN_LOCK:
        yield


def _serialized_agent_turn(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        # Conch currently has process-global shell policy, cwd, and runtime
        # clients. Serialize turns until those become explicit per-session
        # capabilities. RLock permits same-thread delegation.
        with serialized_agent_execution():
            return fn(*args, **kwargs)

    return wrapped


@_serialized_agent_turn
def chat_turn(
    config: dict,
    provider: str,
    raw_fn,
    messages: List[dict],
    tools: Optional[List[dict]],
    tool_map: Dict[str, Any],
    builtin_clients: Dict[str, Any],
    max_tool_rounds: int = 25,
    chat_state=None,
    on_token=None,
    input_fn=None,
) -> tuple:
    """Returns (reply_text, usage_info) where usage_info is a dict with
    input_tokens, output_tokens, and model.

    When *on_token* is a callable, the reply is streamed token-by-token
    through that callback instead of blocking behind a spinner.
    """
    from .providers import STREAM_FNS, PROVIDER_TOOL_LIMITS

    total_usage = {"input_tokens": 0, "output_tokens": 0, "model": ""}
    # Optional per-turn token budget alongside the round budget (plan 2.6)
    try:
        token_budget = int(config.get("turn_token_budget", 0) or 0)
    except (TypeError, ValueError):
        token_budget = 0
    try:
        max_identical_batches = max(
            2, int(config.get("max_identical_tool_batches", 3) or 3)
        )
    except (TypeError, ValueError):
        max_identical_batches = 3
    try:
        max_parallel_tool_calls = max(
            1, int(config.get("max_parallel_tool_calls", 16) or 16)
        )
    except (TypeError, ValueError):
        max_parallel_tool_calls = 16
    previous_batch = ""
    identical_batches = 0
    for _round in range(max_tool_rounds):
        if (
            token_budget
            and _round
            and total_usage["input_tokens"] + total_usage["output_tokens"] >= token_budget
        ):
            print(
                f"  \033[33m⚠ Turn token budget ({token_budget:,}) exhausted — "
                f"summarizing progress\033[0m",
                file=sys.stderr,
            )
            return (
                _graceful_exhaustion(config, provider, raw_fn, messages,
                                     total_usage, "token budget"),
                total_usage,
            )
        if provider == "anthropic":
            sanitize_anthropic_messages(messages)
        if chat_state and getattr(chat_state, "needs_tool_refresh", False):
            tools = chat_state.tools
            chat_state.needs_tool_refresh = False
        send_tools = select_request_tools(
            tools, provider, messages, PROVIDER_TOOL_LIMITS
        )
        # Model-generated compaction first (plan 1.4); char-capping
        # compress_context stays as the cheap backstop below.
        if raw_fn is not None:
            try:
                if auto_compact(
                    messages, send_tools, provider, config, raw_fn
                ):
                    print("  \033[2m(older history auto-compacted)\033[0m", file=sys.stderr)
            except Exception:
                pass
        compressed = compress_context(
            messages, send_tools, provider, config
        )
        if len(compressed) < len(messages):
            messages.clear()
            messages.extend(compressed)
            if provider == "anthropic":
                sanitize_anthropic_messages(messages)
        send_messages = normalize_messages_for_provider(messages, provider)

        # Keep model-facing self-knowledge accurate after fallback and after a
        # local server reports its loaded context. This is request-scoped so
        # persisted history remains portable.
        from .prompts import build_self_description

        active_model = config.get(
            "chat_model", config.get("model", "")
        )
        _append_leading_system_context(
            send_messages,
            "[Current runtime state] "
            + build_self_description(
                provider, active_model, config
            ),
        )

        # Few-shot tool-call anchor + corrective reminder for local models
        # (transient: added to the request only, never persisted).
        available_tool_names = {
            _tool_name(tool) for tool in (send_tools or []) if _tool_name(tool)
        }
        send_messages = apply_tool_call_scaffolding(
            send_messages,
            provider,
            config,
            chat_state,
            available_tool_names,
        )

        # Re-inject the plan scratchpad every round (plan 2.5) — it lives
        # outside compactable history so it survives auto-compaction.
        todo_client = (builtin_clients or {}).get("todo_list")
        todo_block = todo_client.render() if hasattr(todo_client, "render") else ""
        if todo_block:
            _append_leading_system_context(send_messages, todo_block)

        stream_fn = STREAM_FNS.get(provider) if on_token else None
        if stream_fn:
            response = stream_fn(config, send_messages, send_tools if send_tools else None, on_token)
        else:
            with Spinner("Thinking"):
                response = raw_fn(config, send_messages, send_tools if send_tools else None)

        usage = response.get("_usage", {})
        total_usage["input_tokens"] += usage.get("input_tokens", 0)
        total_usage["output_tokens"] += usage.get("output_tokens", 0)
        total_usage["model"] = response.get("_model", total_usage["model"])
        # Calibrate char->token estimates against the provider's real prompt
        # token count (Ollama reports prompt_eval_count on every response).
        if usage.get("input_tokens"):
            record_token_calibration(
                char_count(send_messages, send_tools),
                usage["input_tokens"],
                calibration_key(provider, config),
            )
        if is_error_response(response) and on_token is not None:
            sp = getattr(on_token, "__self__", None)
            if isinstance(sp, StreamPrinter):
                sp.end_waiting()
        if is_error_response(response):
            # Retry once on same provider with 1s backoff (transient errors:
            # rate limits, 5xx, connection refused/timeout, missing model)
            if is_transient_error(error_detail(response)):
                print(
                    "  \033[33m\u26a0 Transient error, retrying in "
                    "1s...\033[0m",
                    file=sys.stderr,
                )
                time.sleep(1)
                if stream_fn:
                    response = stream_fn(
                        config, send_messages, send_tools, on_token
                    )
                else:
                    with Spinner("Retrying"):
                        response = raw_fn(config, send_messages, send_tools)
                usage = response.get("_usage", {})
                total_usage["input_tokens"] += usage.get("input_tokens", 0)
                total_usage["output_tokens"] += usage.get("output_tokens", 0)
                total_usage["model"] = response.get(
                    "_model", total_usage["model"]
                )
            if is_error_response(response):
                err_detail = error_detail(response)
                print(f"  \033[33m⚠ {err_detail}\033[0m", file=sys.stderr)
                from .providers import RAW_FNS, DEFAULT_API_KEY_ENVS, get_fallback_chain
                # Structural errors (invalid request shape, auth) won't be fixed
                # by switching to another model on the same provider.
                _structural = is_structural_error(err_detail)
                current_model = config.get("chat_model", config.get("model", ""))
                fallback_chain = get_fallback_chain(provider, current_model, config)
                if _structural:
                    fallback_chain = [
                        (p, m, s) for p, m, s in fallback_chain if p != provider
                    ]
                failed_provider = provider
                failed_model = current_model
                for fb_provider, fb_model, needs_ctx_switch in fallback_chain:
                    fb_fn = RAW_FNS.get(fb_provider)
                    if not fb_fn:
                        continue
                    if needs_ctx_switch and sys.stdin.isatty():
                        print(
                            f"  \033[1;33m⚠ {failed_provider}/{failed_model} failed.\033[0m",
                            file=sys.stderr,
                        )
                        _input = input_fn or input
                        try:
                            answer = _input(
                                f"  \033[1;33mSwitch to {fb_provider}/{fb_model}? [y/N]\033[0m "
                            ).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            answer = ""
                        if answer not in ("y", "yes"):
                            continue
                    else:
                        print(
                            f"  \033[33m⚠ {failed_provider}/{failed_model} failed, trying {fb_provider}/{fb_model}\033[0m",
                            file=sys.stderr,
                        )
                    fb_config = dict(config)
                    fb_config["provider"] = fb_provider
                    fb_config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(fb_provider, "")
                    fb_config["chat_model"] = fb_model
                    fb_config["model"] = fb_model
                    if needs_ctx_switch:
                        normalize_messages_on_switch(messages, fb_provider)
                    fb_messages = normalize_messages_for_provider(messages, fb_provider)
                    fb_tools = select_request_tools(
                        tools, fb_provider, messages, PROVIDER_TOOL_LIMITS
                    )
                    fb_tool_names = {
                        _tool_name(tool)
                        for tool in (fb_tools or [])
                        if _tool_name(tool)
                    }
                    _append_leading_system_context(
                        fb_messages,
                        "[Current runtime state] "
                        + build_self_description(
                            fb_provider, fb_model, fb_config
                        ),
                    )
                    fb_messages = apply_tool_call_scaffolding(
                        fb_messages,
                        fb_provider,
                        fb_config,
                        chat_state,
                        fb_tool_names,
                    )
                    if todo_block:
                        _append_leading_system_context(
                            fb_messages, todo_block
                        )
                    with Spinner(f"Retrying with {fb_provider}/{fb_model}"):
                        response = fb_fn(fb_config, fb_messages, fb_tools)
                    if not is_error_response(response):
                        provider = fb_provider
                        config["provider"] = fb_provider
                        config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(fb_provider, "")
                        config["chat_model"] = fb_model
                        config["model"] = fb_model
                        stream_fn = STREAM_FNS.get(provider) if on_token else None
                        raw_fn = RAW_FNS.get(provider)
                        send_messages = fb_messages
                        send_tools = fb_tools
                        usage = response.get("_usage", {})
                        total_usage["input_tokens"] += usage.get(
                            "input_tokens", 0
                        )
                        total_usage["output_tokens"] += usage.get(
                            "output_tokens", 0
                        )
                        total_usage["model"] = response.get(
                            "_model", total_usage["model"]
                        )
                        if usage.get("input_tokens"):
                            record_token_calibration(
                                char_count(send_messages, send_tools),
                                usage["input_tokens"],
                                calibration_key(provider, config),
                            )
                        break
                    failed_provider, failed_model = fb_provider, fb_model
            if is_error_response(response):
                # Retry and every fallback failed. Never persist the error
                # text: report it and keep the session alive for a retry.
                final_detail = error_detail(response)
                if provider == "ollama" and is_connection_error(final_detail):
                    from .providers import get_ollama_base_url
                    print(
                        f"  \033[31m✗ Ollama server unreachable at "
                        f"{get_ollama_base_url(config)} — check the server, then "
                        f"send your message again\033[0m",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"  \033[31m✗ {provider} request failed: {final_detail}\033[0m\n"
                        f"  \033[2m(nothing saved — send your message again to retry)\033[0m",
                        file=sys.stderr,
                    )
                # Let the caller know the turn failed so it can preflight the
                # backend before the next one (plan 3.3).
                total_usage["error"] = final_detail
                return "", total_usage
        raw_tool_calls = response.get("tool_calls")
        too_many_tool_calls = (
            isinstance(raw_tool_calls, list)
            and len(raw_tool_calls) > max_parallel_tool_calls
        )
        if too_many_tool_calls:
            raw_tool_calls = raw_tool_calls[:max_parallel_tool_calls]
            total_usage["tool_protocol_error"] = (
                f"more than {max_parallel_tool_calls} parallel tool calls"
            )
        tool_calls = canonicalize_tool_calls(raw_tool_calls, messages)
        if tool_calls:
            response["tool_calls"] = tool_calls
            if provider == "anthropic":
                _sync_anthropic_tool_call_ids(response, tool_calls)
        if on_token is not None:
            sp = getattr(on_token, "__self__", None)
            if hasattr(sp, "end_waiting"):
                sp.end_waiting()
        if not tool_calls:
            reply = response.get("content", "")
            if looks_like_textual_tool_call(reply):
                note_textual_tool_call(chat_state)
                total_usage["tool_protocol_error"] = (
                    "model emitted a textual tool call"
                )
                reply = (
                    "The model emitted a textual tool call, so Conch did not "
                    "execute it. Use a verified native tool-calling model and "
                    "check the inference server's chat template."
                )
                print(
                    "  \033[33m⚠ rejected textual tool call; nothing "
                    "executed\033[0m",
                    file=sys.stderr,
                )
            from .tooling import run_hook

            run_hook("on_turn_end", {"reply": reply}, config)
            return reply, total_usage

        fingerprint = _tool_batch_fingerprint(tool_calls)
        if fingerprint == previous_batch:
            identical_batches += 1
        else:
            previous_batch = fingerprint
            identical_batches = 1

        tool_definitions = {
            _tool_name(tool): tool
            for tool in (send_tools or [])
            if _tool_name(tool)
        }
        allowed_tool_names = set(tool_definitions)
        round_result_budget = max(
            256,
            int(
                get_context_limit(provider, config)
                * get_chars_per_token(calibration_key(provider, config))
                * TOOL_RESULT_BUDGET_FRACTION
            ),
        )
        for client in (builtin_clients or {}).values():
            if hasattr(client, "set_result_budget"):
                client.set_result_budget(round_result_budget)
        remaining_result_budget = round_result_budget
        remaining_result_count = len(tool_calls)
        results = []

        def add_result(tool_call: dict, result_text: Any) -> None:
            nonlocal remaining_result_budget, remaining_result_count
            share = max(
                1,
                remaining_result_budget // max(1, remaining_result_count),
            )
            bounded = truncate_middle(str(result_text), share)
            results.append(
                {"id": tool_call["id"], "content": bounded}
            )
            remaining_result_budget = max(
                0, remaining_result_budget - len(bounded)
            )
            remaining_result_count = max(0, remaining_result_count - 1)

        if too_many_tool_calls:
            reason = (
                f"Rejected tool batch: the model exceeded the "
                f"{max_parallel_tool_calls}-call per-round safety limit."
            )
            for tool_call in tool_calls:
                add_result(tool_call, reason)
            if provider == "anthropic":
                append_results_anthropic(messages, response, results)
            else:
                append_results_openai(messages, response, results)
            continue

        if identical_batches >= max_identical_batches:
            reason = (
                "Rejected repeated identical tool-call batch; inspect the "
                "previous results and choose a different next action."
            )
            for tool_call in tool_calls:
                add_result(tool_call, reason)
            if provider == "anthropic":
                append_results_anthropic(messages, response, results)
            else:
                append_results_openai(messages, response, results)
            if identical_batches > max_identical_batches:
                return (
                    "Stopped this turn because the model repeatedly requested "
                    "the same tool calls without making progress.",
                    total_usage,
                )
            continue

        from .tooling import run_hook

        for tool_call in tool_calls:
            fn = tool_call.get("function", {})
            name = str(fn.get("name") or "")
            if name not in allowed_tool_names:
                result_text = (
                    f"Rejected tool call: {name or '(missing name)'} was not "
                    "offered to the model for this request."
                )
                print(
                    f"  \033[33m⚠ blocked unoffered tool "
                    f"{name or '(missing name)'}\033[0m",
                    file=sys.stderr,
                )
                add_result(tool_call, result_text)
                continue
            arguments, argument_error = parse_and_validate_tool_arguments(
                tool_call, tool_definitions[name]
            )
            if argument_error:
                result_text = f"Rejected tool arguments for {name}: {argument_error}"
                print(
                    f"  \033[33m⚠ invalid arguments for {name}: "
                    f"{argument_error}\033[0m",
                    file=sys.stderr,
                )
                add_result(tool_call, result_text)
                continue
            _print_tool_preview(name, arguments, verbose=_verbose_tools)
            # pre_tool_use hook (plan 2.2): deterministic gate around the
            # loop — non-zero exit blocks, JSON stdout rewrites arguments.
            allowed, hook_out = run_hook(
                "pre_tool_use", {"tool": name, "arguments": arguments}, config
            )
            if not allowed:
                reason = hook_out or "blocked by pre_tool_use hook"
                result_text = f"Blocked by pre_tool_use hook: {reason}"
                print(f"  \033[33m⚠ {name} blocked by hook\033[0m", file=sys.stderr)
                add_result(tool_call, result_text)
                continue
            if hook_out:
                try:
                    rewritten = json.loads(hook_out)
                except json.JSONDecodeError:
                    rewritten = None
                if not isinstance(rewritten, dict):
                    add_result(
                        tool_call,
                        f"Rejected tool call {name}: pre_tool_use hook returned "
                        "invalid replacement arguments.",
                    )
                    continue
                rewrite_error = _validate_schema_value(
                    rewritten,
                    tool_definitions[name]
                    .get("function", {})
                    .get("parameters", {}),
                )
                if rewrite_error:
                    add_result(
                        tool_call,
                        f"Rejected rewritten arguments for {name}: "
                        f"{rewrite_error}",
                    )
                    continue
                arguments = rewritten
                print("  \033[2m(arguments rewritten by pre_tool_use hook)\033[0m",
                      file=sys.stderr)
            try:
                if name in builtin_clients:
                    raw_result = builtin_clients[name].call_tool(name, arguments)
                    content_blocks = raw_result.get("content", [])
                    if isinstance(content_blocks, list):
                        result_text = "\n".join(
                            str(
                                block.get("text", block.get("content", ""))
                                if isinstance(block, dict)
                                else block
                            )
                            for block in content_blocks
                        ).strip()
                    else:
                        result_text = str(content_blocks)
                elif name in tool_map:
                    with Spinner(f"Running {name}"):
                        result_text = mcp_mod.execute_tool(tool_map, name, arguments)
                else:
                    result_text = (
                        f"Error: {name} is no longer connected. Refresh tools "
                        "and try again."
                    )
            except KeyboardInterrupt:
                result_text = "Tool execution cancelled by user."
                print(f"  \033[33m⚠ {name} cancelled\033[0m", file=sys.stderr)
            except Exception as exc:
                result_text = f"Error executing {name}: {exc}"
                print(
                    f"  \033[31m✗ {name} failed: {exc}\033[0m",
                    file=sys.stderr,
                )
            result_text = str(result_text or "(no output)")
            run_hook(
                "post_tool_use",
                {"tool": name, "arguments": arguments, "result": result_text},
                config,
            )
            is_error = result_text.startswith("Error") or "error" in result_text[:50].lower()
            _print_tool_result(result_text, verbose=_verbose_tools, error=is_error)
            add_result(tool_call, result_text)
        if provider == "anthropic":
            append_results_anthropic(messages, response, results)
        else:
            append_results_openai(messages, response, results)
    # Round budget exhausted: summarize progress instead of a bare marker
    # (plan 2.6).
    print(
        f"  \033[33m⚠ Tool round budget ({max_tool_rounds}) exhausted — "
        f"summarizing progress\033[0m",
        file=sys.stderr,
    )
    return (
        _graceful_exhaustion(config, provider, raw_fn, messages, total_usage,
                             "tool round budget"),
        total_usage,
    )

