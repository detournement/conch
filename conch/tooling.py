"""Tool filtering and built-in tool clients."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


MAX_GROUP_TOOLS = 200
MAX_ACTIVE_TOOLS = 300
PINNED_TOOL_NAMES = {"local_shell", "manage_tools", "save_memory", "public_api"}

TOOL_PREFS_PATH = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch" / "tool_prefs.json"

_agent_mode = False


def set_agent_mode(enabled: bool):
    global _agent_mode
    _agent_mode = enabled


def get_agent_mode() -> bool:
    return _agent_mode


def load_tool_prefs() -> dict:
    try:
        return json.loads(TOOL_PREFS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_tool_prefs(prefs: dict):
    TOOL_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOL_PREFS_PATH.write_text(json.dumps(prefs, indent=2))


def tool_group(name: str, tool_map: dict) -> str:
    if name in ("local_shell", "manage_tools", "save_memory"):
        return name
    client = tool_map.get(name)
    client_name = getattr(client, "name", "unknown") if client else "unknown"
    if client_name == "composio" and "_" in name:
        return name.split("_")[0].lower()
    return client_name


def group_tools(all_tools: List[dict], tool_map: dict) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for tool in all_tools:
        name = tool["function"]["name"]
        groups.setdefault(tool_group(name, tool_map), []).append(name)
    return groups


def apply_filter(all_tools: List[dict], tool_map: dict, prefs: dict) -> List[dict]:
    disabled = set(prefs.get("disabled_groups", []))
    picked = set(prefs.get("picked_tools", []))
    if not disabled:
        return all_tools
    result = []
    for tool in all_tools:
        name = tool["function"]["name"]
        grp = tool_group(name, tool_map)
        if grp not in disabled or name in picked:
            result.append(tool)
    return result


def cap_tools(tools: List[dict], max_tools: int = MAX_ACTIVE_TOOLS) -> List[dict]:
    if len(tools) <= max_tools:
        return tools
    pinned: List[dict] = []
    others: List[dict] = []
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        if name in PINNED_TOOL_NAMES:
            pinned.append(tool)
        else:
            others.append(tool)
    if len(pinned) >= max_tools:
        return pinned[:max_tools]
    return others[: max_tools - len(pinned)] + pinned


def auto_disable_oversized_groups(all_tools: List[dict], tool_map: dict, prefs: dict) -> tuple[dict, List[tuple[str, int]]]:
    disabled = set(prefs.get("disabled_groups", []))
    auto_disabled = []
    for grp, names in group_tools(all_tools, tool_map).items():
        if grp in disabled:
            continue
        if len(names) > MAX_GROUP_TOOLS:
            disabled.add(grp)
            auto_disabled.append((grp, len(names)))
    prefs["disabled_groups"] = sorted(disabled)
    return prefs, auto_disabled


# ---------------------------------------------------------------------------
# Tool profiles — named presets for which tool groups are enabled
# ---------------------------------------------------------------------------

BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "minimal": {
        "description": "Shell tools only",
        "groups": None,
    },
    "dev": {
        "description": "Development tools (GitHub, Jira)",
        "groups": {"github", "jira"},
    },
    "comms": {
        "description": "Communication tools (Gmail, Slack)",
        "groups": {"gmail", "slack"},
    },
    "full": {
        "description": "All tools enabled",
        "groups": "__all__",
    },
}


def list_profiles() -> Dict[str, Dict[str, Any]]:
    """Return builtin + user-defined profiles."""
    prefs = load_tool_prefs()
    custom = prefs.get("custom_profiles", {})
    merged = dict(BUILTIN_PROFILES)
    merged.update(custom)
    return merged


def active_profile_name() -> str:
    prefs = load_tool_prefs()
    return prefs.get("active_profile", "")


def activate_profile(
    name: str,
    all_tools: List[dict],
    tool_map: Dict[str, Any],
) -> tuple[List[dict], str]:
    """Activate a profile and return (filtered_tools, description).

    Sets disabled_groups in prefs so that only the profile's groups (plus
    pinned tools) are active.  Returns the new active tool list.
    """
    profiles = list_profiles()
    profile = profiles.get(name)
    if not profile:
        return [], f"Unknown profile '{name}'. Use /profiles to list."

    prefs = load_tool_prefs()
    all_groups = set(group_tools(all_tools, tool_map).keys())

    wanted = profile.get("groups")
    if wanted == "__all__":
        prefs["disabled_groups"] = []
    elif wanted is None:
        prefs["disabled_groups"] = sorted(all_groups - PINNED_TOOL_NAMES)
    else:
        if isinstance(wanted, list):
            wanted = set(wanted)
        prefs["disabled_groups"] = sorted(
            all_groups - wanted - PINNED_TOOL_NAMES
        )

    prefs["active_profile"] = name
    save_tool_prefs(prefs)
    tools = cap_tools(apply_filter(all_tools, tool_map, prefs))
    return tools, profile.get("description", name)


@dataclass
class ToolRuntimeState:
    all_tools: List[dict]
    tool_map: Dict[str, Any]
    tools: List[dict]
    needs_tool_refresh: bool = False


@dataclass
class LocalShellPolicy:
    interactive: bool = True
    allow_auto_execute: bool = False


LOCAL_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "local_shell",
        "description": "Execute a shell command on the user's machine and return stdout/stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "integer", "description": "Max seconds to wait (default 60)"},
            },
            "required": ["command"],
        },
    },
}

MANAGE_TOOLS_TOOL = {
    "type": "function",
    "function": {
        "name": "manage_tools",
        "description": "Search for and selectively enable tool groups or individual tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "search", "enable_tools", "enable", "disable"]},
                "group": {"type": "string"},
                "query": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
        },
    },
}

SAVE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": "Persist a fact or preference to memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
            },
            "required": ["content"],
        },
    },
}


class LocalShellClient:
    name = "local_shell"

    def __init__(self):
        self.policy = LocalShellPolicy()

    def set_policy(self, policy: LocalShellPolicy):
        self.policy = policy

    def call_tool(self, name: str, arguments: dict) -> dict:
        cmd = arguments.get("command", "")
        timeout = int(arguments.get("timeout", 60))
        if not cmd:
            return {"content": [{"type": "text", "text": "Error: empty command"}]}

        print(f"\n  \033[1;33m⚠ Run locally:\033[0m \033[1m{cmd}\033[0m")
        auto_execute = self.policy.allow_auto_execute or get_agent_mode()
        if self.policy.interactive and not auto_execute:
            try:
                answer = input("  \033[1;33mExecute? [y/N]\033[0m ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                return {"content": [{"type": "text", "text": "User declined to execute the command."}]}
        elif not self.policy.interactive and not auto_execute:
            return {"content": [{"type": "text", "text": "Background tasks cannot prompt for local command confirmation."}]}
        else:
            print("  \033[2m(agent mode — auto-executing)\033[0m")

        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"content": [{"type": "text", "text": f"Command timed out after {timeout}s"}]}
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}]}

        output = result.stdout or ""
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if not output:
            output = f"(no output, exit code {result.returncode})"
        elif result.returncode != 0:
            output += f"\n(exit code {result.returncode})"
        if len(output) > 15000:
            output = output[:15000] + "\n... (truncated)"
        return {"content": [{"type": "text", "text": output}]}


class ManageToolsClient:
    name = "manage_tools"

    def __init__(self):
        self._chat_state: Optional[ToolRuntimeState] = None

    def bind(self, state: ToolRuntimeState):
        self._chat_state = state

    def call_tool(self, name: str, arguments: dict) -> dict:
        if not self._chat_state:
            return {"content": [{"type": "text", "text": "Error: tool manager not initialized"}]}
        action = arguments.get("action", "list")
        group = arguments.get("group", "").lower()
        state = self._chat_state
        groups = group_tools(state.all_tools, state.tool_map)
        prefs = load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))

        if action == "list":
            lines = ["Tool groups:"]
            for grp in sorted(groups):
                status = "OFF" if grp in disabled else "ON"
                lines.append(f"  [{status}] {grp} ({len(groups[grp])} tools)")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        if action == "search":
            query = arguments.get("query", "").lower()
            keywords = query.split()
            matches = []
            for tool in state.all_tools:
                fn = tool["function"]
                haystack = (fn["name"] + " " + fn.get("description", "")).lower()
                score = sum(1 for keyword in keywords if keyword in haystack)
                if score:
                    matches.append((score, fn["name"], fn.get("description", "")[:80]))
            matches.sort(reverse=True)
            lines = [f"Found {len(matches[:20])} tools matching '{query}':"]
            for _, tool_name, desc in matches[:20]:
                lines.append(f"  {tool_name} — {desc}")
            if len(lines) == 1:
                lines.append("  No tools found.")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        if action == "enable":
            disabled.discard(group)
            prefs["disabled_groups"] = sorted(disabled)
            save_tool_prefs(prefs)
            state.tools = cap_tools(apply_filter(state.all_tools, state.tool_map, prefs))
            state.needs_tool_refresh = True
            return {"content": [{"type": "text", "text": f"Enabled {group}. {len(state.tools)} tools now active."}]}

        if action == "disable":
            disabled.add(group)
            prefs["disabled_groups"] = sorted(disabled)
            save_tool_prefs(prefs)
            state.tools = cap_tools(apply_filter(state.all_tools, state.tool_map, prefs))
            state.needs_tool_refresh = True
            return {"content": [{"type": "text", "text": f"Disabled {group}. {len(state.tools)} tools now active."}]}

        if action == "enable_tools":
            names = arguments.get("tools", [])
            valid = {tool["function"]["name"] for tool in state.all_tools}
            picked = set(prefs.get("picked_tools", []))
            added = []
            for tool_name in names:
                clean = tool_name.split(" —")[0].strip()
                if clean in valid:
                    picked.add(clean)
                    added.append(clean)
            prefs["picked_tools"] = sorted(picked)
            save_tool_prefs(prefs)
            state.tools = cap_tools(apply_filter(state.all_tools, state.tool_map, prefs))
            state.needs_tool_refresh = True
            return {"content": [{"type": "text", "text": f"Loaded {len(added)} tools: {', '.join(added)}"}]}

        return {"content": [{"type": "text", "text": f"Unknown action: {action}"}]}


class SaveMemoryClient:
    name = "save_memory"

    def __init__(self):
        self._memory = None

    def bind(self, memory):
        self._memory = memory

    def call_tool(self, name: str, arguments: dict) -> dict:
        if self._memory is None:
            return {"content": [{"type": "text", "text": "Error: memory not initialized"}]}
        content = arguments.get("content", "").strip()
        if not content:
            return {"content": [{"type": "text", "text": "Error: empty memory"}]}
        entry = self._memory.add(content, source="auto")
        return {"content": [{"type": "text", "text": f"Saved memory #{entry['id']}: {content}"}]}


def inject_builtin_tools(all_tools: List[dict], tool_map: Dict[str, Any], clients: Dict[str, Any]):
    builtin = [LOCAL_SHELL_TOOL, MANAGE_TOOLS_TOOL, SAVE_MEMORY_TOOL, PUBLIC_API_TOOL]
    if "conch_config" in clients:
        builtin.append(CONCH_CONFIG_TOOL)
    all_tools.extend(builtin)
    for tool_def in builtin:
        name = tool_def["function"]["name"]
        if name in clients:
            tool_map[name] = clients[name]



# ---------------------------------------------------------------------------
# conch_config — lets the LLM inspect and change its own configuration
# ---------------------------------------------------------------------------

CONCH_CONFIG_TOOL = {
    "type": "function",
    "function": {
        "name": "conch_config",
        "description": (
            "Read or change Conch's own configuration. Use this when the user "
            "asks to switch models, change providers, toggle agent mode, clear "
            "history, or asks about current settings, costs, or available models."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "get",
                        "list_models",
                        "set_model",
                        "set_provider",
                        "set_agent_mode",
                        "set_rounds",
                        "clear_history",
                        "new_conversation",
                    ],
                    "description": "The config action to perform",
                },
                "value": {
                    "type": "string",
                    "description": "The value to set (model name, provider name, on/off, number)",
                },
            },
            "required": ["action"],
        },
    },
}


class ConchConfigClient:
    """Built-in tool that lets the LLM read and modify Conch's own config."""

    name = "conch_config"

    def __init__(self):
        self._provider = ""
        self._model = ""
        self._session_usage = {}
        self.pending_actions: List[tuple] = []

    def bind(self, provider: str, model: str, session_usage: dict):
        self._provider = provider
        self._model = model
        self._session_usage = session_usage

    def update(self, provider: str, model: str):
        self._provider = provider
        self._model = model

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def call_tool(self, name: str, arguments: dict) -> dict:
        from .providers import KNOWN_MODELS, MODEL_PRICING, DEFAULT_API_KEY_ENVS
        import os

        action = arguments.get("action", "get")
        value = arguments.get("value", "").strip()

        if action == "get":
            agent = "ON" if get_agent_mode() else "OFF"
            lines = [
                f"provider: {self._provider}",
                f"model: {self._model}",
                f"agent_mode: {agent}",
            ]
            u = self._session_usage
            if u.get("turns"):
                lines.append(f"session_turns: {u['turns']}")
                lines.append(f"session_input_tokens: {u.get('input_tokens', 0):,}")
                lines.append(f"session_output_tokens: {u.get('output_tokens', 0):,}")
                cost = u.get("cost", 0)
                lines.append(f"session_cost: ${cost:.4f}" if cost > 0.0001 else "session_cost: free")
            return self._text("\n".join(lines))

        if action == "list_models":
            lines = []
            for prov, models in KNOWN_MODELS.items():
                key_env = DEFAULT_API_KEY_ENVS.get(prov, "")
                available = not key_env or bool(os.environ.get(key_env, "").strip())
                status = "available" if available else "no API key"
                lines.append(f"\n{prov} ({status}):")
                for m in models:
                    price = MODEL_PRICING.get(m, (0, 0))
                    current = " <-- current" if m == self._model else ""
                    if price[0] == 0 and price[1] == 0:
                        lines.append(f"  {m}  free{current}")
                    else:
                        lines.append(f"  {m}  ${price[0]:.2f}/${price[1]:.2f} per 1M tok (in/out){current}")
            return self._text("\n".join(lines))

        if action == "set_model":
            if not value:
                return self._text("Error: provide a model name in 'value'")
            target_provider = None
            for prov, models in KNOWN_MODELS.items():
                if value in models:
                    target_provider = prov
                    break
            if not target_provider:
                return self._text(f"Unknown model '{value}'. Use action=list_models to see options.")
            key_env = DEFAULT_API_KEY_ENVS.get(target_provider, "")
            if key_env and not os.environ.get(key_env, "").strip():
                return self._text(f"Cannot switch to {target_provider}/{value}: {key_env} not set.")
            self.pending_actions.append(("set_model", target_provider, value))
            price = MODEL_PRICING.get(value, (0, 0))
            cost_str = "free" if price == (0, 0) else f"${price[0]:.2f}/${price[1]:.2f} per 1M tokens"
            return self._text(f"Switched to {target_provider}/{value} ({cost_str}).")

        if action == "set_provider":
            if not value:
                return self._text("Error: provide a provider name in 'value'")
            value = value.lower()
            if value not in KNOWN_MODELS:
                return self._text(f"Unknown provider '{value}'. Options: {', '.join(KNOWN_MODELS)}")
            key_env = DEFAULT_API_KEY_ENVS.get(value, "")
            if key_env and not os.environ.get(key_env, "").strip():
                return self._text(f"Cannot switch to {value}: {key_env} not set.")
            default_model = KNOWN_MODELS[value][0]
            self.pending_actions.append(("set_model", value, default_model))
            return self._text(f"Switched to {value}/{default_model}.")

        if action == "set_agent_mode":
            enabled = value.lower() in ("on", "true", "1", "yes")
            set_agent_mode(enabled)
            return self._text(f"Agent mode {'ON' if enabled else 'OFF'}.")

        if action == "set_rounds":
            try:
                n = int(value)
                if n < 1:
                    raise ValueError
            except (ValueError, TypeError):
                return self._text(f"Invalid number: '{value}'")
            self.pending_actions.append(("set_rounds", n))
            return self._text(f"Max tool rounds set to {n}.")

        if action == "clear_history":
            self.pending_actions.append(("clear_history",))
            return self._text("Conversation history cleared.")

        if action == "new_conversation":
            self.pending_actions.append(("new_conversation",))
            return self._text("New conversation started.")

        return self._text(f"Unknown action: {action}")


# ---------------------------------------------------------------------------
# public_api — search ~1400 free APIs and call no-auth ones directly
# ---------------------------------------------------------------------------

PUBLIC_API_TOOL = {
    "type": "function",
    "function": {
        "name": "public_api",
        "description": (
            "Search 1400+ free public APIs or call no-auth APIs directly. "
            "Use for discovering APIs, getting live data (weather, crypto, "
            "jokes, facts, translations, etc.), or helping users find APIs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "categories", "call"],
                    "description": "search: find APIs by keyword. categories: list all categories. call: make an HTTP request.",
                },
                "query": {
                    "type": "string",
                    "description": "Search keywords (for search action)",
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category name",
                },
                "auth": {
                    "type": "string",
                    "enum": ["any", "none", "apiKey", "OAuth"],
                    "description": "Filter by auth type (default: any)",
                },
                "url": {
                    "type": "string",
                    "description": "URL to call (for call action)",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST"],
                    "description": "HTTP method (default GET)",
                },
                "params": {
                    "type": "object",
                    "description": "Query parameters or POST body",
                },
            },
            "required": ["action"],
        },
    },
}


class PublicApiClient:
    """Built-in tool for searching and calling public APIs."""

    name = "public_api"

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def call_tool(self, name: str, arguments: dict) -> dict:
        from . import public_apis

        action = arguments.get("action", "search")

        if action == "categories":
            cats = public_apis.get_categories()
            if not cats:
                return self._text("Failed to load API catalog. Try again later.")
            lines = [f"Public API categories ({sum(cats.values())} APIs total):\n"]
            for cat, count in cats.items():
                lines.append(f"  {cat}: {count}")
            return self._text("\n".join(lines))

        if action == "search":
            query = arguments.get("query", "")
            auth = arguments.get("auth", "any")
            category = arguments.get("category", "")
            results = public_apis.search(
                query=query,
                auth_filter=auth,
                category_filter=category,
            )
            if not results:
                return self._text(f"No APIs found matching '{query}'.")
            lines = [f"Found {len(results)} APIs:\n"]
            for api in results:
                auth_tag = f" [auth: {api['auth']}]" if api["auth"] != "none" else " [no auth]"
                lines.append(
                    f"  {api['name']} -- {api['description']}\n"
                    f"    {api['url']}{auth_tag}  ({api['category']})"
                )
            return self._text("\n".join(lines))

        if action == "call":
            url = arguments.get("url", "")
            method = arguments.get("method", "GET")
            params = arguments.get("params")
            if not url:
                return self._text("Error: 'url' is required for the call action.")
            result = public_apis.call_api(url, method, params)
            return self._text(result)

        return self._text(f"Unknown action: {action}")
