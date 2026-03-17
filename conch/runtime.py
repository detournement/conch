"""Chat runtime helpers."""

from __future__ import annotations

import ast
import re
import json
import sys
from typing import Any, Dict, List, Optional

from . import mcp as mcp_mod
from .render import Spinner


CHARS_PER_TOKEN = 3.5
CONTEXT_LIMITS = {
    "openai": 120000,
    "anthropic": 180000,
    "ollama": 28000,
}


def estimate_tokens(messages: List[dict], tools: Optional[List[dict]] = None) -> int:
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
    return int(total / CHARS_PER_TOKEN)


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


def compress_context(messages: List[dict], tools: Optional[List[dict]], provider: str) -> List[dict]:
    limit = CONTEXT_LIMITS.get(provider, 120000)
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
    assistant_message: Dict[str, Any] = {"role": "assistant", "content": response.get("content") or None}
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


def extract_textual_tool_use_blocks(text: str) -> Optional[List[dict]]:
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

    # Parse <tool_called name="..." args='...'/>  or  args="..." XML format
    if "<tool_called" in raw:
        normalized = []
        for m in re.finditer(r'<tool_called[^>]+/?>', raw, re.DOTALL):
            chunk = m.group(0)
            name_m = re.search(r'name="([^"]+)"', chunk)
            if not name_m:
                continue
            name = name_m.group(1)
            # Match args with either quote style
            args_str = ""
            for q in ("'", '"'):
                marker = f"args={q}"
                if marker in chunk:
                    start = chunk.find(marker) + len(marker)
                    end = chunk.find(q, start)
                    if end > start:
                        args_str = chunk[start:end]
                    break
            try:
                tool_input = json.loads(args_str) if args_str.strip() else {}
            except (json.JSONDecodeError, ValueError):
                tool_input = {}
            if not isinstance(tool_input, dict):
                tool_input = {}
            normalized.append({
                "type": "tool_use",
                "id": f"xml_tool_use_{len(normalized)+1}",
                "name": name,
                "input": tool_input,
            })
        if normalized:
            return normalized

    if "tool_use" not in raw:
        return None
    candidates = [raw]
    if raw.startswith("```") and raw.endswith("```"):
        fenced = raw.strip("`").strip()
        candidates.append(fenced.split("\n", 1)[1].strip() if "\n" in fenced else fenced)
    for opener, closer in (("{", "}"), ("[", "]"), ("(", ")")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            parsed = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            continue
        blocks = [parsed] if isinstance(parsed, dict) else [block for block in parsed if isinstance(block, dict)] if isinstance(parsed, (list, tuple)) else []
        normalized = []
        for idx, block in enumerate(blocks, start=1):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue
            tool_input = block.get("input", {})
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except json.JSONDecodeError:
                    tool_input = {}
            if not isinstance(tool_input, dict):
                tool_input = {}
            normalized.append({
                "type": "tool_use",
                "id": str(block.get("id") or f"text_tool_use_{idx}"),
                "name": name,
                "input": tool_input,
            })
        if normalized:
            return normalized
    return None



def normalize_messages_for_provider(messages: list, provider: str) -> list:
    """Flatten Anthropic-style structured messages for OpenAI-compatible providers.

    Drops tool execution pairs entirely to prevent the model from echoing
    tool-call artifacts. Only keeps text content from assistant messages.
    """
    if provider == "anthropic":
        return messages
    normalized = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Drop OpenAI-style tool-role messages
        if role == "tool":
            i += 1
            continue

        if isinstance(content, list):
            has_tool = any(
                isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                for b in content
            )
            # Extract only text parts
            text_parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text_content = "\n".join(p for p in text_parts if p).strip()

            if has_tool and not text_content:
                # Pure tool message with no text — skip it and its tool_result pair
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
        else:
            # Drop assistant messages that were pure tool calls
            if role == "assistant" and msg.get("tool_calls") and not str(content).strip():
                i += 1
                continue
            if str(content).strip():
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

        # Drop assistant messages that were pure tool calls (no text content)
        if role == "assistant" and msg.get("tool_calls") and not str(content).strip():
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
    max_tool_rounds: int = 10,
    chat_state=None,
) -> tuple:
    """Returns (reply_text, usage_info) where usage_info is a dict with
    input_tokens, output_tokens, and model."""
    total_usage = {"input_tokens": 0, "output_tokens": 0, "model": ""}
    for _ in range(max_tool_rounds):
        if provider == "anthropic":
            sanitize_anthropic_messages(messages)
        if chat_state and getattr(chat_state, "needs_tool_refresh", False):
            tools = chat_state.tools
            chat_state.needs_tool_refresh = False
        compressed = compress_context(messages, tools, provider)
        if len(compressed) < len(messages):
            messages.clear()
            messages.extend(compressed)
        send_messages = normalize_messages_for_provider(messages, provider)
        with Spinner("Thinking"):
            response = raw_fn(config, send_messages, tools if tools else None)
        usage = response.get("_usage", {})
        total_usage["input_tokens"] += usage.get("input_tokens", 0)
        total_usage["output_tokens"] += usage.get("output_tokens", 0)
        total_usage["model"] = response.get("_model", total_usage["model"])
        content = response.get("content", "")
        if isinstance(content, str) and content.startswith("[API error:"):
            from .providers import RAW_FNS, DEFAULT_API_KEY_ENVS, get_fallback_chain
            current_model = config.get("chat_model", config.get("model", ""))
            fallback_chain = get_fallback_chain(provider, current_model)
            for fb_provider, fb_model, needs_ctx_switch in fallback_chain:
                fb_fn = RAW_FNS.get(fb_provider)
                if not fb_fn:
                    continue
                print(f"  \033[33m⚠ {provider}/{current_model} failed, trying {fb_provider}/{fb_model}\033[0m", file=sys.stderr)
                fb_config = dict(config)
                fb_config["provider"] = fb_provider
                fb_config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(fb_provider, "")
                fb_config["chat_model"] = fb_model
                fb_config["model"] = fb_model
                if needs_ctx_switch:
                    normalize_messages_on_switch(messages, fb_provider)
                fb_messages = normalize_messages_for_provider(messages, fb_provider)
                with Spinner(f"Retrying with {fb_provider}/{fb_model}"):
                    response = fb_fn(fb_config, fb_messages, tools if tools else None)
                fb_content = response.get("content", "")
                if not (isinstance(fb_content, str) and fb_content.startswith("[API error:")):
                    break
        tool_calls = response.get("tool_calls")
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
            print(f"  \033[2m⚡ {name}\033[0m", file=sys.stderr)
            if name in builtin_clients:
                raw_result = builtin_clients[name].call_tool(name, arguments)
                result_text = raw_result.get("content", [{}])[0].get("text", "")
            else:
                with Spinner(f"Running {name}"):
                    result_text = mcp_mod.execute_tool(tool_map, name, arguments)
            if len(result_text) > 8000:
                result_text = result_text[:8000] + "\n... (truncated — result too large)"
            results.append({"id": tool_call.get("id", ""), "content": result_text})
        if provider == "anthropic":
            append_results_anthropic(messages, response, results)
        else:
            append_results_openai(messages, response, results)
    return "[max tool call rounds reached]", total_usage

