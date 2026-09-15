"""Tool filtering and built-in tool clients."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


MAX_GROUP_TOOLS = 200
MAX_ACTIVE_TOOLS = 300
PINNED_TOOL_NAMES = {
    # Keep the always-on schema budget deliberately small. Everything else is
    # routed by relevance and can still be pinned explicitly by a profile.
    "local_shell",
    "interactive_terminal",
    "ssh_remote",
    "manage_tools",
    "todo_list",
    "delegate_task",
    "skill_manage",
    "conch_introspect",
}

TOOL_PREFS_PATH = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch" / "tool_prefs.json"

# ---------------------------------------------------------------------------
# Permission model (plan 2.1): graded modes + prefix allowlists + a
# destructive-command check that prompts even in agent mode.
#
# Swarm Phase 0: agent/permission mode is per-session state (PermissionState)
# instead of module globals. The module-level set/get helpers operate on the
# process-default instance, so the interactive CLI and existing callers keep
# their exact behavior, while concurrent sessions (scheduled, remote,
# delegated, future controller work) own private instances via AgentSession.
# ---------------------------------------------------------------------------

PERMISSION_MODES = ("prompt_all", "safe_auto", "yolo")


class PermissionState:
    """Agent/permission mode owned by one session."""

    __slots__ = ("_agent_mode", "_permission_mode")

    def __init__(self, agent_mode: bool = False,
                 permission_mode: str = "prompt_all"):
        self._agent_mode = bool(agent_mode)
        self._permission_mode = "prompt_all"
        self.set_permission_mode(permission_mode)

    def set_agent_mode(self, enabled: bool):
        self._agent_mode = bool(enabled)

    def get_agent_mode(self) -> bool:
        return self._agent_mode

    def set_permission_mode(self, mode: str):
        normalized = (mode or "").strip().lower().replace("-", "_")
        if normalized in PERMISSION_MODES:
            self._permission_mode = normalized

    def get_permission_mode(self) -> str:
        """Effective mode: agent mode (from /agent, A, or config) means yolo."""
        if self._agent_mode:
            return "yolo"
        return self._permission_mode


_DEFAULT_PERMISSIONS = PermissionState()


def default_permissions() -> PermissionState:
    """The process-default permission state used by the interactive CLI."""
    return _DEFAULT_PERMISSIONS


def set_agent_mode(enabled: bool):
    _DEFAULT_PERMISSIONS.set_agent_mode(enabled)


def get_agent_mode() -> bool:
    return _DEFAULT_PERMISSIONS.get_agent_mode()


def set_permission_mode(mode: str):
    _DEFAULT_PERMISSIONS.set_permission_mode(mode)


def get_permission_mode() -> str:
    """Effective mode of the process-default session."""
    return _DEFAULT_PERMISSIONS.get_permission_mode()


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

REMOTE_SAFE_COMMAND_PREFIXES = (
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "echo",
    "date",
    "cal",
    "whoami",
    "id",
    "uname",
    "hostname",
    "uptime",
    "df",
    "du",
    "stat",
    "printenv",
    "ps",
    "grep",
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
    try:
        tokens = shlex.split(text)
    except ValueError:
        return False
    lowered = {token.lower() for token in tokens[1:]}
    if tokens:
        executable = tokens[0].lower()
        if executable == "find" and any(
            token in lowered
            for token in (
                "-delete",
                "-exec",
                "-execdir",
                "-ok",
                "-okdir",
                "-fprint",
                "-fprint0",
                "-fprintf",
                "-fls",
            )
        ):
            return False
        if executable == "fd" and any(
            token in lowered
            for token in ("-x", "-X", "--exec", "--exec-batch")
        ):
            return False
        if executable in ("rg", "ripgrep") and any(
            token == "--pre" or token.startswith("--pre=")
            for token in lowered
        ):
            return False
        if executable == "git" and any(
            token in ("--ext-diff", "--textconv") for token in lowered
        ):
            return False
    return any(
        text == prefix or text.startswith(prefix + " ")
        for prefix in SAFE_COMMAND_PREFIXES
    )


def is_remote_safe_command(cmd: str) -> bool:
    """Narrow non-extensible subset safe for unattended remote sessions."""
    text = (cmd or "").strip()
    if not is_safe_command(text):
        return False
    return any(
        text == prefix or text.startswith(prefix + " ")
        for prefix in REMOTE_SAFE_COMMAND_PREFIXES
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
    if name in (
        "local_shell",
        "interactive_terminal",
        "ssh_remote",
        "manage_tools",
        "save_memory",
    ):
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
        if name in PINNED_TOOL_NAMES or grp not in disabled or name in picked:
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
        "description": "Core local-agent tools",
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
    # Tool-call drift tracking (see runtime.apply_tool_call_scaffolding):
    # count of textual tool calls seen this session, and a one-shot flag that
    # forces the corrective reminder on the next request (set by /resettools).
    textual_tool_calls: int = 0
    force_tool_reminder: bool = False


@dataclass
class LocalShellPolicy:
    interactive: bool = True
    allow_auto_execute: bool = False
    input_fn: Any = None
    handoff_context: Any = None
    tty_check: Any = None


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

INTERACTIVE_TERMINAL_TOOL = {
    "type": "function",
    "function": {
        "name": "interactive_terminal",
        "description": (
            "Run a local command by handing the real terminal directly to it. "
            "Use this instead of local_shell whenever sudo, SSH, getpass, a "
            "passphrase, or other interactive terminal input may be required. "
            "The user must explicitly confirm even in agent mode. Conch never "
            "reads or returns the interaction; only exit status is reported. "
            "Never put a credential in the command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Command to run. Credentials must be entered only at "
                        "the program's terminal prompt, never in this value."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Optional handoff timeout in seconds; 0 means no timeout"
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

SSH_REMOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "ssh_remote",
        "description": (
            "Manage a validated OpenSSH ControlMaster connection and execute "
            "commands through it. Actions: connect (direct terminal handoff "
            "for password/passphrase/host-key prompts), exec (captured, "
            "noninteractive, permission-gated), shell (direct terminal "
            "handoff; use for remote sudo), status, and disconnect. Interactive "
            "actions always require local confirmation and never return a "
            "transcript. Never put credentials in arguments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "connect",
                        "exec",
                        "shell",
                        "status",
                        "disconnect",
                    ],
                },
                "host": {
                    "type": "string",
                    "description": (
                        "Validated host/IP/SSH-config alias. Required for connect; "
                        "omit later to use the active connection."
                    ),
                },
                "user": {"type": "string"},
                "port": {"type": "integer"},
                "command": {
                    "type": "string",
                    "description": (
                        "Remote command for exec/shell. An empty shell command "
                        "opens an interactive login shell."
                    ),
                },
                "timeout": {"type": "integer"},
            },
            "required": ["action"],
            "additionalProperties": False,
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

    def __init__(self, permissions: Optional[PermissionState] = None):
        self.policy = LocalShellPolicy()
        self._allowed_prefixes: set[str] = set()
        self._result_budget = self.DEFAULT_RESULT_BUDGET
        self._permissions = permissions
        self._cwd: Optional[str] = None

    def bind_permissions(self, permissions: Optional[PermissionState]):
        """Adopt a session's permission state (None = process default)."""
        self._permissions = permissions

    def permissions(self) -> PermissionState:
        return self._permissions or _DEFAULT_PERMISSIONS

    def set_cwd(self, cwd):
        """Working directory for executed commands (None = process cwd)."""
        self._cwd = str(cwd) if cwd else None

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

    def _run_process(self, command, timeout: int, *, shell: bool) -> dict:
        import os
        import pty
        import select
        import errno
        import re as _re
        effective_timeout = timeout if timeout > 0 else 60

        # A PTY preserves useful terminal-formatted output, but stdin is
        # deliberately /dev/null and the child has no controlling terminal.
        # Credential prompts belong exclusively to interactive_terminal.
        master_fd, slave_fd = pty.openpty()
        capture_budget = max(256, int(self._result_budget))
        head_cap = max(1, int(capture_budget * 0.67))
        tail_cap = max(1, int(capture_budget * 0.23))
        captured_head = ""
        captured_tail = ""
        captured_total = 0

        def capture(chunk: str) -> None:
            nonlocal captured_head, captured_tail, captured_total
            captured_total += len(chunk)
            if len(captured_head) < head_cap:
                take = min(head_cap - len(captured_head), len(chunk))
                captured_head += chunk[:take]
                chunk = chunk[take:]
            if chunk:
                captured_tail = (captured_tail + chunk)[-tail_cap:]

        try:
            proc = subprocess.Popen(
                command, shell=shell, cwd=self._cwd,
                stdin=subprocess.DEVNULL, stdout=slave_fd, stderr=slave_fd,
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
                        capture(
                            f"\nCommand timed out after {effective_timeout}s"
                        )
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
                        capture(chunk)
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
                                capture(chunk)
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
        if captured_total <= len(captured_head) + len(captured_tail):
            output = captured_head + captured_tail
        else:
            omitted = captured_total - len(captured_head) - len(captured_tail)
            output = (
                captured_head
                + f"\n... [truncated {omitted:,} streamed chars] ...\n"
                + captured_tail
            )
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

    def _run_command(self, cmd: str, timeout: int) -> dict:
        return self._run_process(cmd, timeout, shell=True)

    def _run_argv(self, argv: List[str], timeout: int) -> dict:
        return self._run_process(list(argv), timeout, shell=False)

    def call_tool(self, name: str, arguments: dict) -> dict:
        cmd = arguments.get("command", "")
        timeout = int(arguments.get("timeout", 60))
        if not cmd:
            return self._text("Error: empty command")

        from .render import clear_active_spinners
        clear_active_spinners()
        print(f"\n  \033[1;33m\u26a0 Run locally:\033[0m \033[1m{cmd}\033[0m", flush=True)

        destructive = is_destructive_command(cmd)
        mode = self.permissions().get_permission_mode()
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
            self.permissions().set_agent_mode(True)
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


class InteractiveTerminalClient:
    """Credential-safe terminal handoff.

    The child owns the real terminal. Conch receives no child input/output and
    returns only a non-secret process-status summary.
    """

    name = "interactive_terminal"

    def __init__(self, runner=None):
        from .secure_terminal import DirectTerminalRunner

        self.policy = LocalShellPolicy()
        self._runner = runner or DirectTerminalRunner()

    def set_policy(self, policy: LocalShellPolicy):
        self.policy = policy

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    @staticmethod
    def _display(value: str, limit: int = 240) -> str:
        clean = "".join(
            ch
            if ch.isprintable() and ch != "\x7f"
            else f"\\x{ord(ch):02x}"
            for ch in str(value)
        )
        return (
            clean
            if limit <= 0 or len(clean) <= limit
            else clean[:limit] + "…"
        )

    def _configure_runner(self):
        from .secure_terminal import TerminalHandoffPolicy

        self._runner.set_policy(
            TerminalHandoffPolicy(
                local_session=self.policy.interactive,
                input_fn=self.policy.input_fn,
                handoff_context=self.policy.handoff_context,
                tty_check=self.policy.tty_check,
            )
        )

    def handoff_available(self) -> bool:
        """Whether this session may hand the terminal to a child right
        now — the same gate run_argv enforces (local foreground session
        with a real TTY). Lets callers like /notes refuse up front with
        a useful message instead of a failed handoff."""
        self._configure_runner()
        return bool(self._runner.available())

    def _format_result(self, result, noun: str = "Interactive command") -> dict:
        if not result.approved:
            if result.error:
                return self._text(f"Refused: {result.error}.")
            return self._text("User declined the interactive terminal handoff.")
        if result.error:
            return self._text(
                f"Error: {noun.lower()} failed to start ({result.error}). "
                "No terminal transcript was captured."
            )
        if result.timed_out:
            return self._text(
                f"{noun} timed out. No terminal input or output was captured."
            )
        if result.interrupted:
            return self._text(
                f"{noun} was interrupted by the user. No terminal input or "
                "output was captured."
            )
        return self._text(
            f"{noun} finished with exit code {result.returncode}. "
            "No terminal input or output was captured."
        )

    def run_argv(
        self,
        argv,
        *,
        description: str,
        timeout: int = 0,
        noun: str = "Interactive command",
    ):
        from .render import clear_active_spinners

        self._configure_runner()
        clear_active_spinners()
        result = self._runner.run(
            argv, description=description, timeout=max(0, int(timeout or 0))
        )
        return result, self._format_result(result, noun)

    def call_tool(self, name: str, arguments: dict) -> dict:
        from .ssh_control import validate_remote_command, SSHValidationError

        command = arguments.get("command", "")
        try:
            command = validate_remote_command(command)
            timeout = int(arguments.get("timeout", 0) or 0)
        except (SSHValidationError, TypeError, ValueError) as exc:
            return self._text(f"Refused: {exc}.")
        _, formatted = self.run_argv(
            ["/bin/sh", "-c", command],
            description=f"Hand terminal to: {self._display(command, limit=0)}?",
            timeout=timeout,
        )
        return formatted


class SSHRemoteClient(LocalShellClient):
    """OpenSSH ControlMaster operations with permission and TTY boundaries."""

    name = "ssh_remote"

    def __init__(self, manager=None, runner=None):
        from .secure_terminal import DirectTerminalRunner
        from .ssh_control import SSHControlManager

        super().__init__()
        self.manager = manager or SSHControlManager()
        self._terminal = InteractiveTerminalClient(
            runner=runner or DirectTerminalRunner()
        )

    def set_policy(self, policy: LocalShellPolicy):
        super().set_policy(policy)
        self._terminal.set_policy(policy)

    def close(self):
        self.manager.close()

    def _target(self, arguments: dict):
        return self.manager.resolve(
            host=str(arguments.get("host", "") or ""),
            user=str(arguments.get("user", "") or ""),
            port=arguments.get("port"),
        )

    def _require_connected(self, target):
        """None when a live master answers ``-O check``; honest error text
        otherwise. Reads registration before the probe purges stale state."""

        was_known = self.manager.is_known(target)
        if self.manager.is_connected(target):
            return None
        if was_known or self.manager.was_lost(target):
            # Registered but the -O check probe failed: the master died
            # (or its ControlPersist window elapsed) since we last spoke.
            return self._text(
                f"Error: SSH control connection to {target.identity} was "
                "lost (the control master no longer responds); reconnect "
                "interactively."
            )
        return self._text(
            f"Error: no active SSH control connection to {target.identity}; "
            "connect interactively first."
        )

    def _status_line(self, target) -> str:
        was_known = self.manager.is_known(target)
        if self.manager.is_connected(target):
            return f"SSH {target.identity}: connected."
        if was_known or self.manager.was_lost(target):
            return (
                f"SSH {target.identity}: connection lost (the control "
                "master no longer responds) — reconnect interactively."
            )
        return f"SSH {target.identity}: not connected."

    def _status(self, arguments: dict) -> dict:
        from .ssh_control import SSHValidationError

        if arguments.get("host"):
            try:
                target = self._target(arguments)
            except SSHValidationError as exc:
                return self._text(f"Error: {exc}")
            return self._text(self._status_line(target))
        try:
            target = self.manager.resolve()
        except SSHValidationError:
            targets = self.manager.connected_targets()
            if not targets:
                return self._text("No active SSH control connection.")
            return self._text(
                "Active SSH connections: "
                + ", ".join(target.identity for target in targets)
            )
        return self._text(self._status_line(target))

    def _connect(self, arguments: dict) -> dict:
        from .ssh_control import SSHValidationError, merge_ssh_target

        host = str(arguments.get("host", "") or "")
        if not host:
            return self._text("Error: host is required for SSH connect.")
        try:
            target = merge_ssh_target(
                host,
                str(arguments.get("user", "") or ""),
                arguments.get("port"),
            )
            timeout = int(arguments.get("timeout", 0) or 0)
        except (SSHValidationError, TypeError, ValueError) as exc:
            return self._text(f"Error: {exc}")
        if self.manager.is_connected(target):
            self.manager.remember(target)
            return self._text(
                f"SSH control connection to {target.identity} is already active."
            )
        try:
            connect_argv = self.manager.connect_argv(target)
        except OSError as exc:
            return self._text(f"Error: could not prepare SSH ControlPath ({exc}).")
        result, formatted = self._terminal.run_argv(
            connect_argv,
            description=(
                f"Open SSH control connection to {target.identity}? "
                "Authentication will occur directly in the terminal."
            ),
            timeout=timeout,
            noun="SSH connection bootstrap",
        )
        if not result.approved or result.error or result.timed_out or result.interrupted:
            self.manager.disconnect(target)
            return formatted
        if result.returncode == 0 and self.manager.is_connected(target):
            self.manager.remember(target)
            return self._text(
                f"SSH control connection to {target.identity} is active. "
                "Authentication input and terminal output were not captured."
            )
        self.manager.disconnect(target)
        return self._text(
            f"SSH connection bootstrap for {target.identity} did not establish "
            "a control socket. Review the terminal-only SSH message and retry; "
            "no terminal transcript was captured."
        )

    def _permission_allows(self, command: str) -> tuple:
        destructive = is_destructive_command(command)
        mode = self.permissions().get_permission_mode()
        auto_execute = self.policy.allow_auto_execute or mode == "yolo"
        if destructive:
            return False, "destructive"
        if auto_execute:
            return True, "agent mode"
        if self._prefix_allowed(command):
            return True, f"always-allowed: {command_prefix(command)}"
        if mode == "safe_auto" and is_safe_command(command):
            return True, "safe command"
        return False, ""

    def _confirm_remote_exec(self, target, command: str) -> tuple:
        allowed, reason = self._permission_allows(command)
        if allowed:
            print(f"  \033[2m({reason} — auto-approved)\033[0m")
            return True, ""
        if not self.policy.interactive:
            return False, (
                "Refused: background or channel sessions cannot approve SSH "
                "remote commands."
            )
        if is_destructive_command(command):
            print(
                "  \033[1;31m⚠ Destructive remote command — confirmation "
                "required (even in agent mode)\033[0m"
            )
        input_fn = self.policy.input_fn or input
        try:
            answer = input_fn(
                f"  \033[1;33mExecute on {target.identity}? [y/N]\033[0m "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            return False, "User declined the remote SSH command."
        return True, ""

    def _exec(self, arguments: dict) -> dict:
        from .ssh_control import validate_remote_command, SSHValidationError

        try:
            target = self._target(arguments)
            command = validate_remote_command(arguments.get("command", ""))
            timeout = int(arguments.get("timeout", 60) or 60)
        except (SSHValidationError, TypeError, ValueError) as exc:
            return self._text(f"Error: {exc}")
        failure = self._require_connected(target)
        if failure is not None:
            return failure
        print(
            f"\n  \033[1;33m⚠ Run over SSH ({target.identity}):\033[0m "
            f"\033[1m{self._terminal._display(command, limit=0)}\033[0m",
            flush=True,
        )
        approved, reason = self._confirm_remote_exec(target, command)
        if not approved:
            return self._text(reason)
        return self._run_argv(
            self.manager.exec_argv(target, command, tty=False), timeout
        )

    def _shell(self, arguments: dict) -> dict:
        from .ssh_control import validate_remote_command, SSHValidationError

        try:
            target = self._target(arguments)
            command = validate_remote_command(
                arguments.get("command", ""), allow_empty=True
            )
            timeout = int(arguments.get("timeout", 0) or 0)
        except (SSHValidationError, TypeError, ValueError) as exc:
            return self._text(f"Error: {exc}")
        failure = self._require_connected(target)
        if failure is not None:
            return failure
        description = (
            f"Open interactive SSH shell on {target.identity}?"
            if not command
            else (
                f"Hand terminal to {target.identity} for: "
                f"{self._terminal._display(command, limit=0)}?"
            )
        )
        _, formatted = self._terminal.run_argv(
            self.manager.exec_argv(target, command, tty=True),
            description=description,
            timeout=timeout,
            noun="Interactive SSH command",
        )
        return formatted

    def _disconnect(self, arguments: dict) -> dict:
        from .ssh_control import SSHValidationError

        try:
            target = self._target(arguments)
        except SSHValidationError as exc:
            return self._text(f"Error: {exc}")
        self._terminal._configure_runner()
        if not self._terminal._runner.available():
            return self._text(
                "Refused: SSH disconnect requires the local interactive session."
            )
        input_fn = self.policy.input_fn or input
        try:
            answer = input_fn(
                f"  \033[1;33mDisconnect SSH {target.identity}? [y/N]\033[0m "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            return self._text("User declined to disconnect SSH.")
        success = self.manager.disconnect(target)
        return self._text(
            f"SSH control connection to {target.identity} "
            f"{'closed' if success else 'is no longer active'}."
        )

    def call_tool(self, name: str, arguments: dict) -> dict:
        action = str(arguments.get("action", "") or "").strip().lower()
        if action == "status":
            return self._status(arguments)
        if action == "connect":
            return self._connect(arguments)
        if action == "exec":
            return self._exec(arguments)
        if action == "shell":
            return self._shell(arguments)
        if action == "disconnect":
            return self._disconnect(arguments)
        return self._text(f"Error: unknown SSH action '{action}'.")


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
        from .secretguard import CredentialRejected

        try:
            entry = self._memory.add(content, source="auto")
        except CredentialRejected as exc:
            return {"content": [{"type": "text", "text": (
                "Save blocked: the content matches credential pattern(s) "
                f"({', '.join(exc.types)}). Memory never stores secrets — "
                "the entry was rejected whole. Save a non-secret reference "
                "instead (e.g. which env var, keychain item, or config file "
                "holds the credential)."
            )}]}
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
            from .memory import credential_withheld as _memory_read_guard

            mem_entries = self._memory.get_all()
            mem_hits = []
            for entry in mem_entries:
                content = str(entry.get("content", ""))
                # Same scan-on-read guard as memory retrieval: a legacy
                # credential-bearing entry must not surface through search
                # either. (Config files are intentionally NOT guarded here
                # — finding the user's own configured keys is this tool's
                # documented purpose; the memory store must never hold any.)
                if _memory_read_guard(content, "search_conversations"):
                    continue
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


# ---------------------------------------------------------------------------
# Personal items (personal-items plan P1): the user's durable capture-and-
# recall store — todo lists, recipes, paper ideas — living in the mission
# kernel's `items` aggregate. Deliberately distinct from TodoListClient
# above: todo_list is the AGENT's in-session plan scratchpad (never
# persisted); personal_items is the USER's data, persisted and queried
# deterministically. Kernel imports stay inside call_tool so the classic
# no-daemon shell never loads conch.kernel until the tool is actually used.
# ---------------------------------------------------------------------------

PERSONAL_ITEMS_TOOL = {
    "type": "function",
    "function": {
        "name": "personal_items",
        "description": (
            "Manage the user's durable personal items: the todo list plus "
            "named spaces like recipes and papers, stored locally and kept "
            "across sessions. Use this when the user says things like 'add "
            "milk to the shopping list', 'todo: renew passport by Friday', "
            "'what's due today?', 'what's most urgent?', or 'mark X done'. "
            "Queries are deterministic (due today, overdue, most urgent, "
            "by space/tag/status) — relay the results as returned. This is "
            "the user's persistent data, not your in-session todo_list "
            "scratchpad."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "add", "update", "complete", "archive", "list",
                        "search", "show",
                    ],
                },
                "id": {
                    "type": "string",
                    "description": (
                        "Item reference for update/complete/archive/show: "
                        "the item id, a unique id prefix, or the #N alias "
                        "shown in listings."
                    ),
                },
                "space": {
                    "type": "string",
                    "description": (
                        "Item space (default 'todo'; e.g. recipes, papers; "
                        "new names create a space)."
                    ),
                },
                "title": {"type": "string", "description": "Item title (for add/update)"},
                "body": {
                    "type": "string",
                    "description": "Markdown body/notes (for add/update)",
                },
                "due": {
                    "type": "string",
                    "description": (
                        "Due (for add/update): today, tomorrow, +N[mhdw], "
                        "YYYY-MM-DD, 'YYYY-MM-DD HH:MM', or none to clear."
                    ),
                },
                "priority": {
                    "type": "integer",
                    "description": "Explicit priority 1 (highest) to 5",
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Tags (for add/update)",
                },
                "status": {
                    "type": "string",
                    "enum": ["open", "done", "archived", "all"],
                    "description": (
                        "list: status filter (default open). update: "
                        "'open' reopens a done/archived item."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "search: the text to find. list: optional view — "
                        "'today', 'overdue', or 'urgent'."
                    ),
                },
                "tag": {"type": "string", "description": "list: tag filter"},
                "limit": {"type": "integer", "description": "Max results"},
            },
            "required": ["action"],
        },
    },
}


class PersonalItemsClient:
    """Builtin fronting the kernel `items` aggregate.

    Opens the kernel store per call and closes it after — sessions come
    and go (remote turns build fresh ones per message) and holding a
    writer thread open across a chat session buys nothing. Item content
    returned here is the user's stored text: it is data, never
    instructions to follow.
    """

    name = "personal_items"

    def __init__(self):
        self._source = "chat"
        self._actor = "user"

    def configure(self, source: str = "chat", actor: str = "user"):
        """Provenance stamped on writes (channel wiring sets this in P2)."""
        self._source = str(source or "chat")
        self._actor = str(actor or "user")

    @staticmethod
    def _text(msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def call_tool(self, name: str, arguments: dict) -> dict:
        # Lazy kernel imports: the no-daemon invariant (classic shell
        # never loads conch.kernel) holds until this tool actually runs.
        from .kernel.model import KernelError
        from .kernel.store import MissionStore
        from .secretguard import CredentialRejected

        try:
            store = MissionStore()
        except Exception as exc:
            return self._text(
                f"personal_items error: cannot open the kernel store: {exc}"
            )
        try:
            return self._dispatch(store, arguments or {})
        except CredentialRejected as exc:
            return self._text(
                "Write blocked: the content matches credential pattern(s) "
                f"({', '.join(exc.types)}). Personal items never store "
                "secrets — the item was rejected whole. Save a non-secret "
                "reference instead (which env var, keychain item, or "
                "config file holds it)."
            )
        except KernelError as exc:
            return self._text(f"personal_items error: {exc}")
        finally:
            store.close()

    def _dispatch(self, store, arguments: dict) -> dict:
        import time as _time

        from .kernel import items as items_mod
        from .kernel.model import KernelError

        action = str(arguments.get("action") or "").strip().lower()
        now = _time.time()

        def resolve(required=True):
            ref = str(arguments.get("id") or "").strip()
            item = store.resolve_item(ref) if ref else None
            if item is None and required:
                raise KernelError(
                    f"no item matching {ref!r} — use action='list' or "
                    "'search' and reference items by #N or id prefix"
                )
            return item

        if action == "add":
            title = str(arguments.get("title") or "").strip()
            if not title:
                raise KernelError("add needs a title")
            item = store.add_item(
                title,
                space=arguments.get("space") or "",
                body=str(arguments.get("body") or ""),
                due_at=items_mod.parse_due(arguments.get("due"), now),
                priority=arguments.get("priority"),
                tags=arguments.get("tags"),
                source=self._source,
                actor=self._actor,
            )
            return self._text(
                "Added " + items_mod.item_line(item, now, with_space=True)
            )
        if action == "update":
            item = resolve()
            fields = {}
            for key in ("title", "body", "space", "status"):
                if arguments.get(key) is not None:
                    fields[key] = arguments[key]
            if arguments.get("due") is not None:
                fields["due_at"] = items_mod.parse_due(
                    arguments["due"], now
                )
            if arguments.get("priority") is not None:
                fields["priority"] = arguments["priority"]
            if arguments.get("tags") is not None:
                fields["tags"] = arguments["tags"]
            store.update_item(
                item["item_id"], fields,
                actor=self._actor, source=self._source,
            )
            updated = store.get_item(item["item_id"])
            return self._text(
                "Updated "
                + items_mod.item_line(updated, now, with_space=True)
            )
        if action in ("complete", "archive"):
            item = resolve()
            if action == "complete":
                store.complete_item(
                    item["item_id"], actor=self._actor,
                    source=self._source,
                )
                verb = "Completed"
            else:
                store.archive_item(
                    item["item_id"], actor=self._actor,
                    source=self._source,
                )
                verb = "Archived"
            updated = store.get_item(item["item_id"])
            return self._text(
                f"{verb} "
                + items_mod.item_line(updated, now, with_space=True)
            )
        if action == "show":
            item = resolve()
            return self._text(items_mod.item_detail(store, item, now))
        if action == "search":
            needle = str(arguments.get("query") or "").strip()
            if not needle:
                raise KernelError("search needs a query")
            rows = store.search_items(
                needle, space=arguments.get("space") or "",
                limit=int(arguments.get("limit") or 25),
            )
            if not rows:
                return self._text(f"No items matching {needle!r}.")
            lines = [f"{len(rows)} item(s) matching {needle!r}:"]
            lines.extend(
                items_mod.item_line(item, now, with_space=True)
                for item in rows
            )
            return self._text("\n".join(lines))
        if action == "list":
            space = arguments.get("space") or ""
            view = str(arguments.get("query") or "").strip().lower()
            limit = int(arguments.get("limit") or 50)
            if view in ("today", "due", "due_today"):
                rows = items_mod.due_today(store, now, space=space)
                label = "due today"
            elif view == "overdue":
                rows = items_mod.overdue(store, now, space=space)
                label = "overdue"
            elif view in ("urgent", "most_urgent"):
                rows = items_mod.most_urgent(
                    store, now, limit=limit, space=space
                )
                label = "most urgent"
            elif view:
                raise KernelError(
                    f"unknown list view {view!r} — use today, overdue,"
                    " or urgent"
                )
            else:
                rows = store.list_items(
                    space=space,
                    status=str(arguments.get("status") or "open"),
                    tag=str(arguments.get("tag") or ""),
                    limit=limit,
                )
                label = str(arguments.get("status") or "open")
            rows = rows[:limit]
            scope = f" in {space}" if space else ""
            if not rows:
                return self._text(f"No {label} items{scope}.")
            lines = [f"{len(rows)} {label} item(s){scope}:"]
            lines.extend(
                items_mod.item_line(item, now, with_space=not space)
                for item in rows
            )
            return self._text("\n".join(lines))
        return self._text(f"Unknown action: {action}")


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
    EXCLUDED_TOOLS = {
        "delegate_task",
        "conch_config",
        "manage_tools",
        "todo_list",
        "interactive_terminal",
        "ssh_remote",
    }

    # Personal-space and external-control tools never flow into a
    # delegated sub-turn implicitly. A skill that lists one in its
    # allowed tools is the operator's explicit offer (the fleet
    # analogue: a worker only sees a tool its task envelope names).
    IMPLICITLY_EXCLUDED_TOOLS = frozenset({
        "personal_items", "capitol_control", "fleet_delegate",
    })

    DEFAULT_ROUNDS = 10

    def __init__(self):
        import threading
        self._config: dict = {}
        self._chat_state = None
        self._builtin_clients: Dict[str, Any] = {}
        self._session = None
        self._lock = threading.Lock()

    def bind(self, config: dict, chat_state, builtin_clients: Dict[str, Any],
             session=None):
        """Bind live references: config/provider/tools are read at call time
        so mid-session model switches carry over to subagents."""
        self._config = config
        self._chat_state = chat_state
        self._builtin_clients = builtin_clients
        self._session = session

    def bind_session(self, session):
        """Bind a parent AgentSession; delegated turns become child sessions
        that share its permission state (child authority never exceeds the
        parent's)."""
        self.bind(
            session.config, session.chat_state, session.builtin_clients,
            session=session,
        )

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def _subagent_config(self, preferred_model: str = "", preferred_provider: str = "") -> tuple:
        """(config copy, provider) for the subagent.

        Model preference order: the skill's model (plan 4.2) beats the
        configured subagent_model beats the parent's model. Ollama models are
        capability-gated (plan 0.4); unusable preferences fall back to the
        parent's model with a warning.
        """
        from .config import local_only_enabled
        from .providers import (
            DEFAULT_API_KEY_ENVS,
            RAW_FNS,
            get_fallback_model,
            validate_model_for_provider,
        )

        config = dict(self._config)
        parent_provider = (config.get("provider") or "").lower()
        provider = parent_provider
        if preferred_provider and preferred_provider in RAW_FNS:
            if (
                local_only_enabled(config, parent_provider)
                and preferred_provider not in ("ollama", "custom")
            ):
                print(
                    f"  \033[33m⚠ subagent provider "
                    f"'{preferred_provider}' blocked by local_only — using "
                    f"{parent_provider}\033[0m",
                    file=sys.stderr,
                )
            else:
                candidate_model = get_fallback_model(
                    preferred_provider, config
                )
                if candidate_model:
                    provider = preferred_provider
                    config["provider"] = preferred_provider
                    config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(
                        preferred_provider, ""
                    )
                    config["model"] = candidate_model
                    config["chat_model"] = candidate_model
                else:
                    print(
                        f"  \033[33m⚠ subagent provider "
                        f"'{preferred_provider}' has no verified model — "
                        f"using {parent_provider}\033[0m",
                        file=sys.stderr,
                    )
        sub_model = (preferred_model or config.get("subagent_model") or "").strip()
        if sub_model:
            ok, reason = validate_model_for_provider(
                provider, sub_model, config
            )
            if ok is True:
                config["model"] = sub_model
                config["chat_model"] = sub_model
                if provider == "custom":
                    config["custom_model"] = sub_model
            else:
                print(f"  \033[33m⚠ subagent model '{sub_model}' unusable "
                      f"({reason or 'unverified'}) — using "
                      f"{config.get('chat_model') or 'parent model'}\033[0m",
                      file=sys.stderr)
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
        from .runtime import truncate_tool_result
        from .session import AgentSession

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
            excluded = self.EXCLUDED_TOOLS | self.IMPLICITLY_EXCLUDED_TOOLS
            pool = getattr(self._chat_state, "tools", None) or []
            sub_tools = [
                t for t in pool
                if t.get("function", {}).get("name") not in excluded
            ]
            sub_clients = {
                k: v for k, v in self._builtin_clients.items()
                if k not in excluded
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
        # Child session: shares the parent's permission state (a subagent's
        # authority is never wider than its parent's) but owns its own config
        # copy, toolset, and budget. Clients are the parent's objects, so they
        # keep the parent's bindings (attach with bind=False).
        parent = self._session
        child = AgentSession(
            config,
            interactive=False,
            permissions=(
                parent.permissions if parent is not None
                else default_permissions()
            ),
            cwd=(parent.cwd if parent is not None else None),
        )
        child.budgets.max_tool_rounds = rounds
        child.attach_clients(sub_clients, bind=False)
        try:
            reply, usage = child.run_turn(
                messages,
                tools=sub_tools or None,
                tool_map=getattr(self._chat_state, "tool_map", {}) or {},
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
    if "interactive_terminal" in clients:
        builtin.append(INTERACTIVE_TERMINAL_TOOL)
    if "ssh_remote" in clients:
        builtin.append(SSH_REMOTE_TOOL)
    if "conch_config" in clients:
        builtin.append(CONCH_CONFIG_TOOL)
    if "api_layer" in clients:
        builtin.append(API_LAYER_TOOL)
    if "todo_list" in clients:
        builtin.append(TODO_LIST_TOOL)
    if "personal_items" in clients:
        builtin.append(PERSONAL_ITEMS_TOOL)
    if "capitol_control" in clients:
        # Lazy import: only Capitol-configured sessions ever construct
        # the client (bootstrap), so only they pay for this module.
        from .capitol.tool import CAPITOL_SESSION_TOOL

        builtin.append(CAPITOL_SESSION_TOOL)
    if "delegate_task" in clients:
        builtin.append(DELEGATE_TASK_TOOL)
    if "fleet_delegate" in clients:
        # Lazy import: only fleet_controller-configured sessions ever
        # construct the client (bootstrap), so only they pay for this.
        from .fleet.delegate import FLEET_DELEGATE_TOOL

        builtin.append(FLEET_DELEGATE_TOOL)
    if "skill_manage" in clients:
        builtin.append(SKILL_MANAGE_TOOL)
    if "conch_introspect" in clients:
        builtin.append(CONCH_INTROSPECT_TOOL)
    if "llamaidx_registry" in clients:
        builtin.append(LLAMAIDX_REGISTRY_TOOL)
    builtin_names = {
        tool_def["function"]["name"] for tool_def in builtin
    }
    all_tools[:] = [
        tool_def
        for tool_def in all_tools
        if tool_def.get("function", {}).get("name") not in builtin_names
    ]
    for builtin_name in builtin_names:
        tool_map.pop(builtin_name, None)
    all_tools.extend(builtin)
    for tool_def in builtin:
        name = tool_def["function"]["name"]
        if name in clients:
            tool_map[name] = clients[name]
    # Executable user tools (plan 2.3), grouped as "user" for profiles
    user_tool_defs, user_client = discover_user_tools()
    for tool_def in user_tool_defs:
        tool_name = tool_def["function"]["name"]
        if tool_name in tool_map:
            continue
        all_tools.append(tool_def)
        tool_map[tool_name] = user_client



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
        self._permissions: Optional[PermissionState] = None
        self.pending_actions: List[tuple] = []

    def bind(self, provider: str, model: str, session_usage: dict, config: Optional[dict] = None):
        self._provider = provider
        self._model = model
        self._session_usage = session_usage
        if config is not None:
            self._config = config

    def bind_permissions(self, permissions: Optional[PermissionState]):
        self._permissions = permissions

    def permissions(self) -> PermissionState:
        return self._permissions or _DEFAULT_PERMISSIONS

    def update(self, provider: str, model: str):
        self._provider = provider
        self._model = model

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def _set_llamaidx_model(self, value: str) -> dict:
        """Queue a switch to a registry-discovered model.

        Mirrors the /model llamaidx/... path: the registry supplied
        flavor + base_url + model id, routing goes through the existing
        ollama/custom adapters, and conch's own probe-on-select runs here
        against the box before anything is queued — the registry's
        tools verdict gated the listing but can be stale.
        """
        from .llamaidx import (
            get_llamaidx_url,
            list_llamaidx_models,
            llamaidx_selection_overrides,
            resolve_llamaidx_model,
        )
        from .providers import validate_model_for_provider

        if not get_llamaidx_url(self._config):
            return self._text(
                "No llama-idx registry is configured (llamaidx_url is unset), "
                "so llamaidx/... names cannot be resolved."
            )
        entry = resolve_llamaidx_model(value, self._config, force_refresh=True)
        if entry is None:
            available = [
                e["name"] for e in list_llamaidx_models(self._config) or []
            ]
            shown = ", ".join(available[:6]) + (", ..." if len(available) > 6 else "")
            hint = (
                f" Registered tool-verified models: {shown}."
                if available
                else " The registry is unreachable or lists no tool-verified models."
            )
            return self._text(f"'{value}' is not in the registry catalog.{hint}")
        overrides = llamaidx_selection_overrides(entry)
        trial = dict(self._config)
        trial.update(overrides)
        ok, reason = validate_model_for_provider(
            overrides["provider"], entry["model_id"], trial
        )
        if ok is not True:
            return self._text(
                f"Cannot switch to {value}: {reason or 'validation failed'}. "
                "The registry lists it as tool-verified, but conch's own "
                "probe disagrees or the provider is unreachable — trust the "
                "probe."
            )
        self.pending_actions.append(
            ("set_model", overrides["provider"], entry["model_id"], overrides)
        )
        degraded = (
            " The provider is currently degraded (loading/recovering)."
            if entry["degraded"]
            else ""
        )
        return self._text(
            f"Model switch to {overrides['provider']}/{entry['model_id']} via "
            f"{entry['name']} at {entry['base_url']} (free, self-hosted) is "
            f"queued.{degraded} IMPORTANT: this response is still generated "
            f"by {self._provider}/{self._model}. The switch takes effect "
            "starting with the NEXT user message."
        )

    def call_tool(self, name: str, arguments: dict) -> dict:
        from .providers import (
            KNOWN_MODELS,
            MODEL_PRICING,
            DEFAULT_API_KEY_ENVS,
            get_fallback_model,
            get_custom_base_url,
            get_ollama_base_url,
            list_custom_models,
            list_ollama_models,
            ollama_model_matches,
            validate_model_for_provider,
        )
        import os

        action = arguments.get("action", "get")
        value = arguments.get("value", "").strip()

        if action == "get":
            from .config import get_config_path
            from .providers import get_context_window
            agent = "ON" if self.permissions().get_agent_mode() else "OFF"
            lines = [
                f"provider: {self._provider}",
                f"model: {self._model}",
                f"context_window: {get_context_window(self._provider, self._model, self._config):,} tokens",
                f"agent_mode: {agent}",
                f"local_only: {self._config.get('local_only', 'auto')}",
                f"config_file: {get_config_path()}",
            ]
            if self._provider == "ollama":
                lines.append(f"ollama_base_url: {get_ollama_base_url(self._config)}")
            elif self._provider == "custom":
                lines.append(f"custom_base_url: {get_custom_base_url(self._config)}")
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
                elif prov == "custom":
                    models = list_custom_models(self._config)
                    if models is None:
                        lines.append(
                            f"\ncustom (unreachable at "
                            f"{get_custom_base_url(self._config)}): no models"
                        )
                        continue
                    if not models:
                        lines.append(
                            "\ncustom: no models passed native tool-call conformance"
                        )
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
            from .llamaidx import get_llamaidx_url, list_llamaidx_models

            if get_llamaidx_url(self._config):
                entries = list_llamaidx_models(self._config)
                if entries is None:
                    lines.append(
                        "\nllamaidx (registry unreachable at "
                        f"{get_llamaidx_url(self._config)}): no models"
                    )
                elif not entries:
                    lines.append(
                        "\nllamaidx (registry reachable): no tool-verified"
                        " models registered"
                    )
                else:
                    lines.append("\nllamaidx (self-hosted fleet registry, free):")
                    for entry in entries[:48]:
                        is_current = (
                            entry["model_id"] == self._model
                            and self._provider in ("ollama", "custom")
                        )
                        current = " <-- current" if is_current else ""
                        ctx = f"  ctx={entry['ctx']}" if entry.get("ctx") else ""
                        degraded = (
                            "  (provider degraded)" if entry["degraded"] else ""
                        )
                        lines.append(
                            f"  {entry['name']}{ctx}{degraded}{current}"
                        )
            return self._text("\n".join(lines))

        if action == "set_model":
            if not value:
                return self._text("Error: provide a model name in 'value'")
            if value.startswith("llamaidx/"):
                return self._set_llamaidx_model(value)
            target_provider = None
            for prov, models in KNOWN_MODELS.items():
                if prov not in ("ollama", "custom") and value in models:
                    target_provider = prov
                    break
            if not target_provider:
                ok, reason = validate_model_for_provider(
                    "ollama", value, self._config
                )
                if ok is True:
                    target_provider = "ollama"
                elif self._provider == "ollama":
                    return self._text(f"Cannot switch: {reason}. Use action=list_models to see options.")
            if not target_provider:
                ok, reason = validate_model_for_provider(
                    "custom", value, self._config
                )
                if ok is True:
                    target_provider = "custom"
                elif self._provider == "custom":
                    return self._text(
                        f"Cannot switch: {reason}. "
                        "Use action=list_models to see options."
                    )
            if not target_provider:
                from .providers import suggest_models
                pool = [
                    m for prov, models in KNOWN_MODELS.items()
                    if prov != "ollama" for m in models
                ]
                suggestions = suggest_models(value, pool)
                hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                return self._text(
                    f"Unknown model '{value}'.{hint} "
                    "Use action=list_models to see options."
                )
            from .config import local_only_enabled

            if local_only_enabled(self._config, self._provider) and (
                target_provider not in ("ollama", "custom")
            ):
                return self._text(
                    "Cannot switch to a cloud model while local_only is enabled."
                )
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
            from .config import local_only_enabled

            if local_only_enabled(self._config, self._provider) and value not in (
                "ollama",
                "custom",
            ):
                return self._text(
                    "Cannot switch to a cloud provider while local_only is enabled."
                )
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
            self.permissions().set_agent_mode(enabled)
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
# conch_introspect — model-facing introspection of conch's own capabilities,
# configuration, and source code. Everything is derived from live registries
# (slash commands, loaded tools, skills, profiles, providers) so it never
# goes stale; outputs are token-bounded for small local models.
# ---------------------------------------------------------------------------

# Sized to hold the full live capability report (every slash command +
# tool + skill line) with headroom; the /notes family pushed the report
# past the old 4000.
INTROSPECT_OUTPUT_MAX = 4500

CONCH_INTROSPECT_TOOL = {
    "type": "function",
    "function": {
        "name": "conch_introspect",
        "description": (
            "Inspect YOUR OWN capabilities, configuration, and source code. "
            "Use when the user asks what you can do, what commands/tools/"
            "skills exist, how a conch feature works internally, or where "
            "you are installed. Actions: capabilities (full feature surface, "
            "generated from live registries), config (effective settings), "
            "source_overview (map of conch's own codebase), read_source "
            "(read one conch source file)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["capabilities", "config", "source_overview", "read_source"],
                },
                "path": {
                    "type": "string",
                    "description": "Source file to read, relative to the conch "
                                   "repo/package (for read_source), e.g. "
                                   "conch/runtime.py",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based line to start reading from "
                                   "(for read_source; default 1)",
                },
            },
            "required": ["action"],
        },
    },
}


def conch_source_root() -> Path:
    """Root of conch's own source: the git checkout containing the package
    when present, otherwise the installed package directory itself."""
    package_dir = Path(__file__).resolve().parent
    if (package_dir.parent / ".git").exists():
        return package_dir.parent
    return package_dir


class ConchIntrospectClient:
    """Built-in tool that lets the model examine conch itself."""

    name = "conch_introspect"

    def __init__(self):
        self._provider = ""
        self._model = ""
        self._config: dict = {}
        self._chat_state = None

    def bind(self, provider: str, model: str, config: dict, chat_state=None):
        self._provider = provider
        self._model = model
        self._config = config
        self._chat_state = chat_state

    def update(self, provider: str, model: str):
        self._provider = provider
        self._model = model

    def _text(self, msg: str) -> dict:
        from .runtime import truncate_middle
        return {"content": [{"type": "text", "text": truncate_middle(msg, INTROSPECT_OUTPUT_MAX)}]}

    def call_tool(self, name: str, arguments: dict) -> dict:
        action = (arguments.get("action") or "capabilities").lower()
        if action == "capabilities":
            return self._text(self._capabilities())
        if action == "config":
            return self._text(self._config_report())
        if action == "source_overview":
            return self._text(self._source_overview())
        if action == "read_source":
            return self._read_source(
                arguments.get("path", ""), int(arguments.get("start_line", 1) or 1)
            )
        return self._text(f"Unknown action: {action}")

    # --- capabilities -------------------------------------------------------

    def _capabilities(self) -> str:
        from . import __version__
        from .commands import SLASH_COMMANDS, load_user_commands
        from .providers import RAW_FNS
        from .skills import load_skills

        lines = [f"Conch v{__version__} — capability report (generated live)"]

        lines.append("\n## Slash commands (typed by the user, handled by conch)")
        for spec, description in SLASH_COMMANDS:
            lines.append(f"- {spec}: {description}")
        user_commands = load_user_commands()
        if user_commands:
            lines.append("- custom commands from ~/.config/conch/commands/: "
                         + ", ".join("/" + n for n in sorted(user_commands)))

        lines.append("\n## Tools (callable by the model)")
        lines.extend(self._tool_lines())

        skills = load_skills()
        lines.append("\n## Skills (reusable procedures; skill_manage / /skill)")
        if skills:
            for skill_name, skill in sorted(skills.items()):
                lines.append(f"- {skill_name}: {skill['description'] or '(no description)'}")
        else:
            lines.append("- none saved yet")

        profiles = list_profiles(self._config)
        active = active_profile_name() or "(default)"
        lines.append(f"\n## Tool profiles (active: {active})")
        for prof_name, info in sorted(profiles.items()):
            lines.append(f"- {prof_name}: {info.get('description', '')}")

        lines.append("\n## Providers")
        lines.append(f"- supported: {', '.join(sorted(RAW_FNS))}")
        lines.append(f"- current: {self._provider}/{self._model}")
        return "\n".join(lines)

    def _tool_lines(self) -> List[str]:
        state = self._chat_state
        all_tools = getattr(state, "all_tools", None) or []
        tool_map = getattr(state, "tool_map", None) or {}
        active = {
            t.get("function", {}).get("name", "")
            for t in (getattr(state, "tools", None) or [])
        }
        if not all_tools:
            return ["- (tool registry not loaded)"]
        by_group: Dict[str, List[str]] = {}
        descriptions: Dict[str, str] = {}
        for tool in all_tools:
            fn = tool.get("function", {})
            tool_name = fn.get("name", "")
            group = tool_group(tool_name, tool_map)
            by_group.setdefault(group, []).append(tool_name)
            desc = (fn.get("description") or "").split(". ")[0].strip()
            descriptions[tool_name] = desc[:110]
        lines: List[str] = []
        for group in sorted(by_group):
            names = by_group[group]
            if len(names) == 1 and names[0] == group:
                marker = "" if group in active else " [inactive]"
                lines.append(f"- {group}: {descriptions.get(group, '')}{marker}")
            else:
                shown = ", ".join(sorted(names)[:8])
                more = f", +{len(names) - 8} more" if len(names) > 8 else ""
                lines.append(f"- {group} ({len(names)} tools): {shown}{more}")
        return lines

    # --- config -------------------------------------------------------------

    def _config_report(self) -> str:
        from .config import (
            find_project_rc,
            get_config_path,
            local_only_enabled,
        )
        from .providers import (
            get_context_window,
            get_custom_base_url,
            get_ollama_base_url,
        )

        lines = ["Effective configuration:"]
        lines.append(f"- provider: {self._provider}")
        lines.append(f"- model: {self._model}")
        lines.append(f"- context window: "
                     f"{get_context_window(self._provider, self._model, self._config):,} tokens")
        if self._provider == "ollama":
            lines.append(f"- ollama server: {get_ollama_base_url(self._config)}")
        elif self._provider == "custom":
            lines.append(
                f"- inference server: "
                f"{get_custom_base_url(self._config)}"
            )
        lines.append(
            f"- local only: "
            f"{'on' if local_only_enabled(self._config, self._provider) else 'off'}"
        )
        lines.append(f"- agent mode: {'on' if get_agent_mode() else 'off'}; "
                     f"permission mode: {get_permission_mode()}")
        config_path = get_config_path()
        exists = "" if os.path.isfile(config_path) else " (not created yet)"
        lines.append(f"- config file: {config_path}{exists}")
        project_rc = find_project_rc()
        if project_rc is not None:
            lines.append(f"- project .conchrc: {project_rc}")

        hidden = ("token", "key", "password", "secret", "credential")

        def _is_secret(key: str) -> bool:
            return not key.endswith("_env") and any(h in key.lower() for h in hidden)

        skip = {"provider", "model", "chat_model"}
        entries = [
            f"  {key} = {value}" for key, value in sorted(self._config.items())
            if key not in skip and not _is_secret(key)
        ]
        if entries:
            lines.append("- settings:")
            lines.extend(entries)
        secret_count = sum(
            1 for key in self._config if key not in skip and _is_secret(key)
        )
        if secret_count:
            lines.append(f"- ({secret_count} secret-like setting(s) hidden)")
        return "\n".join(lines)

    # --- source -------------------------------------------------------------

    def _source_overview(self) -> str:
        from . import __version__
        from .repomap import build_map_for_root

        root = conch_source_root().resolve()
        is_checkout = (root / ".git").exists()
        lines = [f"Conch v{__version__} source at {root} "
                 f"({'git checkout' if is_checkout else 'installed package'})"]
        if is_checkout:
            try:
                branch = subprocess.run(
                    ["git", "branch", "--show-current"], cwd=str(root),
                    capture_output=True, timeout=5,
                ).stdout.decode().strip()
                if branch:
                    lines.append(f"branch: {branch}")
                log = subprocess.run(
                    ["git", "log", "--oneline", "-6"], cwd=str(root),
                    capture_output=True, timeout=5,
                ).stdout.decode().strip()
                if log:
                    lines.append("recent commits:")
                    lines.extend(f"  {entry}" for entry in log.splitlines())
            except (OSError, subprocess.TimeoutExpired):
                pass
        # Sized so every core conch/*.py module line (alphabetical map:
        # through runtime.py and beyond) survives the cut; notes.py's
        # arrival pushed runtime.py out of the old 2800, and llamaidx.py's
        # status-view exports pushed it out of 3200. Headroom check: this
        # plus the version/branch/commits header stays under
        # INTROSPECT_OUTPUT_MAX (4500).
        overview = build_map_for_root(root, budget_chars=3400)
        if overview:
            lines.append(overview)
        return "\n".join(lines)

    def _read_source(self, rel_path: str, start_line: int = 1) -> dict:
        rel_path = (rel_path or "").strip()
        if not rel_path:
            return self._text("Error: provide 'path' (e.g. conch/runtime.py)")
        root = conch_source_root().resolve()
        normalized = rel_path
        # In a checkout root/conch/runtime.py exists; in an installed wheel
        # root itself is site-packages/conch. Accept the same user-facing path
        # in both layouts.
        if (
            not (root / ".git").exists()
            and root.name == "conch"
            and normalized.startswith("conch/")
        ):
            normalized = normalized[len("conch/"):]
        target = (root / normalized).resolve()
        try:
            inside = target.is_relative_to(root)
        except AttributeError:  # python < 3.9 fallback (not expected)
            inside = str(target).startswith(str(root) + os.sep)
        if not inside:
            return self._text("Error: path escapes the conch source tree")
        if not target.is_file():
            return self._text(f"Error: no such file: {rel_path} "
                              "(use action='source_overview' to list files)")
        try:
            source_lines = target.read_text(errors="replace").splitlines()
        except OSError as exc:
            return self._text(f"Error reading {rel_path}: {exc}")
        total = len(source_lines)
        start = max(1, start_line)
        chunk: List[str] = []
        used = 0
        end = start - 1
        for i in range(start - 1, total):
            line = f"{i + 1:5d}| {source_lines[i]}"
            if used + len(line) + 1 > INTROSPECT_OUTPUT_MAX - 200:
                break
            chunk.append(line)
            used += len(line) + 1
            end = i + 1
        header = f"{rel_path} lines {start}-{end} of {total}"
        if end < total:
            header += f" (continue with start_line={end + 1})"
        # Bypass the generic middle-truncation: this output is already sized.
        return {"content": [{"type": "text", "text": header + "\n" + "\n".join(chunk)}]}


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


# ---------------------------------------------------------------------------
# llamaidx_registry — the self-hosted fleet registry as a chat data source
# ---------------------------------------------------------------------------

LLAMAIDX_REGISTRY_TOOL = {
    "type": "function",
    "function": {
        "name": "llamaidx_registry",
        "description": (
            "Query the llama-idx registry, the live data source for the "
            "user's self-hosted inference fleet (llama.cpp, Ollama, and "
            "OpenAI-compatible servers). Use fleet_status when the user "
            "asks what is running, which boxes/GPUs are up, degraded, or "
            "down, or why a server is unavailable — it includes down "
            "providers with their last error and last-seen time, plus "
            "per-model context size, quantization, loaded state, and "
            "modalities. Use list_models for the registry models conch can "
            "actually switch to; select one with conch_config "
            "action=set_model value=llamaidx/<provider>/<model>. Read-only: "
            "answers come from the registry's stored state and never wake "
            "or probe the boxes themselves."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["fleet_status", "list_models"],
                    "description": (
                        "fleet_status: every registered provider with "
                        "health, labels, and models (including down boxes "
                        "and models without tool support). list_models: "
                        "only the selectable tool-verified entries."
                    ),
                },
                "provider": {
                    "type": "string",
                    "description": (
                        "Optional exact provider name to narrow "
                        "fleet_status to one box (e.g. 'burt')."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


class LlamaidxRegistryClient:
    """Read-only chat surface over the llama-idx registry.

    Reporting only, never routing: model selection still goes through
    conch_config/set_model (or /model) with conch's probe-on-select, and
    this client only ever talks to the registry — not to the provider
    boxes it describes. Output is bounded by render_fleet_status. Auth is
    only ever reported as the NAME of an env var, never a value.
    """

    name = "llamaidx_registry"

    def __init__(self, config: dict):
        self._config = config

    def _text(self, msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def call_tool(self, name: str, arguments: dict) -> dict:
        from .llamaidx import (
            fetch_llamaidx_status,
            get_llamaidx_url,
            list_llamaidx_models,
            render_fleet_status,
        )

        arguments = arguments or {}
        action = str(arguments.get("action") or "").strip()
        url = get_llamaidx_url(self._config)
        if not url:
            return self._text(
                "No llama-idx registry is configured (llamaidx_url is unset)."
            )

        if action == "fleet_status":
            # force_refresh: fleet questions ("is burt up?") deserve the
            # registry's current state, not a cached snapshot.
            status = fetch_llamaidx_status(self._config, force_refresh=True)
            if status is None:
                return self._text(
                    f"The llama-idx registry at {url} is unreachable, serves "
                    "an unsupported schema version, or is blocked by "
                    "local_only. Fleet status is unknown — do not guess at "
                    "provider health."
                )
            provider_filter = str(arguments.get("provider") or "").strip()
            if provider_filter:
                matching = [
                    p for p in status["providers"] if p["name"] == provider_filter
                ]
                if not matching:
                    known = ", ".join(
                        sorted(p["name"] for p in status["providers"])
                    ) or "none"
                    return self._text(
                        f"No provider named '{provider_filter}' in the "
                        f"registry. Registered providers: {known}."
                    )
                status = dict(status, providers=matching)
            return self._text(render_fleet_status(status))

        if action == "list_models":
            entries = list_llamaidx_models(self._config, force_refresh=True)
            if entries is None:
                return self._text(
                    f"The llama-idx registry at {url} is unreachable, serves "
                    "an unsupported schema version, or is blocked by "
                    "local_only. No registry models are selectable right now."
                )
            if not entries:
                return self._text(
                    "The registry is reachable but lists no tool-verified "
                    "models on up/degraded providers, so nothing is "
                    "selectable. Use fleet_status to see why (down boxes, "
                    "models that failed the tool probe)."
                )
            lines = ["Selectable registry models (tool-verified, on up/degraded providers):"]
            for entry in entries[:48]:
                bits = []
                if entry.get("ctx"):
                    bits.append(f"ctx={entry['ctx']}")
                bits.append("loaded" if entry.get("loaded") else "not loaded")
                if entry["degraded"]:
                    bits.append("provider degraded")
                lines.append(f"  - {entry['name']}  ({', '.join(bits)})")
            if len(entries) > 48:
                lines.append(f"  (+{len(entries) - 48} more)")
            lines.append(
                "Switch with conch_config action=set_model "
                "value=llamaidx/<provider>/<model> (conch re-probes tool "
                "support before committing)."
            )
            return self._text("\n".join(lines))

        return self._text(
            f"Unknown action '{action}'. Use fleet_status or list_models."
        )
