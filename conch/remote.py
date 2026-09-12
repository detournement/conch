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
    is_destructive_command,
    is_remote_safe_command,
)


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"


REMOTE_REPLY_MAX_CHARS = 3000
REMOTE_APPROVAL_TTL_SECONDS = 600
_APPROVE_RE = re.compile(r"^\s*(approve|deny)\s+#?(\d+)\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Approval store (durable: approvals survive a conch restart)
# ---------------------------------------------------------------------------

_APPROVAL_FILE_LOCK = threading.RLock()


class ApprovalStore:
    def __init__(self):
        self._path = _state_dir() / "remote_approvals.json"
        self._lock = _APPROVAL_FILE_LOCK

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
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    def add(
        self,
        command: str,
        channel: str,
        thread_id: str,
        sender: str = "",
        timeout: int = 60,
        *,
        kind: str = "command",
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Register a pending approval bound to its origin.

        ``kind`` selects what consuming the approval *does*: ``command``
        (the original remote-shell flow) runs the approved command; any
        other kind is a flow-pack approval class (e.g. ``ebay_publish``)
        whose consume *constructs* an exact typed request from the pinned
        immutable contract — never runs a command. For non-command kinds,
        ``command`` holds a human-readable description and ``payload``
        carries the kind's pinned parameters.
        """
        with self._lock:
            data = self._load()
            request_id = int(data.get("next_id", 1))
            data["next_id"] = request_id + 1
            entry: Dict[str, Any] = {
                "command": command,
                "channel": channel,
                "thread_id": thread_id,
                "sender": sender,
                "timeout": timeout,
                "created_at": time.time(),
            }
            if kind != "command":
                entry["kind"] = kind
            if payload:
                entry["payload"] = payload
            data.setdefault("pending", {})[str(request_id)] = entry
            self._save(data)
            return request_id

    def pop(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            entry = data.get("pending", {}).pop(str(request_id), None)
            if entry is not None:
                self._save(data)
            return entry

    def consume(
        self,
        request_id: int,
        *,
        channel: str,
        thread_id: str,
        sender: str,
        max_age: float = REMOTE_APPROVAL_TTL_SECONDS,
    ) -> tuple:
        """Atomically consume an unexpired approval bound to its origin."""
        with self._lock:
            data = self._load()
            pending = data.get("pending", {})
            entry = pending.get(str(request_id))
            if entry is None:
                return None, "missing"
            age = time.time() - float(entry.get("created_at", 0) or 0)
            if max_age > 0 and age > max_age:
                pending.pop(str(request_id), None)
                self._save(data)
                return None, "expired"
            expected = (
                str(entry.get("channel") or ""),
                str(entry.get("thread_id") or ""),
                str(entry.get("sender") or ""),
            )
            actual = (str(channel), str(thread_id), str(sender))
            if expected != actual:
                return None, "origin_mismatch"
            pending.pop(str(request_id), None)
            self._save(data)
            return entry, ""

    def pending(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._load().get("pending", {}))


# ---------------------------------------------------------------------------
# Remote-capped shell
# ---------------------------------------------------------------------------

class RemoteShellClient(LocalShellClient):
    """local_shell for remote sessions: capped at safe_auto regardless of the
    local agent mode. Safe/allowlisted commands run; everything else posts an
    approval request over the channel and returns immediately."""

    def __init__(
        self,
        approvals: ApprovalStore,
        notify_fn,
        channel: str,
        thread_id: str,
        sender: str = "",
    ):
        super().__init__()
        self.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=False))
        self._approvals = approvals
        self._notify_fn = notify_fn
        self._channel = channel
        self._thread_id = thread_id
        self._sender = sender

    def call_tool(self, name: str, arguments: dict) -> dict:
        cmd = (arguments.get("command") or "").strip()
        timeout = int(arguments.get("timeout", 60))
        if not cmd:
            return self._text("Error: empty command")
        # safe_auto cap: only plain read-only commands (or explicitly
        # allowlisted prefixes) run unattended — never destructive ones.
        if not is_destructive_command(cmd) and (
            is_remote_safe_command(cmd) or self._prefix_allowed(cmd)
        ):
            return self._run_command(cmd, timeout)
        request_id = self._approvals.add(
            cmd,
            self._channel,
            self._thread_id,
            self._sender,
            timeout,
        )
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
    "request the user must confirm — when that happens, say so and move on. "
    "Interactive terminal and SSH-control tools are unavailable remotely."
)

# Tools a remote session must never see.
REMOTE_EXCLUDED_TOOLS = {
    "delegate_task", "conch_config", "manage_tools", "skill_manage",
    "todo_list", "api_layer", "conch_introspect", "interactive_terminal",
    "ssh_remote",
}


class RemoteLoop:
    """Polls channels, maps threads to conversations, runs turns, replies."""

    def __init__(self, config: dict, conv_mgr=None, chat_state=None,
                 builtin_clients: Optional[Dict[str, Any]] = None,
                 session=None):
        # An AgentSession may be passed instead of loose config/state; the
        # explicit keyword arguments still win so tests and older callers
        # keep working unchanged.
        self._session = session
        if session is not None:
            config = config or session.config
            chat_state = chat_state or session.chat_state
            builtin_clients = builtin_clients or session.builtin_clients
        self.config = config or {}
        self.manager = ChannelManager(self.config)
        self.approvals = ApprovalStore()
        self._conv_mgr = conv_mgr
        self._chat_state = chat_state
        self._builtin_clients = builtin_clients or {}
        self._sessions_path = _state_dir() / "remote_sessions.json"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._turn_lock = threading.RLock()
        self._pack_flows_cache = None

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
        try:
            self._sessions_path.chmod(0o600)
        except OSError:
            pass

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

    def _remote_clients(
        self, channel: str, thread_id: str, sender: str
    ) -> Dict[str, Any]:
        clients = {
            k: v for k, v in self._builtin_clients.items()
            if k not in REMOTE_EXCLUDED_TOOLS and k != "local_shell"
        }
        clients["local_shell"] = RemoteShellClient(
            self.approvals,
            lambda text, tid: self.manager.notify(text, channel=channel, thread_id=tid),
            channel,
            thread_id,
            sender,
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
        from .session import AgentSession
        from .tooling import PermissionState

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

        # Each inbound turn is its own child session with a fresh, never-
        # agent-mode permission state: the local /agent toggle can never lift
        # the remote safe_auto cap, and remote turns cannot mutate the
        # interactive session's policy. Shared clients keep their own
        # bindings (bind=False); the shell is a per-turn RemoteShellClient.
        session = AgentSession(
            dict(self.config),
            interactive=False,
            permissions=PermissionState(),
        )
        session.attach_clients(
            self._remote_clients(
                message.channel, message.thread_id, message.sender
            ),
            bind=False,
        )
        reply, usage = session.run_turn(
            messages,
            tools=self._remote_tools(),
            tool_map=getattr(self._chat_state, "tool_map", {}) or {},
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

    # --- flow-pack channel intake ---------------------------------------------

    def _packs_configured(self) -> bool:
        return bool(str(self.config.get("capitol_base_url") or "").strip())

    def _pack_flows(self):
        """Channel flows for every loaded flow pack. Lazy: sessions
        without Capitol config never import the pack engine."""
        if self._pack_flows_cache is None:
            from .capitol.packs.registry import channel_flows

            self._pack_flows_cache = channel_flows(
                self.config,
                self.approvals,
                lambda text, channel, thread_id: self.manager.notify(
                    text, channel=channel, thread_id=thread_id
                ),
            )
        return self._pack_flows_cache

    def _pack_flow_for_kind(self, kind: str):
        """The pack channel flow that declares approval-store *kind*."""
        for flow in self._pack_flows():
            if kind in flow.pack.approval_kinds():
                return flow
        return None

    def _maybe_pack_intake(self, message: InboundMessage) -> Optional[str]:
        """Messages matching a pack's channel intake (and replies in bound
        pack threads) route into that pack's governed pipeline instead of
        the model turn. Inbound text stays business data throughout — it
        never selects tools."""
        if not self._packs_configured():
            return None
        for flow in self._pack_flows():
            reply = flow.handle_message(message)
            if reply is not None:
                return reply
        return None

    # --- inbound dispatch ----------------------------------------------------

    def handle_inbound(self, message: InboundMessage) -> str:
        """Process one allowlisted inbound message; returns the reply text
        that was sent back over the channel."""
        from .runtime import serialized_agent_execution

        with self._turn_lock, serialized_agent_execution():
            match = _APPROVE_RE.match(message.text)
            if match:
                reply = self._handle_approval(match, message)
            else:
                reply = self._maybe_pack_intake(message)
                if reply is None:
                    reply = self._run_turn(message)
            reply = self._bound_reply(reply)
            self.manager.notify(
                reply, channel=message.channel, thread_id=message.thread_id
            )
            return reply

    def _bound_reply(self, reply: str) -> str:
        from .runtime import truncate_middle
        return truncate_middle(reply or "(no response)", REMOTE_REPLY_MAX_CHARS)

    def _handle_approval(self, match, message: InboundMessage) -> str:
        verb = match.group(1).lower()
        request_id = int(match.group(2))
        try:
            ttl = float(
                self.config.get(
                    "remote_approval_ttl", REMOTE_APPROVAL_TTL_SECONDS
                )
                or REMOTE_APPROVAL_TTL_SECONDS
            )
        except (TypeError, ValueError):
            ttl = REMOTE_APPROVAL_TTL_SECONDS
        # Peek at the kind before the atomic consume discards an expired
        # entry — an expired *pack* approval gets a fresh one reissued.
        pending_kind = str(
            (self.approvals.pending().get(str(request_id)) or {}).get("kind")
            or "command"
        )
        entry, error = self.approvals.consume(
            request_id,
            channel=message.channel,
            thread_id=message.thread_id,
            sender=message.sender,
            max_age=ttl,
        )
        if entry is None:
            if error == "expired":
                if pending_kind != "command" and self._packs_configured():
                    flow = self._pack_flow_for_kind(pending_kind)
                    if flow is not None:
                        reissued = flow.reissue_expired(message)
                        if reissued:
                            return reissued
                return f"Approval #{request_id} expired; request it again."
            if error == "origin_mismatch":
                return (
                    f"Approval #{request_id} belongs to a different sender "
                    "or conversation."
                )
            return f"No pending approval #{request_id}."
        entry_kind = str(entry.get("kind") or "command")
        if entry_kind != "command":
            # Consuming a pack approval never runs a command: the pack's
            # flow constructs the exact typed request from the pinned
            # immutable contract (the second staleness check runs
            # Capitol-side).
            flow = (
                self._pack_flow_for_kind(entry_kind)
                if self._packs_configured() else None
            )
            if flow is None:
                return (
                    f"Approval #{request_id} has kind {entry_kind!r} but "
                    "no configured flow pack handles it; nothing was done."
                )
            return flow.handle_approval(request_id, entry, verb, message)
        if verb == "deny":
            return f"Denied #{request_id}: `{entry['command']}` will not run."
        from .tooling import run_hook

        command = entry["command"]
        arguments = {
            "command": command,
            "timeout": int(entry.get("timeout", 60) or 60),
        }
        # Even an explicitly user-approved command passes required policy:
        # deterministic policy authorizes, and it fails closed.
        from .policy import evaluate_required_policy

        decision = evaluate_required_policy(
            "pre_tool_use",
            {
                "tool": "local_shell",
                "arguments": arguments,
                "remote_approval": {
                    "id": request_id,
                    "channel": message.channel,
                    "thread_id": message.thread_id,
                    "sender": message.sender,
                },
            },
        )
        if not decision.allowed:
            return (
                f"Approval #{request_id} denied by required policy: "
                f"{decision.reason or decision.check or 'no reason given'}"
            )
        allowed, hook_out = run_hook(
            "pre_tool_use",
            {
                "tool": "local_shell",
                "arguments": arguments,
                "remote_approval": {
                    "id": request_id,
                    "channel": message.channel,
                    "thread_id": message.thread_id,
                    "sender": message.sender,
                },
            },
            self.config,
        )
        if not allowed:
            return (
                f"Approval #{request_id} was blocked by pre_tool_use hook: "
                f"{hook_out or 'no reason provided'}"
            )
        if hook_out:
            return (
                f"Approval #{request_id} was not run because the hook tried "
                "to rewrite an already-approved command."
            )
        # Explicit user approval: run the exact command that was proposed.
        shell = LocalShellClient()
        shell.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        result = shell.call_tool("local_shell", arguments)
        output = result.get("content", [{}])[0].get("text", "")
        run_hook(
            "post_tool_use",
            {
                "tool": "local_shell",
                "arguments": arguments,
                "result": output,
                "remote_approval_id": request_id,
            },
            self.config,
        )
        return f"Ran #{request_id}: {command}\n\n{output}"

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
