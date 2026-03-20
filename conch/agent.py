"""AgentSession - shared agent runtime for terminal and Slack transports."""

from __future__ import annotations

import datetime
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .commands import handle_slash_command
from .config import load_config
from .conversations import ConversationManager
from .memory import MemoryStore
from .providers import DEFAULT_API_KEY_ENVS, RAW_FNS, estimate_cost
from .prompts import get_chat_prompt
from .runtime import chat_turn
from .scheduler import Scheduler
from .tooling import (
    ConchConfigClient,
    LocalShellClient,
    LocalShellPolicy,
    ManageToolsClient,
    PublicApiClient,
    SaveMemoryClient,
    ToolRuntimeState,
    apply_filter,
    auto_disable_oversized_groups,
    cap_tools,
    get_agent_mode,
    inject_builtin_tools,
    load_tool_prefs,
    save_tool_prefs,
    set_agent_mode,
)
from . import mcp as mcp_mod


MAX_TOOL_ROUNDS = 25


def _detect_location():
    try:
        req = urllib.request.Request("https://ipinfo.io/json", headers={"User-Agent": "conch/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            import json
            data = json.loads(resp.read().decode())
        parts = [v for v in (data.get("city"), data.get("region"), data.get("country")) if v]
        loc = ", ".join(dict.fromkeys(parts))
        if data.get("timezone"):
            loc += " (tz: " + data["timezone"] + ")"
        return loc
    except Exception:
        return ""


def _build_system_prompt(base_prompt, location="", provider="", model=""):
    now = datetime.datetime.now()
    tz_name = datetime.datetime.now(datetime.timezone.utc).astimezone().tzname()
    parts = ["Current date and time: " + now.strftime("%A, %B %d, %Y %I:%M %p") + " (timezone: " + tz_name + ")."]
    if location:
        parts.append("User location: " + location + ".")
    if provider and model:
        parts.append("You are currently running as " + provider + "/" + model + ".")
    parts.append("Use this for any time-sensitive or location-relevant requests.")
    return base_prompt + "\n\n" + " ".join(parts)


def _make_builtin_clients(memory, interactive=True):
    local_shell = LocalShellClient()
    local_shell.set_policy(LocalShellPolicy(interactive=interactive, allow_auto_execute=get_agent_mode()))
    manage_tools = ManageToolsClient()
    save_memory = SaveMemoryClient()
    save_memory.bind(memory)
    conch_config = ConchConfigClient()
    public_api = PublicApiClient()
    return {
        "local_shell": local_shell,
        "manage_tools": manage_tools,
        "save_memory": save_memory,
        "conch_config": conch_config,
        "public_api": public_api,
    }


def _load_runtime_tools(builtin_clients, log=None):
    mcp_clients = mcp_mod.create_clients()
    all_tools, tool_map = mcp_mod.collect_tools(mcp_clients)
    mcp_mod.save_tool_cache(all_tools)
    inject_builtin_tools(all_tools, tool_map, builtin_clients)
    prefs = load_tool_prefs()
    prefs, auto_disabled = auto_disable_oversized_groups(all_tools, tool_map, prefs)
    if auto_disabled:
        save_tool_prefs(prefs)
        if log:
            for name, count in auto_disabled:
                log("Auto-disabled " + name + " (" + str(count) + " tools).")
    tools = cap_tools(apply_filter(all_tools, tool_map, prefs))
    state = ToolRuntimeState(all_tools=all_tools, tool_map=tool_map, tools=tools)
    builtin_clients["manage_tools"].bind(state)
    return mcp_clients, state


class AgentSession:
    """Transport-agnostic agent runtime.

    Holds all state for one conversation: config, provider, messages,
    tools, memory, scheduler.  Both the terminal chat_loop and the
    Slack transport create one of these and call turn().
    """

    def __init__(self, interactive=True, auto_agent=False, log=None):
        self._log = log or (lambda msg: None)
        self.config = load_config()
        self.provider = (self.config.get("provider") or "openai").lower()
        self.raw_fn = RAW_FNS.get(self.provider)
        if not self.raw_fn:
            raise ValueError("Unknown provider: " + self.provider)
        self.model_name = self.config.get("chat_model", self.config.get("model", ""))
        if auto_agent:
            set_agent_mode(True)
        base_prompt = self.config.get("chat_system_prompt") or get_chat_prompt(self.provider, self.model_name)
        self.location = _detect_location()
        self.system_prompt = _build_system_prompt(base_prompt, self.location, self.provider, self.model_name)
        self._base_prompt = base_prompt
        self.memory = MemoryStore()
        self.builtin_clients = _make_builtin_clients(self.memory, interactive=interactive)
        self.mcp_clients, self.chat_state = _load_runtime_tools(self.builtin_clients, log=self._log)
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.max_tool_rounds = MAX_TOOL_ROUNDS
        self.session_usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0}
        self.builtin_clients["conch_config"].bind(self.provider, self.model_name, self.session_usage)
        self.sched = Scheduler()
        self.sched.set_executor(self._scheduled_executor)
        self.sched.start()
        self.conv_mgr = ConversationManager()

    def _scheduled_executor(self, prompt, _task):
        sm = MemoryStore()
        sb = _make_builtin_clients(sm, interactive=False)
        sc, ss = _load_runtime_tools(sb)
        try:
            msgs = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": prompt}]
            return chat_turn(self.config, self.provider, self.raw_fn, msgs, ss.tools, ss.tool_map, sb, max_tool_rounds=self.max_tool_rounds, chat_state=ss)
        finally:
            mcp_mod.close_all(sc)

    def turn(self, user_input, on_token=None):
        """Run one agent turn. Returns (reply_text, usage_dict)."""
        self.builtin_clients["local_shell"].set_policy(LocalShellPolicy(interactive=True, allow_auto_execute=get_agent_mode()))
        mem_context = self.memory.build_context(user_input)
        self.messages[0]["content"] = self.system_prompt + ("\n\n" + mem_context if mem_context else "")
        self.messages.append({"role": "user", "content": user_input})
        reply, turn_usage = chat_turn(
            self.config, self.provider, self.raw_fn, self.messages,
            self.chat_state.tools, self.chat_state.tool_map, self.builtin_clients,
            max_tool_rounds=self.max_tool_rounds, chat_state=self.chat_state, on_token=on_token,
        )
        if reply:
            self.messages.append({"role": "assistant", "content": reply})
        in_tok = turn_usage.get("input_tokens", 0)
        out_tok = turn_usage.get("output_tokens", 0)
        used_model = turn_usage.get("model", self.model_name)
        if in_tok or out_tok:
            cost = estimate_cost(used_model, in_tok, out_tok)
            self.session_usage["input_tokens"] += in_tok
            self.session_usage["output_tokens"] += out_tok
            self.session_usage["cost"] += cost
            self.session_usage["turns"] += 1
        self._apply_config_actions()
        return reply, turn_usage

    def handle_command(self, cmd):
        """Handle a slash command. Returns the command result."""
        return handle_slash_command(
            cmd, self.config, self.provider, self.model_name, set_agent_mode,
            memory=self.memory, all_tools=self.chat_state.all_tools,
            tool_map=self.chat_state.tool_map, sched=self.sched,
            conv_mgr=self.conv_mgr, current_conv=None, session_usage=self.session_usage,
        )

    def _apply_config_actions(self):
        cfg = self.builtin_clients["conch_config"]
        for action in cfg.pending_actions:
            if action[0] == "set_model":
                new_prov, new_mod = action[1], action[2]
                new_fn = RAW_FNS.get(new_prov)
                if new_fn:
                    self.provider = new_prov
                    self.model_name = new_mod
                    self.raw_fn = new_fn
                    self.config["provider"] = new_prov
                    self.config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(new_prov, "")
                    self.config["chat_model"] = new_mod
                    self.config["model"] = new_mod
                    self._base_prompt = self.config.get("chat_system_prompt") or get_chat_prompt(self.provider, self.model_name)
                    self.system_prompt = _build_system_prompt(self._base_prompt, self.location, self.provider, self.model_name)
                    self.messages[0]["content"] = self.system_prompt
                    cfg.update(self.provider, self.model_name)
                    self._log("Now using " + self.provider + "/" + self.model_name)
            elif action[0] == "set_rounds":
                self.max_tool_rounds = action[1]
            elif action[0] == "clear_history":
                self.clear_history()
                self._log("Conversation history cleared.")
            elif action[0] == "new_conversation":
                self.clear_history()
                self._log("New conversation started.")
        cfg.pending_actions.clear()

    def reload_tools(self):
        mcp_mod.close_all(self.mcp_clients)
        self.mcp_clients, new_state = _load_runtime_tools(self.builtin_clients, log=self._log)
        self.chat_state.all_tools = new_state.all_tools
        self.chat_state.tool_map = new_state.tool_map
        self.chat_state.tools = new_state.tools
        self.chat_state.needs_tool_refresh = False

    def clear_history(self):
        self.messages.clear()
        self.messages.append({"role": "system", "content": self.system_prompt})

    def summarize_and_save(self):
        user_turns = [m for m in self.messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
        if len(user_turns) < 2:
            return
        try:
            summary_msgs = [{"role": "system", "content": "You summarize conversations concisely."}]
            for msg in self.messages[1:]:
                if isinstance(msg.get("content"), str) and msg["role"] in ("user", "assistant"):
                    summary_msgs.append({"role": msg["role"], "content": msg["content"][:500]})
            summary_msgs.append({"role": "user", "content": "Summarize this conversation in 2-3 concise bullet points."})
            resp = self.raw_fn(self.config, summary_msgs, None)
            summary = resp.get("content", "").strip()
            if summary:
                self.memory.add("[Session summary] " + summary, source="summary")
        except Exception:
            pass

    def close(self):
        self.summarize_and_save()
        self.sched.stop()
        mcp_mod.close_all(self.mcp_clients)

    @property
    def tool_count(self):
        return len(self.chat_state.tools)

    @property
    def total_tool_count(self):
        return len(self.chat_state.all_tools)
