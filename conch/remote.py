"""Remote agentic loop (plan 4.3): channel message → conversation →
chat_turn → reply back over the channel.

Safety posture (hard requirements from the plan):
- Inbound senders must be allowlisted per channel (enforced in channels.py —
  fail closed).
- Remote sessions are capped at **safe_auto** permissions regardless of the
  local agent/yolo mode: read-only commands run, anything else becomes an
  approval request posted over the channel ("approve <id>" / "deny <id>").
  Destructive commands are never auto-approved remotely.
- Remote sessions never get self-management or delegation tools.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .channels import ChannelManager, InboundMessage
from .tooling import (
    LocalShellClient,
    LocalShellPolicy,
    command_prefix,
    is_destructive_command,
    is_safe_command,
)


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"


REMOTE_REPLY_MAX_CHARS = 3000
_APPROVE_RE = re.compile(r"^\s*(approve|deny)\s+#?(\d+)\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Approval store (durable: approvals survive a conch restart)
# ---------------------------------------------------------------------------

class ApprovalStore:
    def __init__(self):
        self._path = _state_dir() / "remote_approvals.json"

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"next_id": 1, "pending": {}}

    def _save(self, data: Dict[str, Any]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)

    def add(self, command: str, channel: str, thread_id: str) -> int:
        data = self._load()
        request_id = int(data.get("next_id", 1))
        data["next_id"] = request_id + 1
        data.setdefault("pending", {})[str(request_id)] = {
            "command": command,
            "channel": channel,
            "thread_id": thread_id,
            "created_at": time.time(),
        }
        self._save(data)
        return request_id

    def pop(self, request_id: int) -> Optional[Dict[str, Any]]:
        data = self._load()
        entry = data.get("pending", {}).pop(str(request_id), None)
        if entry is not None:
            self._save(data)
        return entry

    def pending(self) -> Dict[str, Any]:
        return dict(self._load().get("pending", {}))


# ---------------------------------------------------------------------------
# Remote-capped shell
# ---------------------------------------------------------------------------

class RemoteShellClient(LocalShellClient):
    """local_shell for remote sessions: capped at safe_auto regardless of the
    local agent mode. Safe/allowlisted commands run; everything else posts an
    approval request over the channel and returns immediately."""

    def __init__(self, approvals: ApprovalStore, notify_fn, channel: str, thread_id: str):
        super().__init__()
        self.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=False))
        self._approvals = approvals
        self._notify_fn = notify_fn
        self._channel = channel
        self._thread_id = thread_id

    def call_tool(self, name: str, arguments: dict) -> dict:
        cmd = (arguments.get("command") or "").strip()
        timeout = int(arguments.get("timeout", 60))
        if not cmd:
            return self._text("Error: empty command")
        # safe_auto cap: only plain read-only commands (or explicitly
        # allowlisted prefixes) run unattended — never destructive ones.
        if not is_destructive_command(cmd) and (
            is_safe_command(cmd) or self._prefix_allowed(cmd)
        ):
            return self._run_command(cmd, timeout)
        request_id = self._approvals.add(cmd, self._channel, self._thread_id)
        self._notify_fn(
            f"Approval needed [#{request_id}]:\n  {cmd}\n"
            f"Reply 'approve {request_id}' or 'deny {request_id}'.",
            self._thread_id,
        )
        return self._text(
            f"Command requires user approval (request #{request_id} sent over "
            f"{self._channel}). Do not retry it; tell the user it is pending "
            "and continue with what you can do without it."
        )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

REMOTE_SYSTEM_PROMPT = (
    "You are Conch, an LLM shell assistant, operating REMOTELY over {channel}. "
    "The user is not at the machine. Keep replies short and plain-text "
    "(no markdown tables; this may be SMS). Shell access is restricted: "
    "read-only commands run immediately; anything else creates an approval "
    "request the user must confirm — when that happens, say so and move on."
)

# Tools a remote session must never see.
REMOTE_EXCLUDED_TOOLS = {
    "delegate_task", "conch_config", "manage_tools", "skill_manage",
    "todo_list", "api_layer",
}


class RemoteLoop:
    """Polls channels, maps threads to conversations, runs turns, replies."""

    def __init__(self, config: dict, conv_mgr=None, chat_state=None,
                 builtin_clients: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.manager = ChannelManager(self.config)
        self.approvals = ApprovalStore()
        self._conv_mgr = conv_mgr
        self._chat_state = chat_state
        self._builtin_clients = builtin_clients or {}
        self._sessions_path = _state_dir() / "remote_sessions.json"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- session mapping (channel thread == conch conversation) -----------

    def _load_sessions(self) -> Dict[str, str]:
        try:
            return json.loads(self._sessions_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_sessions(self, sessions: Dict[str, str]):
        self._sessions_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._sessions_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sessions, indent=2))
        tmp.replace(self._sessions_path)

    def _conversation_for(self, message: InboundMessage):
        """Load (or create) the conversation mapped to this channel thread."""
        if self._conv_mgr is None:
            from .conversations import ConversationManager
            self._conv_mgr = ConversationManager()
        key = f"{message.channel}:{message.thread_id}"
        sessions = self._load_sessions()
        conv = None
        if key in sessions:
            conv = self._conv_mgr.load(sessions[key])
        if conv is None:
            provider = (self.config.get("provider") or "").lower()
            model = self.config.get("chat_model", self.config.get("model", ""))
            conv = self._conv_mgr.create(model=model, provider=provider)
            conv.title = f"[{message.channel}] {message.sender}"
            sessions[key] = conv.id
            self._save_sessions(sessions)
        return conv

    # --- turn execution ----------------------------------------------------

    def _remote_clients(self, channel: str, thread_id: str) -> Dict[str, Any]:
        clients = {
            k: v for k, v in self._builtin_clients.items()
            if k not in REMOTE_EXCLUDED_TOOLS and k != "local_shell"
        }
        clients["local_shell"] = RemoteShellClient(
            self.approvals,
            lambda text, tid: self.manager.notify(text, channel=channel, thread_id=tid),
            channel,
            thread_id,
        )
        return clients

    def _remote_tools(self) -> Optional[List[dict]]:
        pool = getattr(self._chat_state, "tools", None) or []
        tools = [
            t for t in pool
            if t.get("function", {}).get("name") not in REMOTE_EXCLUDED_TOOLS
        ]
        return tools or None

    def _run_turn(self, message: InboundMessage) -> str:
        from .providers import RAW_FNS
        from .runtime import chat_turn

        provider = (self.config.get("provider") or "").lower()
        raw_fn = RAW_FNS.get(provider)
        if raw_fn is None:
            return f"conch: unknown provider '{provider}'"
        conv = self._conversation_for(message)
        system_prompt = REMOTE_SYSTEM_PROMPT.format(channel=message.channel)
        messages = conv.messages
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": system_prompt})
        else:
            messages[0]["content"] = system_prompt
        messages.append({"role": "user", "content": message.text})

        config = dict(self.config)
        reply, usage = chat_turn(
            config,
            provider,
            raw_fn,
            messages,
            self._remote_tools(),
            getattr(self._chat_state, "tool_map", {}) or {},
            self._remote_clients(message.channel, message.thread_id),
            max_tool_rounds=int(self.config.get("remote_rounds", 8) or 8),
        )
        if usage.get("error"):
            reply = ("conch: the model backend is unreachable right now — "
                     "try again later.")
        if reply:
            messages.append({"role": "assistant", "content": reply})
        conv.messages = messages
        if self._conv_mgr is not None:
            self._conv_mgr.save(conv)
        return reply or "(no response)"

    # --- inbound dispatch ----------------------------------------------------

    def handle_inbound(self, message: InboundMessage) -> str:
        """Process one allowlisted inbound message; returns the reply text
        that was sent back over the channel."""
        match = _APPROVE_RE.match(message.text)
        if match:
            reply = self._handle_approval(match, message)
        else:
            reply = self._run_turn(message)
        reply = self._bound_reply(reply)
        self.manager.notify(reply, channel=message.channel, thread_id=message.thread_id)
        return reply

    def _bound_reply(self, reply: str) -> str:
        from .runtime import truncate_middle
        return truncate_middle(reply or "(no response)", REMOTE_REPLY_MAX_CHARS)

    def _handle_approval(self, match, message: InboundMessage) -> str:
        verb = match.group(1).lower()
        request_id = int(match.group(2))
        entry = self.approvals.pop(request_id)
        if entry is None:
            return f"No pending approval #{request_id}."
        if verb == "deny":
            return f"Denied #{request_id}: `{entry['command']}` will not run."
        # Explicit user approval: run the exact command that was proposed.
        shell = LocalShellClient()
        shell.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        result = shell.call_tool("local_shell", {"command": entry["command"]})
        output = result.get("content", [{}])[0].get("text", "")
        return f"Ran #{request_id}: {entry['command']}\n\n{output}"

    # --- polling -------------------------------------------------------------

    def poll_once(self) -> int:
        """One polling pass; returns the number of messages handled."""
        handled = 0
        for message in self.manager.poll_all():
            try:
                self.handle_inbound(message)
                handled += 1
            except Exception as exc:
                import sys
                print(f"  \033[33m⚠ remote loop error: {exc}\033[0m", file=sys.stderr)
        return handled

    def _loop(self):
        interval = float(self.config.get("remote_poll_interval", 60) or 60)
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(interval)

    def start(self):
        if not self.manager.configured():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
