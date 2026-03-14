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
PINNED_TOOL_NAMES = {"local_shell", "manage_tools", "save_memory"}

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
    all_tools.extend([LOCAL_SHELL_TOOL, MANAGE_TOOLS_TOOL, SAVE_MEMORY_TOOL])
    tool_map["local_shell"] = clients["local_shell"]
    tool_map["manage_tools"] = clients["manage_tools"]
    tool_map["save_memory"] = clients["save_memory"]

