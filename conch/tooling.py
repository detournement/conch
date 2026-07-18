"""Tool filtering and built-in tool clients."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


MAX_GROUP_TOOLS = 200
MAX_ACTIVE_TOOLS = 300
PINNED_TOOL_NAMES = {
    "local_shell", "manage_tools", "save_memory", "public_api", "conch_config",
    "search_conversations", "api_layer", "todo_list", "delegate_task",
    "skill_manage",
}

TOOL_PREFS_PATH = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch" / "tool_prefs.json"

_agent_mode = False


def set_agent_mode(enabled: bool):
    global _agent_mode
    _agent_mode = enabled


def get_agent_mode() -> bool:
    return _agent_mode


# ---------------------------------------------------------------------------
# Permission model (plan 2.1): graded modes + prefix allowlists + a
# destructive-command check that prompts even in agent mode.
# ---------------------------------------------------------------------------

PERMISSION_MODES = ("prompt_all", "safe_auto", "yolo")
_permission_mode = "prompt_all"


def set_permission_mode(mode: str):
    global _permission_mode
    normalized = (mode or "").strip().lower().replace("-", "_")
    if normalized in PERMISSION_MODES:
        _permission_mode = normalized


def get_permission_mode() -> str:
    """Effective mode: agent mode (from /agent, A, or config) means yolo."""
    if _agent_mode:
        return "yolo"
    return _permission_mode


# Read-only commands auto-approved in safe_auto mode. Matched against the
# whole command only when it contains no shell chaining/substitution, so a
# safe prefix can't smuggle a second command.
SAFE_COMMAND_PREFIXES = (
    "ls", "pwd", "cat", "head", "tail", "wc", "echo", "date", "cal",
    "whoami", "id", "uname", "hostname", "uptime", "df", "du", "stat",
    "file", "which", "whereis", "printenv", "ps", "tree", "grep", "rg",
    "find", "fd",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git stash list",
)

_SHELL_CHAIN_RE = re.compile(r"[;&|`><]|\$\(")

_DESTRUCTIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\brm\b", r"\brmdir\b", r"\bmkfs", r"\bdd\b", r"\bshred\b",
    r"\btruncate\b", r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"\bpoweroff\b", r"\bkillall\b", r"\bpkill\b",
    r"git\s+push\s+[^\n]*(-f\b|--force)", r"git\s+reset\s+--hard",
    r"git\s+clean\b", r"git\s+checkout\s+\.\s*$",
    r"\bchmod\s+-r\b", r"\bchown\s+-r\b",
    r">\s*/dev/(sd|disk|nvme)", r"\bdrop\s+(table|database)\b",
    r":\s*\(\s*\)\s*\{",
)]


def is_destructive_command(cmd: str) -> bool:
    """True for commands that can destroy data or take down the machine.
    These prompt for confirmation even in agent/yolo mode."""
    return any(p.search(cmd or "") for p in _DESTRUCTIVE_PATTERNS)


def is_safe_command(cmd: str) -> bool:
    """True for plain read-only commands (no chaining/redirection)."""
    text = (cmd or "").strip()
    if not text or _SHELL_CHAIN_RE.search(text):
        return False
    return any(
        text == prefix or text.startswith(prefix + " ")
        for prefix in SAFE_COMMAND_PREFIXES
    )


# ---------------------------------------------------------------------------
# Lifecycle hooks (plan 2.2): user shell scripts run around the agent loop.
# Config keys: hook_pre_tool_use, hook_post_tool_use, hook_on_turn_end.
# The payload arrives as JSON on stdin. For pre_tool_use a non-zero exit
# blocks the tool call (stderr/stdout becomes the reason) and stdout that
# parses as a JSON object rewrites the tool arguments.
# ---------------------------------------------------------------------------

HOOK_EVENTS = ("pre_tool_use", "post_tool_use", "on_turn_end")
HOOK_TIMEOUT = 10


def get_hook_command(event: str, config: Optional[dict]) -> str:
    return str((config or {}).get(f"hook_{event}", "") or "").strip()


def run_hook(event: str, payload: dict, config: Optional[dict], timeout: int = HOOK_TIMEOUT) -> tuple:
    """Run the configured hook for *event* with *payload* as JSON on stdin.

    Returns (allowed, output): allowed is False only when the hook exists and
    exits non-zero (deterministic gate); output is the hook's stdout (or the
    block reason). Hooks that are missing, crash, or time out are permissive
    — a broken hook must not brick the agent loop.
    """
    script = get_hook_command(event, config)
    if not script:
        return True, ""
    try:
        proc = subprocess.run(
            script,
            shell=True,
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        print(f"  \033[33m⚠ {event} hook failed to run: {exc}\033[0m", file=sys.stderr)
        return True, ""
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        reason = proc.stderr.decode("utf-8", errors="replace").strip() or stdout
        return False, reason
    return True, stdout


# Multi-word prefix for tools whose first word says nothing by itself.
_SUBCOMMAND_TOOLS = {
    "git", "docker", "kubectl", "npm", "pnpm", "yarn", "pip", "pip3",
    "brew", "cargo", "apt", "systemctl", "gh", "helm", "terraform",
}


def command_prefix(cmd: str) -> str:
    """The allowlist key for a command: first token, or first two tokens for
    subcommand-style tools (so 'a' on `git status` doesn't allow `git push`)."""
    tokens = (cmd or "").strip().split()
    if not tokens:
        return ""
    if tokens[0] in _SUBCOMMAND_TOOLS and len(tokens) > 1:
        return f"{tokens[0]} {tokens[1]}"
    return tokens[0]


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


def select_relevant_tools(tools: List[dict], query: str, limit: int) -> List[dict]:
    """Pick at most *limit* tools, ranked by relevance to the user's turn.

    Pinned tools always survive; the remaining slots go to tools whose
    name/description overlaps the query's keywords (ties keep original
    order). Replaces the old order-based truncation, which kept whatever
    happened to be first in the list.
    """
    if limit <= 0 or len(tools) <= limit:
        return tools
    pinned: List[dict] = []
    others: List[dict] = []
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        (pinned if name in PINNED_TOOL_NAMES else others).append(tool)
    slots = limit - len(pinned)
    if slots <= 0:
        return pinned[:limit]
    words = {w for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 2}
    scored = []
    for idx, tool in enumerate(others):
        fn = tool.get("function", {})
        haystack = (fn.get("name", "") + " " + fn.get("description", "")).lower()
        score = sum(1 for w in words if w in haystack)
        scored.append((-score, idx, tool))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [tool for _, _, tool in scored[:slots]] + pinned


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


def config_profiles(config: Optional[dict]) -> Dict[str, Dict[str, Any]]:
    """Profiles defined in ~/.config/conch/config as ``profile_<name> =
    group1, group2`` lines (plan 1.6: user-definable named group sets)."""
    profiles: Dict[str, Dict[str, Any]] = {}
    for key, value in (config or {}).items():
        if not key.startswith("profile_"):
            continue
        name = key[len("profile_"):].strip().lower()
        if not name:
            continue
        groups = {g.strip().lower() for g in str(value).split(",") if g.strip()}
        profiles[name] = {
            "description": f"Config-defined ({', '.join(sorted(groups)) or 'no groups'})",
            "groups": groups,
        }
    return profiles


def list_profiles(config: Optional[dict] = None) -> Dict[str, Dict[str, Any]]:
    """Return builtin + config-defined + prefs-defined profiles."""
    prefs = load_tool_prefs()
    merged = dict(BUILTIN_PROFILES)
    merged.update(config_profiles(config))
    merged.update(prefs.get("custom_profiles", {}))
    return merged


def active_profile_name() -> str:
    prefs = load_tool_prefs()
    return prefs.get("active_profile", "")


def _profile_disabled_groups(profile: Dict[str, Any], all_groups: set) -> List[str]:
    wanted = profile.get("groups")
    if wanted == "__all__":
        return []
    if wanted is None:
        return sorted(all_groups - PINNED_TOOL_NAMES)
    if isinstance(wanted, list):
        wanted = set(wanted)
    return sorted(all_groups - wanted - PINNED_TOOL_NAMES)


def profile_tool_filter(
    name: str,
    all_tools: List[dict],
    tool_map: Dict[str, Any],
    config: Optional[dict] = None,
) -> tuple[Optional[List[dict]], str]:
    """Compute the tool list a profile would produce, without persisting
    prefs. Returns (tools, description) or (None, error message).

    Used for session-only activation (the ollama minimal default and the
    ``tool_profile`` config key) so an automatic choice never overwrites the
    user's saved preferences.
    """
    profiles = list_profiles(config)
    profile = profiles.get(name)
    if not profile:
        return None, f"Unknown profile '{name}'. Use /profiles to list."
    all_groups = set(group_tools(all_tools, tool_map).keys())
    prefs = {"disabled_groups": _profile_disabled_groups(profile, all_groups)}
    tools = cap_tools(apply_filter(all_tools, tool_map, prefs))
    return tools, profile.get("description", name)


def activate_profile(
    name: str,
    all_tools: List[dict],
    tool_map: Dict[str, Any],
    config: Optional[dict] = None,
) -> tuple[List[dict], str]:
    """Activate a profile and return (filtered_tools, description).

    Sets disabled_groups in prefs so that only the profile's groups (plus
    pinned tools) are active.  Returns the new active tool list.
    """
    profiles = list_profiles(config)
    profile = profiles.get(name)
    if not profile:
        return [], f"Unknown profile '{name}'. Use /profiles to list."

    prefs = load_tool_prefs()
    all_groups = set(group_tools(all_tools, tool_map).keys())
    prefs["disabled_groups"] = _profile_disabled_groups(profile, all_groups)
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
        "description": "Persist a durable fact, user preference, or piece of context to memory so it can be recalled in later sessions.",
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

    # Default char budget for command output handed to the LLM; app.py scales
    # this to the active model's context window (see set_result_budget).
    DEFAULT_RESULT_BUDGET = 15000

    def __init__(self):
        self.policy = LocalShellPolicy()
        self._allowed_prefixes: set[str] = set()
        self._result_budget = self.DEFAULT_RESULT_BUDGET

    def set_policy(self, policy: LocalShellPolicy):
        self.policy = policy

    def set_result_budget(self, budget_chars: int):
        if budget_chars > 0:
            self._result_budget = budget_chars

    def allow_prefixes(self, prefixes):
        """Seed the always-allow list (config key allow_prefixes)."""
        for prefix in prefixes or []:
            prefix = str(prefix).strip()
            if prefix:
                self._allowed_prefixes.add(prefix)

    def _prefix_allowed(self, cmd: str) -> bool:
        prefix = command_prefix(cmd)
        return bool(prefix) and prefix in self._allowed_prefixes

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
        if len(output) > self._result_budget:
            from .runtime import truncate_middle
            output = truncate_middle(output, self._result_budget)
        return self._text(output)

    def call_tool(self, name: str, arguments: dict) -> dict:
        cmd = arguments.get("command", "")
        timeout = int(arguments.get("timeout", 60))
        if not cmd:
            return self._text("Error: empty command")

        from .render import clear_active_spinners
        clear_active_spinners()
        print(f"\n  \033[1;33m\u26a0 Run locally:\033[0m \033[1m{cmd}\033[0m", flush=True)

        destructive = is_destructive_command(cmd)
        mode = get_permission_mode()
        auto_execute = self.policy.allow_auto_execute or mode == "yolo"

        # Decide whether this command may run without a prompt.
        if destructive:
            # Destructive commands always prompt, even in agent/yolo mode.
            auto = False
        elif auto_execute:
            auto = True
        elif self._prefix_allowed(cmd):
            auto = True
        elif mode == "safe_auto" and is_safe_command(cmd):
            auto = True
        else:
            auto = False

        if auto:
            if auto_execute:
                print("  \033[2m(agent mode \u2014 auto-executing)\033[0m")
            elif self._prefix_allowed(cmd):
                print(f"  \033[2m(always-allowed: {command_prefix(cmd)})\033[0m")
            else:
                print("  \033[2m(safe command \u2014 auto-approved)\033[0m")
            return self._run_command(cmd, timeout)

        if not self.policy.interactive:
            if destructive:
                return self._text(
                    "Refused: destructive commands require interactive confirmation."
                )
            return self._text("Background tasks cannot prompt for local command confirmation.")

        if destructive:
            print("  \033[1;31m\u26a0 Destructive command \u2014 confirmation required"
                  " (even in agent mode)\033[0m")

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
                    "    \033[1ma\033[0m          Always allow commands with this prefix (session)\n"
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
            prefix = command_prefix(cmd)
            if prefix and not destructive:
                self._allowed_prefixes.add(prefix)
                print(f"  \033[2m(will auto-approve '{prefix} ...' this session)\033[0m")
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


# ---------------------------------------------------------------------------
# Plan/todo scratchpad (plan 2.5): session state the runtime re-injects into
# every round *outside* compactable history, keeping small models on track
# across long tool sequences.
# ---------------------------------------------------------------------------

TODO_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "todo_list",
        "description": (
            "Track your plan for multi-step tasks. The current list is "
            "re-shown to you every round, surviving history compaction. "
            "Use add/complete as you work; keep items short."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "complete", "remove", "clear", "list"],
                },
                "item": {"type": "string", "description": "Item text (for add)"},
                "items": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Several items to add at once",
                },
                "id": {"type": "integer", "description": "Item id (for complete/remove)"},
            },
            "required": ["action"],
        },
    },
}


class TodoListClient:
    """Session-scoped plan scratchpad (never persisted, never compacted)."""

    name = "todo_list"
    MAX_ITEMS = 30

    def __init__(self):
        self._items: List[Dict[str, Any]] = []
        self._next_id = 1

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def render(self) -> str:
        """Current state as the block injected each round ("" when empty)."""
        if not self._items:
            return ""
        lines = ["[Current plan — todo_list]"]
        for item in self._items:
            mark = "x" if item["done"] else " "
            lines.append(f"  [{mark}] #{item['id']} {item['text']}")
        return "\n".join(lines)

    def _add(self, texts: List[str]) -> int:
        added = 0
        for text in texts:
            text = str(text).strip()
            if not text or len(self._items) >= self.MAX_ITEMS:
                continue
            self._items.append({"id": self._next_id, "text": text, "done": False})
            self._next_id += 1
            added += 1
        return added

    def call_tool(self, name: str, arguments: dict) -> dict:
        action = (arguments.get("action") or "list").lower()
        if action == "add":
            texts = arguments.get("items") or []
            if arguments.get("item"):
                texts = [arguments["item"]] + list(texts)
            added = self._add(texts)
            if not added:
                return self._text("Error: nothing added (provide 'item' or 'items')")
        elif action == "complete":
            item = next((i for i in self._items if i["id"] == arguments.get("id")), None)
            if not item:
                return self._text(f"Error: no item #{arguments.get('id')}")
            item["done"] = True
        elif action == "remove":
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != arguments.get("id")]
            if len(self._items) == before:
                return self._text(f"Error: no item #{arguments.get('id')}")
        elif action == "clear":
            self._items = []
        elif action != "list":
            return self._text(f"Unknown action: {action}")
        return self._text(self.render() or "(todo list empty)")

    def clear(self):
        self._items = []


# Tool definition for the delegate_task subagent (plan 3.1 + 4.2); the
# client lives below with the runtime wiring.
DELEGATE_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegate a self-contained subtask to a fresh subagent with clean "
            "context and its own tool budget. It returns only a concise "
            "summary — use this to keep large exploration or multi-step side "
            "work out of your own context. Subagents run one at a time. "
            "Pass 'skill' to run a skill-scoped subagent: it gets that "
            "skill's instructions, allowed tools, and model preference."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Complete, self-contained task description",
                },
                "context": {
                    "type": "string",
                    "description": "Optional extra context the subagent needs",
                },
                "skill": {
                    "type": "string",
                    "description": "Optional skill name scoping the subagent "
                                   "(see skill_manage action='list')",
                },
            },
            "required": ["task"],
        },
    },
}


# ---------------------------------------------------------------------------
# Skill management (plan 4.1): list/use/save/delete skills from
# ~/.config/conch/skills/. 'use' injects the skill body into context (the
# tool result is the injection); 'save' is the in-chat skill builder with a
# human-in-the-loop confirmation before anything is written.
# ---------------------------------------------------------------------------

SKILL_MANAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "skill_manage",
        "description": (
            "Manage and use skills (reusable procedures in "
            "~/.config/conch/skills/). action='use' loads a skill's "
            "instructions into context — do this before performing a task a "
            "skill covers. action='save' creates/updates a skill: when the "
            "user asks to turn a procedure you just performed into a skill, "
            "draft the name/description/body (steps, commands, pitfalls, "
            "verification) and save it — the user confirms before anything "
            "is written."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "use", "save", "delete"],
                },
                "name": {"type": "string", "description": "Skill name (lowercase, dashes ok)"},
                "description": {"type": "string", "description": "One-line description (for save)"},
                "body": {
                    "type": "string",
                    "description": "Skill instructions in markdown (for save): "
                                   "steps, commands, pitfalls, verification",
                },
                "tools": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Tools the skill is allowed to use (for save; omit = all)",
                },
                "model": {"type": "string", "description": "Optional model preference (for save)"},
            },
            "required": ["action"],
        },
    },
}


class SkillManageClient:
    """Skill loader/builder tool. Saving and deleting are human-in-the-loop:
    the user reviews the skill and confirms before the file is touched."""

    name = "skill_manage"

    def __init__(self):
        self._interactive = True
        self._input_fn = None

    def configure(self, interactive: bool = True, input_fn=None):
        self._interactive = interactive
        self._input_fn = input_fn

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def _confirm(self, prompt: str) -> bool:
        _input = self._input_fn or input
        try:
            answer = _input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("", "y", "yes")

    def call_tool(self, name: str, arguments: dict) -> dict:
        from . import skills as skills_mod

        action = (arguments.get("action") or "list").lower()

        if action == "list":
            skills = skills_mod.load_skills()
            if not skills:
                return self._text(
                    "No skills defined yet. Save one with action='save', or "
                    f"drop markdown files in {skills_mod.skills_dir()}."
                )
            lines = ["Available skills:"]
            for skill_name, skill in sorted(skills.items()):
                scope = "all tools" if skill["tools"] is None else ", ".join(skill["tools"])
                model = f", model={skill['model']}" if skill["model"] else ""
                lines.append(f"- {skill_name}: {skill['description'] or '(no description)'} "
                             f"[tools: {scope}{model}]")
            return self._text("\n".join(lines))

        if action == "use":
            skill = skills_mod.get_skill(arguments.get("name", ""))
            if skill is None:
                return self._text(
                    f"Unknown skill '{arguments.get('name', '')}'. Use action='list'."
                )
            return self._text(
                skills_mod.render_skill(skill)
                + "\n\nFollow this skill's procedure for the current task."
            )

        if action == "save":
            skill_name = (arguments.get("name") or "").strip().lower()
            body = (arguments.get("body") or "").strip()
            if not skill_name or not skills_mod.SKILL_NAME_RE.fullmatch(skill_name):
                return self._text("Error: provide a valid 'name' (lowercase, digits, - or _)")
            if not body:
                return self._text("Error: provide the skill 'body' (the procedure)")
            description = (arguments.get("description") or "").strip()
            tools = arguments.get("tools") or None
            if tools is not None:
                tools = [str(t).strip() for t in tools if str(t).strip()] or None
            model = (arguments.get("model") or "").strip()
            if not self._interactive:
                return self._text("Error: saving skills requires an interactive session.")
            existing = skills_mod.get_skill(skill_name)
            verb = "Update" if existing else "Save new"
            preview = skills_mod.format_skill_file(skill_name, description, body, tools, model)
            print(f"\n  \033[1;33m{verb} skill '{skill_name}':\033[0m")
            for line in preview.splitlines()[:30]:
                print(f"    \033[2m{line}\033[0m")
            if len(preview.splitlines()) > 30:
                print(f"    \033[2m... ({len(preview.splitlines()) - 30} more lines)\033[0m")
            if not self._confirm(f"  \033[1;33m{verb} skill? [y/N]\033[0m "):
                return self._text("User declined to save the skill.")
            path = skills_mod.save_skill(skill_name, description, body, tools, model)
            return self._text(f"Saved skill '{skill_name}' to {path}. "
                              f"Use it with skill_manage action='use' or /skill {skill_name}.")

        if action == "delete":
            skill_name = (arguments.get("name") or "").strip().lower()
            if skills_mod.get_skill(skill_name) is None:
                return self._text(f"Unknown skill '{skill_name}'.")
            if not self._interactive:
                return self._text("Error: deleting skills requires an interactive session.")
            if not self._confirm(f"  \033[1;33mDelete skill '{skill_name}'? [y/N]\033[0m "):
                return self._text("User declined to delete the skill.")
            skills_mod.delete_skill(skill_name)
            return self._text(f"Deleted skill '{skill_name}'.")

        return self._text(f"Unknown action: {action}")


class DelegateTaskClient:
    """delegate_task subagent (plan 3.1): runs a fresh chat_turn with clean
    context, a narrowed toolset, and its own round budget, returning only a
    summary to the parent.

    Subagents run strictly serially — on a single Ollama server a second
    concurrent model load competes for VRAM and can evict the parent's model
    (losing its KV cache). Default model is the parent's; a smaller/faster
    model can be configured with ``subagent_model``.
    """

    name = "delegate_task"

    SUBAGENT_PROMPT = (
        "You are a Conch subagent handling one delegated subtask with a "
        "fresh, clean context. Work autonomously with the available tools. "
        "When finished, reply with a concise summary of what you did and "
        "found (results, key facts, file paths, commands) — the parent agent "
        "sees ONLY your final reply."
    )

    # Tools the subagent never gets: itself (no recursive delegation) and
    # self-management tools that belong to the parent session.
    EXCLUDED_TOOLS = {"delegate_task", "conch_config", "manage_tools", "todo_list"}

    DEFAULT_ROUNDS = 10

    def __init__(self):
        import threading
        self._config: dict = {}
        self._chat_state = None
        self._builtin_clients: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def bind(self, config: dict, chat_state, builtin_clients: Dict[str, Any]):
        """Bind live references: config/provider/tools are read at call time
        so mid-session model switches carry over to subagents."""
        self._config = config
        self._chat_state = chat_state
        self._builtin_clients = builtin_clients

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def _subagent_config(self, preferred_model: str = "", preferred_provider: str = "") -> tuple:
        """(config copy, provider) for the subagent.

        Model preference order: the skill's model (plan 4.2) beats the
        configured subagent_model beats the parent's model. Ollama models are
        capability-gated (plan 0.4); unusable preferences fall back to the
        parent's model with a warning.
        """
        from .providers import RAW_FNS

        config = dict(self._config)
        provider = (config.get("provider") or "").lower()
        if preferred_provider and preferred_provider in RAW_FNS:
            provider = preferred_provider
            config["provider"] = preferred_provider
        sub_model = (preferred_model or config.get("subagent_model") or "").strip()
        if sub_model:
            usable = True
            if provider == "ollama":
                from .providers import validate_ollama_model
                ok, reason = validate_ollama_model(sub_model, config)
                if ok is not True:
                    usable = False
                    print(f"  \033[33m⚠ subagent model '{sub_model}' unusable "
                          f"({reason or 'unverified'}) — using parent model\033[0m",
                          file=sys.stderr)
            if usable:
                config["model"] = sub_model
                config["chat_model"] = sub_model
        return config, provider

    def call_tool(self, name: str, arguments: dict) -> dict:
        task = (arguments.get("task") or "").strip()
        if not task:
            return self._text("Error: 'task' is required")
        if self._chat_state is None:
            return self._text("Error: delegate_task not initialized")
        skill = None
        skill_name = (arguments.get("skill") or "").strip().lower()
        if skill_name:
            from . import skills as skills_mod
            skill = skills_mod.get_skill(skill_name)
            if skill is None:
                return self._text(
                    f"Error: unknown skill '{skill_name}' — use skill_manage "
                    "action='list' to see available skills."
                )
        # Serialize: one subagent at a time (single local GPU).
        if not self._lock.acquire(blocking=False):
            return self._text(
                "Error: another subagent is already running — subagents run "
                "one at a time. Finish or wait, then retry."
            )
        try:
            return self._run(task, (arguments.get("context") or "").strip(), skill)
        finally:
            self._lock.release()

    def _run(self, task: str, context: str, skill: Optional[dict] = None) -> dict:
        from .config import get_int
        from .providers import RAW_FNS
        from .runtime import chat_turn, truncate_tool_result

        config, provider = self._subagent_config(
            preferred_model=(skill or {}).get("model", ""),
            preferred_provider=(skill or {}).get("provider", ""),
        )
        raw_fn = RAW_FNS.get(provider)
        if raw_fn is None:
            return self._text(f"Error: unknown provider '{provider}'")
        rounds = (skill or {}).get("rounds", 0) or get_int(
            config, "subagent_rounds", self.DEFAULT_ROUNDS
        )

        if skill is not None and skill.get("tools") is not None:
            # Skill-scoped toolset (plan 4.2): only the skill's allowed tools
            # (minus the always-excluded self-management set), drawn from the
            # full loaded tool list so profile filtering doesn't hide them.
            allowed = set(skill["tools"]) - self.EXCLUDED_TOOLS
            pool = (getattr(self._chat_state, "all_tools", None)
                    or getattr(self._chat_state, "tools", None) or [])
            sub_tools = [
                t for t in pool
                if t.get("function", {}).get("name") in allowed
            ]
            sub_clients = {
                k: v for k, v in self._builtin_clients.items() if k in allowed
            }
        else:
            pool = getattr(self._chat_state, "tools", None) or []
            sub_tools = [
                t for t in pool
                if t.get("function", {}).get("name") not in self.EXCLUDED_TOOLS
            ]
            sub_clients = {
                k: v for k, v in self._builtin_clients.items()
                if k not in self.EXCLUDED_TOOLS
            }
        system_prompt = self.SUBAGENT_PROMPT
        if skill is not None:
            from . import skills as skills_mod
            system_prompt += (
                "\n\nYou are running the following skill — follow its "
                "procedure:\n\n" + skills_mod.render_skill(skill)
            )
        user_content = task if not context else f"{task}\n\nContext:\n{context}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        label = f"subagent[{skill['name']}]" if skill else "subagent"
        print(f"  \033[2m({label}: {task[:70]})\033[0m", file=sys.stderr)
        try:
            reply, usage = chat_turn(
                config,
                provider,
                raw_fn,
                messages,
                sub_tools or None,
                getattr(self._chat_state, "tool_map", {}) or {},
                sub_clients,
                max_tool_rounds=rounds,
            )
        except Exception as exc:
            return self._text(f"Error: subagent failed: {exc}")
        if not (reply or "").strip():
            return self._text("Subagent finished without a summary (likely a provider error).")
        reply = truncate_tool_result(reply, provider, config)
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        footer = f"\n\n(subagent used {in_tok:,} in / {out_tok:,} out tokens)" if in_tok or out_tok else ""
        return self._text(f"Subagent summary:\n{reply}{footer}")


# ---------------------------------------------------------------------------
# Executable tools directory (plan 2.3): any executable in
# ~/.config/conch/tools/ becomes a tool. `<exe> --schema` must print a JSON
# object with name/description/parameters; invocation passes the arguments
# as JSON on stdin and stdout becomes the tool result.
# ---------------------------------------------------------------------------

USER_TOOL_TIMEOUT = 60
USER_TOOL_SCHEMA_TIMEOUT = 5
_USER_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def user_tools_dir() -> Path:
    return _config_dir() / "tools"


class UserToolClient:
    """Single client fronting every executable user tool (group: "user")."""

    name = "user"

    def __init__(self):
        self._executables: Dict[str, str] = {}

    def register(self, tool_name: str, path: str):
        self._executables[tool_name] = path

    def call_tool(self, name: str, arguments: dict) -> dict:
        path = self._executables.get(name)
        if not path:
            return {"content": [{"type": "text", "text": f"Error: unknown user tool '{name}'"}]}
        try:
            proc = subprocess.run(
                [path],
                input=json.dumps(arguments or {}).encode(),
                capture_output=True,
                timeout=USER_TOOL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"content": [{"type": "text", "text": f"Error: user tool '{name}' timed out after {USER_TOOL_TIMEOUT}s"}]}
        except OSError as exc:
            return {"content": [{"type": "text", "text": f"Error: user tool '{name}' failed to run: {exc}"}]}
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            parts = [text, stderr, f"(exit code {proc.returncode})"]
            text = "\n".join(p for p in parts if p)
        return {"content": [{"type": "text", "text": text or "(no output)"}]}


def discover_user_tools() -> tuple[List[dict], UserToolClient]:
    """Probe every executable in the user tools dir with --schema."""
    client = UserToolClient()
    tools: List[dict] = []
    directory = user_tools_dir()
    if not directory.is_dir():
        return tools, client
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        try:
            proc = subprocess.run(
                [str(path), "--schema"],
                capture_output=True,
                timeout=USER_TOOL_SCHEMA_TIMEOUT,
            )
            schema = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        except Exception:
            print(f"  \033[33m⚠ user tool {path.name}: --schema failed, skipped\033[0m",
                  file=sys.stderr)
            continue
        if not isinstance(schema, dict):
            continue
        tool_name = str(schema.get("name") or path.stem)
        if not _USER_TOOL_NAME_RE.fullmatch(tool_name):
            continue
        parameters = schema.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        tools.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": str(schema.get("description", "")),
                "parameters": parameters,
            },
        })
        client.register(tool_name, str(path))
    return tools, client


def inject_builtin_tools(all_tools: List[dict], tool_map: Dict[str, Any], clients: Dict[str, Any]):
    builtin = [LOCAL_SHELL_TOOL, MANAGE_TOOLS_TOOL, SAVE_MEMORY_TOOL, PUBLIC_API_TOOL, SEARCH_CONVERSATIONS_TOOL]
    if "conch_config" in clients:
        builtin.append(CONCH_CONFIG_TOOL)
    if "api_layer" in clients:
        builtin.append(API_LAYER_TOOL)
    if "todo_list" in clients:
        builtin.append(TODO_LIST_TOOL)
    if "delegate_task" in clients:
        builtin.append(DELEGATE_TASK_TOOL)
    if "skill_manage" in clients:
        builtin.append(SKILL_MANAGE_TOOL)
    all_tools.extend(builtin)
    for tool_def in builtin:
        name = tool_def["function"]["name"]
        if name in clients:
            tool_map[name] = clients[name]
    # Executable user tools (plan 2.3), grouped as "user" for profiles
    user_tool_defs, user_client = discover_user_tools()
    for tool_def in user_tool_defs:
        all_tools.append(tool_def)
        tool_map[tool_def["function"]["name"]] = user_client



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
        self._config: dict = {}
        self.pending_actions: List[tuple] = []

    def bind(self, provider: str, model: str, session_usage: dict, config: Optional[dict] = None):
        self._provider = provider
        self._model = model
        self._session_usage = session_usage
        if config is not None:
            self._config = config

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
            get_fallback_model,
            get_ollama_base_url,
            list_ollama_models,
            ollama_model_matches,
            validate_ollama_model,
        )
        import os

        action = arguments.get("action", "get")
        value = arguments.get("value", "").strip()

        if action == "get":
            from .config import get_config_path
            from .providers import get_context_window
            agent = "ON" if get_agent_mode() else "OFF"
            lines = [
                f"provider: {self._provider}",
                f"model: {self._model}",
                f"context_window: {get_context_window(self._provider, self._model, self._config):,} tokens",
                f"agent_mode: {agent}",
                f"config_file: {get_config_path()}",
            ]
            if self._provider == "ollama":
                lines.append(f"ollama_base_url: {get_ollama_base_url(self._config)}")
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
                if prov == "ollama":
                    models = list_ollama_models(self._config)
                    if models is None:
                        lines.append(f"\nollama (unreachable at {get_ollama_base_url(self._config)}): no models")
                        continue
                    if not models:
                        lines.append("\nollama (reachable): no tool-capable models installed")
                        continue
                lines.append(f"\n{prov} ({status}):")
                for m in models:
                    price = MODEL_PRICING.get(m, (0, 0))
                    is_current = m == self._model or (
                        prov == "ollama" and self._provider == "ollama"
                        and ollama_model_matches(self._model, [m])
                    )
                    current = " <-- current" if is_current else ""
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
                if prov != "ollama" and value in models:
                    target_provider = prov
                    break
            if not target_provider:
                ok, reason = validate_ollama_model(value, self._config)
                if ok:
                    target_provider = "ollama"
                elif ok is False and "tool calling" in reason:
                    return self._text(f"Cannot switch: {reason}.")
                elif self._provider == "ollama":
                    if ok is None:
                        return self._text(f"Cannot verify model '{value}': {reason}.")
                    return self._text(f"Cannot switch: {reason}. Use action=list_models to see options.")
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
            default_model = get_fallback_model(value, self._config)
            if value == "custom" and not default_model:
                return self._text(
                    "Cannot switch to custom: set custom_base_url and "
                    "custom_model in ~/.config/conch/config first."
                )
            if value == "ollama" and not default_model:
                if list_ollama_models(self._config) is None:
                    return self._text(
                        f"Cannot switch to ollama: server unreachable at {get_ollama_base_url(self._config)}."
                    )
                return self._text("Cannot switch to ollama: no tool-capable models installed on the server.")
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
