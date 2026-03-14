"""Primary Conch chat runtime built from the extracted services."""

from __future__ import annotations

import copy
import datetime
import os
import readline
import shutil
import sys
import urllib.request
from typing import Any, Dict, List

from .commands import handle_slash_command
from .config import load_config
from .conversations import Conversation, ConversationManager
from .memory import MemoryStore
from .providers import RAW_FNS
from .render import highlight
from .runtime import chat_turn, sanitize_anthropic_messages
from .scheduler import Scheduler
from .tooling import (
    LocalShellClient,
    LocalShellPolicy,
    ManageToolsClient,
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


CHAT_SYSTEM_PROMPT = (
    "You are Conch, a helpful, concise assistant built into the user's shell. "
    "Answer clearly. Use markdown formatting sparingly — this is a terminal.\n\n"
    "You are an LLM-assisted shell with chat, memory, MCP tools, and background scheduling.\n"
    "Use local_shell for local commands, use MCP tools when available, and keep answers practical."
)

MAX_TOOL_ROUNDS = 10

CONCH_SHELL_ART = [
    "    __",
    " .-'  `'-.",
    "/  .--.    \\",
    "| /    \\    |",
    "| |  o  |   |",
    "\\  '--'   ./'",
    " `-.___.-'",
]


def _detect_location() -> str:
    try:
        request = urllib.request.Request("https://ipinfo.io/json", headers={"User-Agent": "conch/1.0"})
        with urllib.request.urlopen(request, timeout=3) as response:
            import json
            data = json.loads(response.read().decode())
        parts = [value for value in (data.get("city"), data.get("region"), data.get("country")) if value]
        location = ", ".join(dict.fromkeys(parts))
        if data.get("timezone"):
            location += f" (tz: {data['timezone']})"
        return location
    except Exception:
        return ""


def _build_system_prompt(base_prompt: str) -> str:
    now = datetime.datetime.now()
    tz_name = datetime.datetime.now(datetime.timezone.utc).astimezone().tzname()
    location = _detect_location()
    parts = [f"Current date and time: {now.strftime('%A, %B %d, %Y %I:%M %p')} (timezone: {tz_name})."]
    if location:
        parts.append(f"User location: {location}.")
    parts.append("Use this for any time-sensitive or location-relevant requests.")
    return base_prompt + "\n\n" + " ".join(parts)


def _history_path() -> str:
    return os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
        "conch",
        "chat_history",
    )


def _make_builtin_clients(memory: MemoryStore, interactive: bool = True) -> Dict[str, Any]:
    local_shell = LocalShellClient()
    local_shell.set_policy(LocalShellPolicy(interactive=interactive, allow_auto_execute=get_agent_mode()))
    manage_tools = ManageToolsClient()
    save_memory = SaveMemoryClient()
    save_memory.bind(memory)
    return {
        "local_shell": local_shell,
        "manage_tools": manage_tools,
        "save_memory": save_memory,
    }


def _load_runtime_tools(builtin_clients: Dict[str, Any]):
    mcp_clients = mcp_mod.create_clients()
    all_tools, tool_map = mcp_mod.collect_tools(mcp_clients)
    inject_builtin_tools(all_tools, tool_map, builtin_clients)
    prefs = load_tool_prefs()
    prefs, auto_disabled = auto_disable_oversized_groups(all_tools, tool_map, prefs)
    if auto_disabled:
        save_tool_prefs(prefs)
        for group_name, count in auto_disabled:
            print(
                f"\033[33m  Auto-disabled {group_name} ({count} tools — exceeds 200 limit). Use /enable {group_name} to override.\033[0m",
                file=sys.stderr,
            )
    tools = cap_tools(apply_filter(all_tools, tool_map, prefs))
    state = ToolRuntimeState(all_tools=all_tools, tool_map=tool_map, tools=tools)
    builtin_clients["manage_tools"].bind(state)
    return mcp_clients, state


def _summarize_and_save(messages: List[dict], config: dict, raw_fn, memory: MemoryStore):
    user_turns = [m for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if len(user_turns) < 2:
        return
    try:
        summary_prompt = "Summarize this conversation in 2-3 concise bullet points."
        summary_messages = [{"role": "system", "content": "You summarize conversations concisely."}]
        for message in messages[1:]:
            if isinstance(message.get("content"), str) and message["role"] in ("user", "assistant"):
                summary_messages.append({"role": message["role"], "content": message["content"][:500]})
        summary_messages.append({"role": "user", "content": summary_prompt})
        response = raw_fn(config, summary_messages, None)
        summary = response.get("content", "").strip()
        if summary:
            memory.add(f"[Session summary] {summary}", source="summary")
    except Exception:
        pass


def _conversation_title(conv: Conversation, messages: List[dict]) -> str:
    if conv.title and conv.title != "New conversation":
        return conv.title
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"].strip().splitlines()[0][:60] or "New conversation"
    return "New conversation"


def _print_conch_shell_art():
    """Print a small conch-like shell aligned to the right."""
    if not sys.stdout.isatty():
        return
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    art_width = max(len(line) for line in CONCH_SHELL_ART)
    pad = max(0, width - art_width - 2)
    prefix = " " * pad
    for line in CONCH_SHELL_ART:
        print(f"{prefix}\033[2;36m{line}\033[0m")


def chat_loop():
    config = load_config()
    provider = (config.get("provider") or "openai").lower()
    raw_fn = RAW_FNS.get(provider)
    if not raw_fn:
        print(f"conch: unknown provider {provider}", file=sys.stderr)
        sys.exit(1)

    system_prompt = _build_system_prompt(config.get("chat_system_prompt", CHAT_SYSTEM_PROMPT))
    model_name = config.get("chat_model", config.get("model", ""))
    memory = MemoryStore()
    builtin_clients = _make_builtin_clients(memory, interactive=True)
    mcp_clients, chat_state = _load_runtime_tools(builtin_clients)
    sched = Scheduler()

    def _scheduled_executor(prompt: str, _task):
        scheduled_memory = MemoryStore()
        scheduled_builtins = _make_builtin_clients(scheduled_memory, interactive=False)
        scheduled_clients, scheduled_state = _load_runtime_tools(scheduled_builtins)
        try:
            scheduled_messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
            return chat_turn(
                config,
                provider,
                raw_fn,
                scheduled_messages,
                scheduled_state.tools,
                scheduled_state.tool_map,
                scheduled_builtins,
                max_tool_rounds=MAX_TOOL_ROUNDS,
                chat_state=scheduled_state,
            )
        finally:
            mcp_mod.close_all(scheduled_clients)

    sched.set_executor(_scheduled_executor)
    sched.start()

    conv_mgr = ConversationManager()
    current_conv = conv_mgr.get_most_recent()
    if current_conv and current_conv.messages:
        messages = current_conv.messages
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        if provider == "anthropic":
            sanitize_anthropic_messages(messages)
    else:
        current_conv = conv_mgr.create(model=model_name, provider=provider)
        messages = [{"role": "system", "content": system_prompt}]
        current_conv.messages = messages

    history_file = _history_path()
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    try:
        readline.read_history_file(history_file)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(500)

    def _save_current():
        current_conv.messages = messages
        current_conv.provider = provider
        current_conv.model = model_name
        current_conv.title = _conversation_title(current_conv, messages)
        conv_mgr.save(current_conv)

    def _switch_to(conv: Conversation):
        nonlocal current_conv, messages
        _save_current()
        current_conv = conv
        messages = conv.messages
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        if provider == "anthropic":
            sanitize_anthropic_messages(messages)

    def _reload_tools():
        nonlocal mcp_clients
        print("  \033[2mReloading MCP tools...\033[0m")
        mcp_mod.close_all(mcp_clients)
        mcp_clients, new_state = _load_runtime_tools(builtin_clients)
        chat_state.all_tools = new_state.all_tools
        chat_state.tool_map = new_state.tool_map
        chat_state.tools = new_state.tools
        chat_state.needs_tool_refresh = False
        print(f"  \033[1;32m{len(chat_state.tools)}/{len(chat_state.all_tools)} tools active\033[0m\n")

    def _print_banner():
        _print_conch_shell_art()
        print(f"\033[1;36mConch chat\033[0m \033[2m({provider}/{model_name})\033[0m")
        if chat_state.all_tools:
            if len(chat_state.tools) < len(chat_state.all_tools):
                print(f"\033[2m{len(chat_state.tools)}/{len(chat_state.all_tools)} tools active (/tools to manage)\033[0m")
            else:
                print(f"\033[2m{len(chat_state.tools)} tools available\033[0m")
        memory_count = len(memory.get_all())
        if memory_count:
            print(f"\033[2m{memory_count} memor{'y' if memory_count == 1 else 'ies'} loaded\033[0m")
        user_msgs = [m for m in messages if m.get("role") == "user"
                     and isinstance(m.get("content"), str)]
        if user_msgs:
            print(f"\033[2mResuming: {current_conv.title} ({len(user_msgs)} messages)\033[0m")
        convos = conv_mgr.list_all()
        if len(convos) > 1:
            print(f"\033[2m{len(convos)} conversations (/convos to browse, /new for fresh)\033[0m")
        active_tasks = [task for task in sched.list_tasks() if task.active]
        if active_tasks:
            print(f"\033[2m{len(active_tasks)} scheduled task{'s' if len(active_tasks) != 1 else ''} running\033[0m")
        print("\033[2mType 'exit' or Ctrl+D to quit. /help for commands.\033[0m\n")

    _print_banner()

    last_interrupt = 0.0
    try:
        while True:
            try:
                user_input = input("\033[1;33myou:\033[0m ")
            except EOFError:
                print("\n")
                break
            except KeyboardInterrupt:
                now = datetime.datetime.now().timestamp()
                if now - last_interrupt < 1.5:
                    print("\n")
                    break
                last_interrupt = now
                print("\n  \033[2m(Ctrl+C again to exit)\033[0m\n")
                continue

            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped.lower() in ("exit", "quit", "/q"):
                break

            builtin_clients["local_shell"].set_policy(
                LocalShellPolicy(interactive=True, allow_auto_execute=get_agent_mode())
            )

            if stripped.startswith("/"):
                result = handle_slash_command(
                    stripped,
                    config,
                    provider,
                    model_name,
                    set_agent_mode,
                    memory=memory,
                    all_tools=chat_state.all_tools,
                    tool_map=chat_state.tool_map,
                    sched=sched,
                    conv_mgr=conv_mgr,
                    current_conv=current_conv,
                )
                if result == "new_conversation":
                    _save_current()
                    _summarize_and_save(messages, config, raw_fn, memory)
                    current_conv = conv_mgr.create(model=model_name, provider=provider)
                    messages = [{"role": "system", "content": system_prompt}]
                    current_conv.messages = messages
                    print("\n  \033[1;32m✓ New conversation started\033[0m\n")
                    continue
                if isinstance(result, tuple) and result[0] == "switch_conversation":
                    conv = conv_mgr.load(result[1])
                    if conv:
                        _switch_to(conv)
                    else:
                        print(f"\n  \033[31mNo conversation with ID {result[1]}\033[0m\n")
                    continue
                if result == "reload_tools":
                    _reload_tools()
                    continue
                if result is not None:
                    provider, model_name, raw_fn = result
                continue

            turn_snapshot = copy.deepcopy(messages)
            mem_context = memory.build_context(user_input)
            messages[0]["content"] = system_prompt + ("\n\n" + mem_context if mem_context else "")
            messages.append({"role": "user", "content": user_input})
            try:
                reply = chat_turn(
                    config,
                    provider,
                    raw_fn,
                    messages,
                    chat_state.tools,
                    chat_state.tool_map,
                    builtin_clients,
                    max_tool_rounds=MAX_TOOL_ROUNDS,
                    chat_state=chat_state,
                )
            except KeyboardInterrupt:
                print("\n\n  \033[33m⚠ Interrupted\033[0m\n")
                messages.clear()
                messages.extend(turn_snapshot)
                continue

            if reply:
                messages.append({"role": "assistant", "content": reply})
                print(f"\n\033[1;36massistant:\033[0m\n{highlight(reply)}\n")
            else:
                print("\n\033[2m[no response]\033[0m\n")
            _save_current()
    finally:
        _save_current()
        _summarize_and_save(messages, config, raw_fn, memory)
        sched.stop()
        try:
            readline.write_history_file(history_file)
        except OSError:
            pass
        mcp_mod.close_all(mcp_clients)


def main():
    if len(sys.argv) > 1:
        config = load_config()
        provider = (config.get("provider") or "openai").lower()
        raw_fn = RAW_FNS.get(provider)
        if not raw_fn:
            print(f"conch: unknown provider {provider}", file=sys.stderr)
            sys.exit(1)
        system_prompt = _build_system_prompt(config.get("chat_system_prompt", CHAT_SYSTEM_PROMPT))
        user_text = " ".join(sys.argv[1:])
        memory = MemoryStore()
        mem_context = memory.build_context(user_text)
        if mem_context:
            system_prompt += "\n\n" + mem_context
        builtin_clients = _make_builtin_clients(memory, interactive=True)
        mcp_clients, chat_state = _load_runtime_tools(builtin_clients)
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_text}]
        try:
            reply = chat_turn(
                config,
                provider,
                raw_fn,
                messages,
                chat_state.tools,
                chat_state.tool_map,
                builtin_clients,
                max_tool_rounds=MAX_TOOL_ROUNDS,
                chat_state=chat_state,
            )
            if reply:
                print(highlight(reply))
            else:
                print("[no response]", file=sys.stderr)
                sys.exit(1)
        finally:
            mcp_mod.close_all(mcp_clients)
    else:
        chat_loop()
