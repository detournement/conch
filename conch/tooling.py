"""Tool filtering and built-in tool clients."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


MAX_GROUP_TOOLS = 200
MAX_ACTIVE_TOOLS = 300
PINNED_TOOL_NAMES = {"local_shell", "manage_tools", "save_memory", "public_api", "conch_config", "search_conversations", "api_layer"}

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
    input_fn: Any = None


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
        "description": "Persist a fact, preference, token, API key, credential, or URL to memory so it can be recalled later.",
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
        self._allowed_commands: set[str] = set()

    def set_policy(self, policy: LocalShellPolicy):
        self.policy = policy

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def _run_command(self, cmd: str, timeout: int) -> dict:
        import os, pty, select, errno, re as _re
        effective_timeout = timeout if timeout > 0 else 60

        # Use a PTY so interactive programs (sudo, passwd, ssh, expect) get a
        # real terminal — prevents dropped characters and hanging prompts.
        master_fd, slave_fd = pty.openpty()
        output_parts: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd, shell=True,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid,
            )
            os.close(slave_fd)
            slave_fd = -1
            deadline = time.time() + effective_timeout
            try:
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        proc.kill()
                        output_parts.append(f"\nCommand timed out after {effective_timeout}s")
                        break
                    try:
                        rlist, _, _ = select.select([master_fd], [], [], min(remaining, 0.5))
                    except (select.error, ValueError):
                        break
                    if master_fd in rlist:
                        try:
                            data = os.read(master_fd, 4096)
                        except OSError as e:
                            if e.errno in (errno.EIO, errno.EBADF):
                                break
                            raise
                        if not data:
                            break
                        chunk = data.decode("utf-8", errors="replace")
                        output_parts.append(chunk)
                        sys.stderr.write(f"    \033[2m{chunk.rstrip()}\033[0m\n")
                        sys.stderr.flush()
                    if proc.poll() is not None:
                        try:
                            while True:
                                r2, _, _ = select.select([master_fd], [], [], 0.1)
                                if not r2:
                                    break
                                data = os.read(master_fd, 4096)
                                if not data:
                                    break
                                chunk = data.decode("utf-8", errors="replace")
                                output_parts.append(chunk)
                        except OSError:
                            pass
                        break
            except KeyboardInterrupt:
                proc.kill()
                return self._text("Command interrupted by user.")
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass
            if slave_fd != -1:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass

        proc.wait()
        output = "".join(output_parts)
        output = _re.sub(r"\r", "\n", output)
        output = _re.sub(r"\x1b\[[0-9;]*[mABCDEFGHJKLMSTfhilnprsu]", "", output)
        output = _re.sub(r"\n{3,}", "\n\n", output).strip()

        if not output:
            output = f"(no output, exit code {proc.returncode})"
        elif proc.returncode != 0:
            output += f"\n(exit code {proc.returncode})"
        if len(output) > 15000:
            output = output[:15000] + "\n... (truncated)"
        return self._text(output)

    def call_tool(self, name: str, arguments: dict) -> dict:
        cmd = arguments.get("command", "")
        timeout = int(arguments.get("timeout", 60))
        if not cmd:
            return self._text("Error: empty command")

        from .render import clear_active_spinners
        clear_active_spinners()
        print(f"\n  \033[1;33m\u26a0 Run locally:\033[0m \033[1m{cmd}\033[0m", flush=True)

        auto_execute = self.policy.allow_auto_execute or get_agent_mode()
        if self.policy.interactive and not auto_execute:
            if cmd in self._allowed_commands:
                print("  \033[2m(always-allowed)\033[0m")
                return self._run_command(cmd, timeout)

            _input = self.policy.input_fn or input
            while True:
                try:
                    sys.stdout.flush()
                    answer = _input("  \033[1;33mExecute? [y/n/e/a/A/?]\033[0m ").strip()
                except (EOFError, KeyboardInterrupt):
                    answer = ""

                if answer == "?":
                    print(
                        "    \033[1my\033[0m / \033[1mEnter\033[0m  Run this command\n"
                        "    \033[1mn\033[0m          Decline (with optional feedback to the LLM)\n"
                        "    \033[1me\033[0m          Edit the command before running\n"
                        "    \033[1ma\033[0m          Always allow this exact command (session)\n"
                        "    \033[1mA\033[0m          Turn on agent mode (auto-execute everything)"
                    )
                    continue
                break

            if answer.lower() in ("", "y", "yes"):
                return self._run_command(cmd, timeout)

            if answer == "A":
                set_agent_mode(True)
                self.policy = LocalShellPolicy(
                    interactive=self.policy.interactive,
                    allow_auto_execute=True,
                    input_fn=self.policy.input_fn,
                )
                print("  \033[1;32mAgent mode: ON\033[0m \u2014 all commands will auto-execute")
                return self._run_command(cmd, timeout)

            if answer.lower() in ("a", "always"):
                self._allowed_commands.add(cmd)
                return self._run_command(cmd, timeout)

            if answer.lower() in ("e", "edit"):
                try:
                    edited = _input("  \033[1;33mCommand:\033[0m ").strip()
                except (EOFError, KeyboardInterrupt):
                    edited = ""
                cmd = edited or cmd
                print(f"  \033[2m\u2192 {cmd}\033[0m")
                return self._run_command(cmd, timeout)

            # n / anything else = decline; ask for optional feedback
            try:
                feedback = _input("  \033[2mReason (optional):\033[0m ").strip()
            except (EOFError, KeyboardInterrupt):
                feedback = ""
            msg = "User declined to execute the command."
            if feedback:
                msg += f" Feedback: {feedback}"
            return self._text(msg)

        elif not self.policy.interactive and not auto_execute:
            return self._text("Background tasks cannot prompt for local command confirmation.")
        else:
            print("  \033[2m(agent mode \u2014 auto-executing)\033[0m")
            return self._run_command(cmd, timeout)


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


SEARCH_CONVERSATIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_conversations",
        "description": (
            "Search through all past conversations, saved memories, and Conch config "
            "files (~/.config/conch/*) for specific topics, commands, tokens, credentials, "
            "or information discussed previously. Returns matching snippets with context. "
            "Use this when the user asks 'what did we talk about', 'find that conversation "
            "where...', 'what was the command for...', 'find my token/key', or any recall question. "
            "Config files (config, mcp.json) are always searched automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms (space-separated keywords)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum conversations to return (default 10)",
                },
            },
            "required": ["query"],
        },
    },
}


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"


def _search_config_files(keywords: List[str]) -> List[dict]:
    """Search all files in ~/.config/conch/ for keyword matches."""
    config_dir = _config_dir()
    if not config_dir.is_dir():
        return []
    hits: List[dict] = []
    for path in sorted(config_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        text_lower = text.lower()
        hit_count = sum(text_lower.count(kw) for kw in keywords)
        if not hit_count:
            continue
        matching_lines = []
        for line in text.splitlines():
            if any(kw in line.lower() for kw in keywords):
                matching_lines.append(line.strip())
        hits.append({
            "file": path.name,
            "path": str(path),
            "hits": hit_count,
            "lines": matching_lines[:10],
        })
    return hits


class SearchConversationsClient:
    name = "search_conversations"

    def __init__(self):
        self._conv_mgr = None
        self._memory = None

    def bind(self, conv_mgr, memory=None):
        self._conv_mgr = conv_mgr
        self._memory = memory

    def call_tool(self, name: str, arguments: dict) -> dict:
        if not self._conv_mgr:
            return {"content": [{"type": "text", "text": "Error: conversation manager not initialized"}]}
        query = arguments.get("query", "").strip()
        if not query:
            return {"content": [{"type": "text", "text": "Error: empty search query"}]}
        keywords = query.lower().split()
        max_results = int(arguments.get("max_results", 10))

        sections: List[str] = []

        config_hits = _search_config_files(keywords)
        if config_hits:
            sections.append("## Config files (~/.config/conch/)\n")
            for h in config_hits:
                sections.append(f"**{h['file']}** ({h['hits']} matches):")
                for line in h["lines"]:
                    sections.append(f"  {line}")
                sections.append("")

        if self._memory:
            mem_entries = self._memory.get_all()
            mem_hits = []
            for entry in mem_entries:
                content = str(entry.get("content", ""))
                content_lower = content.lower()
                count = sum(content_lower.count(kw) for kw in keywords)
                if count:
                    mem_hits.append((count, entry))
            if mem_hits:
                mem_hits.sort(key=lambda x: x[0], reverse=True)
                sections.append("## Memories\n")
                for _, entry in mem_hits[:5]:
                    sections.append(f"  #{entry['id']}: {entry['content']}")
                sections.append("")

        results = self._conv_mgr.search(query, max_results=max_results)
        if results:
            sections.append(f"## Conversations ({len(results)} match{'es' if len(results) != 1 else ''})\n")
            for r in results:
                sections.append(f"### {r['title']} (id: {r['id']}, {r['message_count']} msgs)")
                for m in r["matches"][:5]:
                    sections.append(f"  [{m['role']}]: {m['snippet']}")
                sections.append("")

        if not sections:
            return {"content": [{"type": "text", "text": f"No results found matching '{query}' in conversations, memories, or config files."}]}
        header = f"Search results for '{query}':\n\n"
        return {"content": [{"type": "text", "text": header + "\n".join(sections)}]}


def inject_builtin_tools(all_tools: List[dict], tool_map: Dict[str, Any], clients: Dict[str, Any]):
    builtin = [LOCAL_SHELL_TOOL, MANAGE_TOOLS_TOOL, SAVE_MEMORY_TOOL, PUBLIC_API_TOOL, SEARCH_CONVERSATIONS_TOOL]
    if "conch_config" in clients:
        builtin.append(CONCH_CONFIG_TOOL)
    if "api_layer" in clients:
        builtin.append(API_LAYER_TOOL)
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
        from .providers import (
            KNOWN_MODELS,
            MODEL_PRICING,
            DEFAULT_API_KEY_ENVS,
            DEFAULT_CHAT_MODEL_BY_PROVIDER,
        )
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
            if value == self._model and target_provider == self._provider:
                return self._text(f"Already using {target_provider}/{value}. No change needed.")
            key_env = DEFAULT_API_KEY_ENVS.get(target_provider, "")
            if key_env and not os.environ.get(key_env, "").strip():
                return self._text(f"Cannot switch to {target_provider}/{value}: {key_env} not set.")
            self.pending_actions.append(("set_model", target_provider, value))
            price = MODEL_PRICING.get(value, (0, 0))
            cost_str = "free" if price == (0, 0) else f"${price[0]:.2f}/${price[1]:.2f} per 1M tokens"
            return self._text(
                f"Model switch to {target_provider}/{value} ({cost_str}) is queued. "
                f"IMPORTANT: this response is still generated by {self._provider}/{self._model}. "
                f"The switch takes effect starting with the NEXT user message. "
                f"Tell the user the switch will take effect on their next message."
            )

        if action == "set_provider":
            if not value:
                return self._text("Error: provide a provider name in 'value'")
            value = value.lower()
            if value not in KNOWN_MODELS:
                return self._text(f"Unknown provider '{value}'. Options: {', '.join(KNOWN_MODELS)}")
            if value == self._provider:
                return self._text(f"Already using provider {value}/{self._model}. No change needed.")
            key_env = DEFAULT_API_KEY_ENVS.get(value, "")
            if key_env and not os.environ.get(key_env, "").strip():
                return self._text(f"Cannot switch to {value}: {key_env} not set.")
            default_model = DEFAULT_CHAT_MODEL_BY_PROVIDER.get(value) or (
                KNOWN_MODELS[value][0] if KNOWN_MODELS.get(value) else ""
            )
            self.pending_actions.append(("set_model", value, default_model))
            return self._text(
                f"Provider switch to {value}/{default_model} is queued. "
                f"IMPORTANT: this response is still generated by {self._provider}/{self._model}. "
                f"The switch takes effect starting with the NEXT user message. "
                f"Tell the user the switch will take effect on their next message."
            )

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


# ---------------------------------------------------------------------------
# api_layer — authenticated access to APILayer marketplace APIs
# ---------------------------------------------------------------------------

_APILAYER_APIS = {
    "exchangerates_data": {
        "desc": "Exchange rates & currency conversion (170+ currencies)",
        "endpoints": "/latest, /convert, /symbols, /{date}, /timeseries, /fluctuation",
    },
    "fixer": {
        "desc": "Foreign exchange rates (Fixer)",
        "endpoints": "/latest, /convert, /symbols, /{date}, /timeseries, /fluctuation",
    },
    "currency_data": {
        "desc": "Currency data & conversion",
        "endpoints": "/live, /convert, /historical, /timeframe, /change, /list",
    },
    "number_verification": {
        "desc": "Phone number validation & lookup",
        "endpoints": "/validate?number=<number>",
    },
    "bad_words": {
        "desc": "Profanity/content moderation filter",
        "endpoints": "/bad_words?text=<text>",
    },
    "ip_to_location": {
        "desc": "IP address geolocation",
        "endpoints": "/check, /check?ip=<ip>",
    },
    "weatherstack": {
        "desc": "Weather data (current, historical, forecast)",
        "endpoints": "/current?query=<city>, /historical, /forecast",
    },
    "mediastack": {
        "desc": "Live news & headlines",
        "endpoints": "/news?keywords=<q>&languages=en",
    },
    "aviationstack": {
        "desc": "Flight tracking & aviation data",
        "endpoints": "/flights, /airports, /airlines",
    },
    "countrylayer": {
        "desc": "Country information (capital, population, languages)",
        "endpoints": "/name/{name}, /alpha/{code}, /all",
    },
    "vat_layer": {
        "desc": "EU VAT number validation",
        "endpoints": "/validate?vat_number=<number>",
    },
}

API_LAYER_TOOL = {
    "type": "function",
    "function": {
        "name": "api_layer",
        "description": (
            "Call APILayer marketplace APIs (authenticated). Available APIs: "
            "exchangerates_data (currency rates/conversion), fixer (forex), "
            "currency_data, number_verification (phone lookup), bad_words "
            "(content moderation), ip_to_location (IP geolocation), weatherstack "
            "(weather), mediastack (news), aviationstack (flights), countrylayer "
            "(country info), vat_layer (EU VAT). Use 'list' to see all APIs and "
            "endpoints, or 'call' to make an authenticated request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "call"],
                    "description": "list: show available APIs and endpoints. call: make an API request.",
                },
                "api": {
                    "type": "string",
                    "description": "API name (e.g. 'exchangerates_data', 'weatherstack'). For call action.",
                },
                "endpoint": {
                    "type": "string",
                    "description": "API endpoint path (e.g. '/latest', '/convert'). For call action.",
                },
                "params": {
                    "type": "object",
                    "description": "Query parameters as key-value pairs (e.g. {\"from\": \"USD\", \"to\": \"EUR\", \"amount\": 100})",
                },
            },
            "required": ["action"],
        },
    },
}


class ApiLayerClient:
    """Built-in tool for calling APILayer marketplace APIs."""

    name = "api_layer"

    def __init__(self):
        self._api_key = ""

    def bind(self, api_key: str):
        self._api_key = api_key

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def call_tool(self, name: str, arguments: dict) -> dict:
        from . import public_apis

        action = arguments.get("action", "list")

        if action == "list":
            lines = ["APILayer APIs available:\n"]
            for api_name, info in _APILAYER_APIS.items():
                lines.append(f"  {api_name}")
                lines.append(f"    {info['desc']}")
                lines.append(f"    Endpoints: {info['endpoints']}")
                lines.append(f"    Base: https://api.apilayer.com/{api_name}")
                lines.append("")
            lines.append("Use action='call' with api=<name> endpoint=<path> params={...}")
            return self._text("\n".join(lines))

        if action == "call":
            if not self._api_key:
                return self._text("Error: API_LAYER_KEY not configured. Add it to ~/.config/conch/config")
            api = arguments.get("api", "").strip()
            endpoint = arguments.get("endpoint", "").strip()
            params = arguments.get("params")
            if not api:
                return self._text("Error: 'api' is required (e.g. 'exchangerates_data')")
            if not endpoint:
                return self._text("Error: 'endpoint' is required (e.g. '/latest')")
            if not endpoint.startswith("/"):
                endpoint = "/" + endpoint

            url = f"https://api.apilayer.com/{api}{endpoint}"
            headers = {"apikey": self._api_key}
            result = public_apis.call_api(url, "GET", params, headers=headers)
            return self._text(result)

        return self._text(f"Unknown action: {action}")
