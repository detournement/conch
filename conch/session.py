"""Per-session agent execution context (Swarm Phase 0).

``AgentSession`` owns what used to be process-global mutable state: provider
and model configuration, built-in tool clients, permission/agent mode, the
working directory for executed commands, and execution budgets. It wraps
``runtime.chat_turn`` without changing its internals, so every execution
surface — the interactive shell, scheduled tasks, remote channel turns, and
delegated subagents — runs from an explicit context instead of mutating
module globals, and two simultaneous sessions cannot leak policy, cwd, or
tool state into each other.

The interactive CLI keeps its exact behavior by constructing its session
around :func:`conch.tooling.default_permissions`, which the module-level
``set_agent_mode``/``set_permission_mode`` helpers (and therefore ``/agent``,
the ``A`` approval answer, and config defaults) continue to operate on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .tooling import PermissionState

_UNSET = object()


@dataclass
class SessionBudgets:
    """Execution budgets owned by one session.

    ``turn_token_budget`` of ``None`` defers to the session config's
    ``turn_token_budget`` key (current behavior); a number overrides it for
    this session only.
    """

    max_tool_rounds: int = 25
    turn_token_budget: Optional[int] = None


class AgentSession:
    """One agent execution context: config, clients, policy, cwd, budgets."""

    def __init__(
        self,
        config: dict,
        *,
        interactive: bool = False,
        permissions: Optional[PermissionState] = None,
        budgets: Optional[SessionBudgets] = None,
        cwd: Optional[str] = None,
        memory=None,
    ):
        # Live reference by design: interactive model/provider switches and
        # chat_turn's API fallback update the config dict in place.
        self.config = config
        self.interactive = bool(interactive)
        self.permissions = (
            permissions if permissions is not None else PermissionState()
        )
        self.budgets = budgets or SessionBudgets()
        self.cwd = str(cwd) if cwd else os.getcwd()
        self.memory = memory
        self.builtin_clients: Dict[str, Any] = {}
        self.chat_state = None
        self.mcp_clients: Dict[str, Any] = {}
        self._closed = False

    # -- derived provider/model state (always read live from config) --------

    @property
    def provider(self) -> str:
        return (self.config.get("provider") or "").lower()

    @property
    def model(self) -> str:
        return self.config.get("chat_model", self.config.get("model", "")) or ""

    def raw_fn(self):
        from .providers import RAW_FNS

        return RAW_FNS.get(self.provider)

    # -- tool clients --------------------------------------------------------

    def attach_clients(
        self,
        builtin_clients: Dict[str, Any],
        chat_state=None,
        mcp_clients: Optional[Dict[str, Any]] = None,
        *,
        bind: bool = True,
    ):
        """Adopt tool clients for this session.

        With ``bind=True`` (fresh clients owned by this session) every client
        that supports it is bound to this session's permission state and cwd.
        Use ``bind=False`` when the clients are shared with a parent session
        and must keep the parent's bindings (delegated subagents).
        """
        self.builtin_clients = builtin_clients or {}
        if chat_state is not None:
            self.chat_state = chat_state
        if mcp_clients is not None:
            self.mcp_clients = mcp_clients
        if bind:
            for client in self.builtin_clients.values():
                if hasattr(client, "bind_permissions"):
                    client.bind_permissions(self.permissions)
                if hasattr(client, "set_cwd"):
                    client.set_cwd(self.cwd)
        return self

    # -- turn execution ------------------------------------------------------

    def run_turn(
        self,
        messages: List[dict],
        *,
        tools=_UNSET,
        tool_map=_UNSET,
        max_tool_rounds: Optional[int] = None,
        on_token=None,
        input_fn=None,
    ) -> tuple:
        """Run one bounded agent turn through ``runtime.chat_turn``.

        This is a wrapper: chat_turn's internals (rounds, compaction,
        budgets, tool dispatch) are unchanged; the session only supplies the
        state that used to be threaded through by hand at every call site.
        """
        raw_fn = self.raw_fn()
        if raw_fn is None:
            raise RuntimeError(f"unknown provider '{self.provider}'")
        if tools is _UNSET:
            tools = getattr(self.chat_state, "tools", None)
        if tool_map is _UNSET:
            tool_map = getattr(self.chat_state, "tool_map", {}) or {}
        config = self.config
        if self.budgets.turn_token_budget is not None:
            config = dict(config)
            config["turn_token_budget"] = self.budgets.turn_token_budget
        from .runtime import chat_turn

        return chat_turn(
            config,
            self.provider,
            raw_fn,
            messages,
            tools,
            tool_map,
            self.builtin_clients,
            max_tool_rounds=(
                max_tool_rounds
                if max_tool_rounds is not None
                else self.budgets.max_tool_rounds
            ),
            chat_state=self.chat_state,
            on_token=on_token,
            input_fn=input_fn,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self):
        """Release session-owned resources (SSH control sockets, MCP clients).

        Idempotent and never raises. Child sessions built on a parent's
        shared clients never include ``ssh_remote`` (it is excluded from
        delegated and remote toolsets), so closing a child cannot tear down
        the parent's connections.
        """
        if self._closed:
            return
        self._closed = True
        ssh = self.builtin_clients.get("ssh_remote")
        if hasattr(ssh, "close"):
            try:
                ssh.close()
            except Exception:
                pass
        if self.mcp_clients:
            try:
                from . import mcp as mcp_mod

                mcp_mod.close_all(self.mcp_clients)
            except Exception:
                pass
            self.mcp_clients = {}
