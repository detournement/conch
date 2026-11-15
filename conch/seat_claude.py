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


def invoke(model: str, prompt: str, schema: dict, timeout: float) -> dict:
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
                "claude", *_ISOLATION, "--print", "--output-format", "json",
                "--json-schema", encoded_schema, "--tools", "",
                "--disable-slash-commands", "--no-session-persistence",
                "--no-chrome", "--permission-mode", "dontAsk",
                "--model", model or "sonnet",
            ]
            result = run(argv, prompt=prompt, timeout=remaining, cwd=cwd)
    except OSError as exc:
        raise SeatError(f"Could not isolate Claude ({type(exc).__name__})") from exc
    if result.returncode != 0:
        # Native stderr can contain account details or echoed prompt content.
        raise SeatError(
            f"Claude invocation failed (exit {result.returncode}); check the official CLI login, model access and subscription limits. {_LOGIN_REMEDY}"
        )
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
    return {"response": response, "usage": usage, "model": actual_model}
