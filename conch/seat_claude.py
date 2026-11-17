"""Subscription transport through the unmodified, provider-owned Claude CLI.

The native CLI owns credentials, refresh and JSON Schema validation. Conch owns
conversation state and every executable tool; this process has no native tools.
Result/usage contract: https://code.claude.com/docs/en/agent-sdk/typescript
"""

from __future__ import annotations

import json
import math
import tempfile
import time

from .seat_common import SeatError, run

_LOGIN_REMEDY = "Run conch auth login claude-code."
# Safe mode, unlike --bare, preserves native subscription authentication. It
# disables customizations including installed plugins, LSP, memory and helpers.
# Restricted mode also ignores user/project/local settings; explicit settings
# disable hooks while leaving the native credential store in place.
# Keep a flag after variadic --mcp-config so it cannot consume auth subcommands.
_ISOLATION = (
    "--restricted", "--setting-sources", "",
    "--settings", '{"disableAllHooks":true,"forceLoginMethod":"claudeai"}',
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--safe-mode",
)


def auth_command(action: str) -> list[str]:
    """Return only official native login operations, never credential material."""
    if action not in ("login", "logout", "status"):
        raise SeatError("unsupported Claude authentication action")
    argv = ["claude", *_ISOLATION, "auth", action]
    if action == "login":
        argv.append("--claudeai")
    elif action == "status":
        argv.append("--json")
    return argv


def _status(timeout: float, cwd: str) -> tuple[bool, str]:
    try:
        result = run(auth_command("status"), timeout=timeout, cwd=cwd)
    except SeatError as exc:
        return False, f"{exc}. {_LOGIN_REMEDY}"
    try:
        identity = json.loads(result.stdout)
    except (ValueError, TypeError):
        return False, f"Claude returned unreadable login status; update the official CLI. {_LOGIN_REMEDY}"
    if not isinstance(identity, dict):
        return False, f"Claude returned invalid login status. {_LOGIN_REMEDY}"
    if result.returncode != 0 or identity.get("loggedIn") is not True:
        return False, f"Claude has no usable native login. {_LOGIN_REMEDY}"
    if identity.get("authMethod") != "claude.ai":
        return False, f"Claude is not using a stored Claude subscription login (API-key and Console logins are not seats). {_LOGIN_REMEDY}"
    if identity.get("apiProvider", "firstParty") != "firstParty":
        return False, f"Claude is routed to an API provider, not a Claude subscription. {_LOGIN_REMEDY}"
    # A claude.ai identity with a null tier can still route to API billing; do
    # not infer a paid seat from an OAuth login alone.
    tier = identity.get("subscriptionType")
    if not isinstance(tier, str) or tier.strip().lower() not in {
        "pro", "max", "ultra", "team", "enterprise",
    }:
        return False, f"Claude has not confirmed a subscription seat. {_LOGIN_REMEDY}"
    # Do not echo identity fields: future native versions may add secrets.
    return True, "Native Claude subscription login detected; model access has not been verified."


def status(timeout: float = 10) -> tuple[bool, str]:
    """Inspect native login metadata, without claiming successful inference."""
    if not math.isfinite(timeout) or timeout <= 0:
        return False, f"Claude login status requires a positive finite timeout. {_LOGIN_REMEDY}"
    try:
        with tempfile.TemporaryDirectory(prefix="conch-claude-status-") as cwd:
            return _status(timeout, cwd)
    except OSError as exc:
        return False, f"Could not isolate Claude login status ({type(exc).__name__}). {_LOGIN_REMEDY}"


def _count(usage: dict, key: str, *, optional: bool = False) -> int:
    value = usage.get(key, 0 if optional else None)
    if optional and value is None:
        return 0
    if type(value) is not int or value < 0:
        raise SeatError("Claude returned malformed token usage")
    return value


def _usage(payload: dict) -> dict:
    # Per-model totals include auxiliary calls, unlike the main-loop-only
    # `usage` field. Input counts must include both cache reads and writes.
    models = payload.get("modelUsage")
    if models is not None and not isinstance(models, dict):
        raise SeatError("Claude returned malformed model usage")
    if models:
        input_tokens = output_tokens = 0
        for usage in models.values():
            if not isinstance(usage, dict):
                raise SeatError("Claude returned malformed model usage")
            input_tokens += (
                _count(usage, "inputTokens")
                + _count(usage, "cacheReadInputTokens", optional=True)
                + _count(usage, "cacheCreationInputTokens", optional=True)
            )
            output_tokens += _count(usage, "outputTokens")
        return {"input_tokens": input_tokens, "output_tokens": output_tokens}
    usage = payload.get("usage")
    if usage is None:
        return {}
    if not isinstance(usage, dict):
        raise SeatError("Claude returned malformed token usage")
    return {
        "input_tokens": (
            _count(usage, "input_tokens")
            + _count(usage, "cache_read_input_tokens", optional=True)
            + _count(usage, "cache_creation_input_tokens", optional=True)
        ),
        "output_tokens": _count(usage, "output_tokens"),
    }


class _Events:
    """Consume Claude's partial messages without exposing its schema envelope."""

    def __init__(self, on_detail):
        self.on_detail = on_detail
        self.result = None
        self.message_id = ""
        self.blocks = {}
        self.current_index = 0
        self.thinking = {}
        self.structured_ids = set()

    def detail(self, kind, text):
        if text:
            self.on_detail(kind, text)

    def _thinking(self, key, text):
        if not isinstance(text, str):
            raise SeatError("Claude returned invalid thinking text")
        previous = self.thinking.get(key, "")
        if text.startswith(previous):
            self.thinking[key] = text
            self.detail("reasoning", text[len(previous):])

    def _block(self, block, key):
        if not isinstance(block, dict):
            raise SeatError("Claude returned an invalid content block")
        kind = block.get("type")
        if kind == "thinking":
            self._thinking(key, block.get("thinking", ""))
        elif kind == "tool_use":
            # This CLI-owned validator returns data, not native execution:
            # https://code.claude.com/docs/en/agent-sdk/structured-outputs
            identity = block.get("id")
            if block.get("name") != "StructuredOutput" or not isinstance(identity, str) or not identity:
                raise SeatError("Claude attempted a native tool; Conch must own all tool execution")
            self.structured_ids.add(identity)
        elif kind == "tool_result":
            identity = block.get("tool_use_id")
            if not isinstance(identity, str) or identity not in self.structured_ids:
                raise SeatError("Claude returned an unrecognized native tool result")
            # A validation failure may be retried by Claude. The final result
            # remains authoritative; never display unvalidated input or errors.
        elif kind not in ("text", "redacted_thinking"):
            raise SeatError("Claude attempted an unsupported native content block")
        return kind

    def _message(self, message):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            raise SeatError("Claude returned an invalid assistant message")
        identity = message.get("id", self.message_id)
        if not isinstance(identity, str):
            raise SeatError("Claude returned an invalid message identity")
        blocks = message["content"]
        for index, block in enumerate(blocks):
            # The CLI publishes each completed block before its stop event,
            # using the same message ID, not always the whole accumulated list.
            if len(blocks) == 1 and identity == self.message_id:
                index = self.current_index
            self._block(block, (identity, index))

    def feed(self, line):
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise SeatError("Claude returned malformed stream JSON") from exc
        if not isinstance(event, dict):
            raise SeatError("Claude returned an invalid event stream")
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "api_retry":
            # The CLI retries transient API failures itself; the final result
            # stays authoritative for whether the turn succeeded.
            self.detail("progress", "Claude retrying after a transient API error")
            return
        if event.get("error") or event.get("is_error") or kind == "error":
            raise SeatError("Claude reported a provider error; check native login and subscription limits")
        if event.get("parent_tool_use_id"):
            raise SeatError("Claude attempted a native subagent")
        if kind == "result":
            if self.result is not None:
                raise SeatError("Claude returned multiple final results")
            self.result = event
        elif kind == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                self.detail("progress", "Claude session started")
            elif subtype in ("status", "compact_boundary"):
                self.detail("progress", "Claude updating session context")
            else:
                raise SeatError("Claude returned an unsupported system event")
        elif kind == "assistant":
            self._message(event.get("message"))
        elif kind == "user":
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                raise SeatError("Claude returned an invalid tool result message")
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    raise SeatError("Claude returned an unexpected user message")
                self._block(block, ("", 0))
        elif kind == "stream_event":
            self._partial(event.get("event"))
        elif kind == "rate_limit_event":
            self.detail("progress", "Claude rate limit update")
        else:
            raise SeatError("Claude returned an unsupported event; update Conch or the official CLI")

    def _partial(self, event):
        if not isinstance(event, dict):
            raise SeatError("Claude returned an invalid partial message")
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise SeatError("Claude returned an invalid message identity")
            self.message_id = message["id"]
            self.blocks = {}
            self.current_index = 0
            self._message(message)
            self.detail("progress", "Claude generating")
        elif kind in ("content_block_start", "content_block_delta", "content_block_stop"):
            index = event.get("index")
            if type(index) is not int or index < 0:
                raise SeatError("Claude returned an invalid content block index")
            key = (self.message_id, index)
            if kind == "content_block_start":
                block = event.get("content_block")
                self.blocks[index] = self._block(block, key)
                self.current_index = index
                if self.blocks[index] == "text":
                    self.detail("progress", "Claude preparing response")
                elif self.blocks[index] == "tool_use":
                    self.detail("progress", "Claude preparing structured response")
            elif index not in self.blocks:
                raise SeatError("Claude returned a delta without a content block")
            elif kind == "content_block_delta":
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    raise SeatError("Claude returned an invalid content delta")
                delta_kind = delta.get("type")
                block_kind = self.blocks[index]
                if delta_kind == "thinking_delta" and block_kind == "thinking":
                    text = delta.get("thinking")
                    if not isinstance(text, str):
                        raise SeatError("Claude returned invalid thinking text")
                    self._thinking(key, self.thinking.get(key, "") + text)
                elif (delta_kind, block_kind) not in (
                    ("signature_delta", "thinking"), ("text_delta", "text"),
                    ("input_json_delta", "tool_use"),
                ):
                    raise SeatError("Claude returned an unsupported content delta")
        elif kind == "message_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise SeatError("Claude returned an invalid message delta")
            if delta.get("stop_reason") == "tool_use" and "tool_use" not in self.blocks.values():
                raise SeatError("Claude attempted an unrecognized native tool")
        elif kind not in ("message_stop", "ping"):
            raise SeatError("Claude returned an unsupported partial event")


def invoke(model: str, prompt: str, schema: dict, timeout: float, on_detail=None) -> dict:
    """Generate one schema-constrained response without executing native tools."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise SeatError("Claude requires a positive finite timeout")
    started = time.monotonic()
    try:
        encoded_schema = json.dumps(schema, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SeatError("Claude requires a JSON-serializable output schema") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="conch-claude-") as cwd:
            ready, diagnosis = _status(min(timeout, 10), cwd)
            if not ready:
                raise SeatError(diagnosis)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise SeatError("Claude timed out while checking its native login")
            argv = [
                "claude", *_ISOLATION, "--print", "--output-format",
                "stream-json" if on_detail is not None else "json",
                "--json-schema", encoded_schema, "--tools", "",
                "--disable-slash-commands", "--no-session-persistence",
                "--no-chrome", "--permission-mode", "dontAsk",
                "--model", model or "sonnet",
            ]
            events = None
            if on_detail is None:
                result = run(argv, prompt=prompt, timeout=remaining, cwd=cwd)
            else:
                argv.extend(["--verbose", "--include-partial-messages"])
                events = _Events(on_detail)
                result = run(argv, prompt=prompt, timeout=remaining, cwd=cwd,
                             on_stdout_line=events.feed)
    except OSError as exc:
        raise SeatError(f"Could not isolate Claude ({type(exc).__name__})") from exc
    if result.returncode != 0:
        # Native stderr can contain account details or echoed prompt content.
        raise SeatError(
            f"Claude invocation failed (exit {result.returncode}); check the official CLI login, model access and subscription limits. {_LOGIN_REMEDY}"
        )
    if events is not None:
        payload = events.result
    else:
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise SeatError("Claude returned malformed result JSON") from exc
    if not isinstance(payload, dict) or payload.get("type") != "result":
        raise SeatError("Claude returned no final result")
    if payload.get("subtype") != "success" or payload.get("is_error") is not False:
        raise SeatError("Claude reported a provider error or incomplete structured generation; check native login, model access and subscription limits")
    if payload.get("errors") or payload.get("api_error_status") or payload.get("permission_denials"):
        raise SeatError("Claude reported an invocation error or attempted native tool execution")
    response = payload.get("structured_output")
    if not isinstance(response, dict):
        raise SeatError("Claude returned no schema-constrained response object")
    usage = _usage(payload)
    models = payload.get("modelUsage") or {}
    actual_model = next(iter(models)) if len(models) == 1 else model or "sonnet"
    if not isinstance(actual_model, str) or not actual_model:
        raise SeatError("Claude returned an invalid model identity")
    if on_detail is not None:
        on_detail("progress", "Claude generation complete")
    return {"response": response, "usage": usage, "model": actual_model}
