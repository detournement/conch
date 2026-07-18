"""Chat runtime helpers."""

from __future__ import annotations

import re
import json
import sys
import time
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

# Running (chars sent, prompt tokens reported) totals. Ollama returns
# prompt_eval_count on every response, so the chars-per-token estimate can be
# calibrated to the active model's real tokenizer instead of the 3.5 guess.
_token_calibration = {"chars": 0.0, "tokens": 0}
_CALIBRATION_MIN_TOKENS = 200  # don't trust tiny samples
_CALIBRATION_CLAMP = (1.5, 8.0)


def record_token_calibration(char_count: int, token_count: int) -> None:
    if char_count <= 0 or token_count <= 0:
        return
    _token_calibration["chars"] += char_count
    _token_calibration["tokens"] += token_count


def reset_token_calibration() -> None:
    _token_calibration["chars"] = 0.0
    _token_calibration["tokens"] = 0


def get_chars_per_token() -> float:
    if _token_calibration["tokens"] >= _CALIBRATION_MIN_TOKENS:
        ratio = _token_calibration["chars"] / _token_calibration["tokens"]
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
    return max(2048, int(window * 0.9))


def char_count(messages: List[dict], tools: Optional[List[dict]] = None) -> int:
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                total += len(json.dumps(block)) if isinstance(block, dict) else len(str(block))
    if tools:
        total += len(json.dumps(tools))
    return total


def estimate_tokens(messages: List[dict], tools: Optional[List[dict]] = None) -> int:
    return int(char_count(messages, tools) / get_chars_per_token())


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
    head = int(budget_chars * 0.67)
    tail = max(0, budget_chars - head)
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n... [truncated {omitted:,} chars — head and tail kept] ...\n"
        + (text[-tail:] if tail else "")
    )


def tool_result_char_budget(provider: str, config: Optional[dict] = None) -> int:
    """Char budget for a single tool result, scaled to the model's window."""
    limit = get_context_limit(provider, config)
    tokens = max(_TOOL_RESULT_MIN_TOKENS, int(limit * TOOL_RESULT_BUDGET_FRACTION))
    return int(tokens * get_chars_per_token())


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
        content = " ".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    if not isinstance(content, str):
        content = str(content)
    return content.strip()


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
    if estimate_tokens(messages, tools) < limit * AUTO_COMPACT_THRESHOLD:
        return False
    has_system = bool(messages) and messages[0].get("role") == "system"
    start = 1 if has_system else 0
    cut = len(messages) - AUTO_COMPACT_KEEP_RECENT
    # Never split an assistant tool_call from its tool results.
    while cut > start and (
        messages[cut].get("role") == "tool" or is_anthropic_tool_result(messages[cut])
    ):
        cut -= 1
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
    # Keep the summary request itself well inside the window.
    transcript = truncate_middle(transcript, int(limit * 2))

    summary_messages = [
        {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
        {"role": "user", "content": transcript},
    ]
    try:
        response = raw_fn(config, summary_messages, None)
    except Exception:
        return False
    if is_error_response(response):
        return False
    summary = (response.get("content") or "").strip()
    if not summary:
        return False

    note = {
        "role": "system",
        "content": f"[Earlier conversation summarized]\n{summary}",
    }
    kept_recent = messages[cut:]
    new_messages = ([messages[0]] if has_system else []) + [note] + kept_recent
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
    if estimate_tokens(messages, tools) <= limit:
        return messages
    if len(messages) <= 5:
        return [summarize_message(message) for message in messages]
    system = messages[0]
    recent = messages[-4:]
    middle = messages[1:-4]
    compressed = [system] + [summarize_message(message) for message in middle] + recent
    if estimate_tokens(compressed, tools) <= limit:
        return compressed
    while middle and estimate_tokens([system] + [summarize_message(message) for message in middle] + recent, tools) > limit:
        middle.pop(0)
    if middle:
        note = {"role": "system", "content": f"[Earlier conversation compressed — {len(messages) - len(middle) - 5} messages summarized]"}
        return [system, note] + [summarize_message(message) for message in middle] + recent
    note = {"role": "system", "content": f"[Conversation history compressed — {len(messages) - 5} older messages dropped to fit context]"}
    return [system, note] + [summarize_message(message) for message in recent]


def append_results_openai(messages: List[dict], response: dict, results: List[dict]):
    assistant_message: Dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
    if response.get("tool_calls"):
        assistant_message["tool_calls"] = response["tool_calls"]
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


def extract_textual_tool_use_blocks(text: str) -> Optional[List[dict]]:
    """Guarded JSON-only recovery of tool calls the serving layer failed to
    parse into structured tool_calls.

    Live testing showed qwen2.5-coder via Ollama emitting calls either as
    bare JSON content ({"name": ..., "arguments": {...}}) or inside
    <tool_call>...</tool_call> tags. Both paths require strict JSON and a
    tool-call-shaped payload, so quoted prose or ordinary JSON answers are
    never executed. The old regex/XML/ast.literal_eval recovery paths were
    removed (plan 0.5) — native tool_calls plus these two JSON recoveries
    are the only accepted forms.
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

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
    for _ in range(max_tool_rounds):
        if provider == "anthropic":
            sanitize_anthropic_messages(messages)
        if chat_state and getattr(chat_state, "needs_tool_refresh", False):
            tools = chat_state.tools
            chat_state.needs_tool_refresh = False
        # Model-generated compaction first (plan 1.4); char-capping
        # compress_context stays as the cheap backstop below.
        if raw_fn is not None:
            try:
                if auto_compact(messages, tools, provider, config, raw_fn):
                    print("  \033[2m(older history auto-compacted)\033[0m", file=sys.stderr)
            except Exception:
                pass
        compressed = compress_context(messages, tools, provider, config)
        if len(compressed) < len(messages):
            messages.clear()
            messages.extend(compressed)
            if provider == "anthropic":
                sanitize_anthropic_messages(messages)
        send_messages = normalize_messages_for_provider(messages, provider)

        send_tools = tools
        tool_limit = PROVIDER_TOOL_LIMITS.get(provider)
        if tool_limit and send_tools and len(send_tools) > tool_limit:
            # Over the provider cap: keep pinned tools and fill the rest by
            # relevance to the current user turn (plan 1.6), not list order.
            from .tooling import select_relevant_tools
            user_text = next(
                (m.get("content", "") for m in reversed(messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "",
            )
            send_tools = select_relevant_tools(send_tools, user_text, tool_limit)

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
                char_count(send_messages, send_tools), usage["input_tokens"]
            )
        content = response.get("content", "")
        if is_error_response(response) and on_token is not None:
            sp = getattr(on_token, "__self__", None)
            if isinstance(sp, StreamPrinter):
                sp.end_waiting()
        if is_error_response(response):
            # Retry once on same provider with 1s backoff (transient errors:
            # rate limits, 5xx, connection refused/timeout, missing model)
            if is_transient_error(error_detail(response)):
                print(f"  \033[33m\u26a0 Transient error, retrying in 1s...\033[0m", file=sys.stderr)
                time.sleep(1)
                if stream_fn:
                    response = stream_fn(config, send_messages, tools if tools else None, on_token)
                else:
                    with Spinner("Retrying"):
                        response = raw_fn(config, send_messages, tools if tools else None)
                usage = response.get("_usage", {})
                total_usage["input_tokens"] += usage.get("input_tokens", 0)
                total_usage["output_tokens"] += usage.get("output_tokens", 0)
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
                    fb_tool_limit = PROVIDER_TOOL_LIMITS.get(fb_provider)
                    fb_tools = tools
                    if fb_tool_limit and fb_tools and len(fb_tools) > fb_tool_limit:
                        from .tooling import select_relevant_tools as _select
                        _user_text = next(
                            (m.get("content", "") for m in reversed(messages)
                             if m.get("role") == "user" and isinstance(m.get("content"), str)),
                            "",
                        )
                        fb_tools = _select(fb_tools, _user_text, fb_tool_limit)
                    with Spinner(f"Retrying with {fb_provider}/{fb_model}"):
                        response = fb_fn(fb_config, fb_messages, fb_tools if fb_tools else None)
                    if not is_error_response(response):
                        provider = fb_provider
                        config["provider"] = fb_provider
                        config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(fb_provider, "")
                        config["chat_model"] = fb_model
                        config["model"] = fb_model
                        stream_fn = STREAM_FNS.get(provider) if on_token else None
                        raw_fn = RAW_FNS.get(provider)
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
                return "", total_usage
        tool_calls = response.get("tool_calls")
        if on_token is not None:
            sp = getattr(on_token, "__self__", None)
            if hasattr(sp, "end_waiting"):
                sp.end_waiting()
        if not tool_calls:
            recovered = extract_textual_tool_use_blocks(response.get("content", ""))
            if not recovered:
                return response.get("content", ""), total_usage
            tool_calls = [{
                "id": str(block.get("id")),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            } for block in recovered]
            response["tool_calls"] = tool_calls
            if provider == "anthropic":
                response["_anthropic_content"] = recovered
            response["content"] = ""
            print("  \033[2m(recovered textual tool call)\033[0m", file=sys.stderr)
        results = []
        for tool_call in tool_calls:
            fn = tool_call.get("function", {})
            name = fn.get("name", "unknown")
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            _print_tool_preview(name, arguments, verbose=_verbose_tools)
            try:
                if name in builtin_clients:
                    raw_result = builtin_clients[name].call_tool(name, arguments)
                    result_text = raw_result.get("content", [{}])[0].get("text", "")
                else:
                    with Spinner(f"Running {name}"):
                        result_text = mcp_mod.execute_tool(tool_map, name, arguments)
            except KeyboardInterrupt:
                result_text = "Tool execution cancelled by user."
                print(f"  \033[33m⚠ {name} cancelled\033[0m", file=sys.stderr)
            is_error = result_text.startswith("Error") or "error" in result_text[:50].lower()
            _print_tool_result(result_text, verbose=_verbose_tools, error=is_error)
            # Budget scaled to the model's context window (plan 1.5), not a
            # fixed char cap; keeps head + tail of oversized output.
            result_text = truncate_tool_result(result_text, provider, config)
            results.append({"id": tool_call.get("id", ""), "content": result_text})
        if provider == "anthropic":
            append_results_anthropic(messages, response, results)
        else:
            append_results_openai(messages, response, results)
    return "[max tool call rounds reached]", total_usage

