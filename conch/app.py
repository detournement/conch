"""Primary Conch chat runtime built from the extracted services."""

from __future__ import annotations

import copy
import datetime
import os
import readline
import select
import shutil
import sys
import termios
import threading
import tty
import urllib.request
from typing import Any, Dict, List

from .commands import handle_slash_command
from .config import load_config
from .conversations import Conversation, ConversationManager
from .memory import MemoryStore
from .providers import RAW_FNS
from .render import highlight, StreamPrinter
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


from .prompts import get_chat_prompt

CHAT_SYSTEM_PROMPT = None  # resolved per-provider at startup

MAX_TOOL_ROUNDS = 25  # default, adjustable via /rounds

CONCH_SHELL_ART = [
    "      ,/",
    "     //",
    "    //  .-~~~-.",
    "   //  /  (•)  \\",
    "  //  |   /~~\\  |",
    " //   |  | __ | |",
    " \\    \\ \\____/ /",
    "  \\    '------'",
    "   \\___________)",
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


class TypeaheadBuffer:
    """Capture keystrokes in cbreak mode while the LLM is working.

    Uses a single reader thread with select() + cbreak so there is never
    a second thread blocked on stdin.  Readline's input() owns stdin at
    all other times.
    """

    def __init__(self):
        self._buffer = ""
        self._queued: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_settings = None

    def start(self):
        if not sys.stdin.isatty():
            return
        self._stop.clear()
        self._buffer = ""
        try:
            self._old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            self._old_settings = None
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> str:
        """Stop capturing and restore terminal. Returns un-entered partial text."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            except termios.error:
                pass
            self._old_settings = None
        partial = self._buffer
        self._buffer = ""
        return partial

    def get_queued(self) -> list[str]:
        lines = list(self._queued)
        self._queued.clear()
        return lines

    def _loop(self):
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not ready or self._stop.is_set():
                    continue
                ch = sys.stdin.read(1)
                if not ch:
                    break
                if ch in ("\r", "\n"):
                    if self._buffer:
                        self._queued.append(self._buffer)
                        sys.stderr.write(
                            f"\r\033[K  \033[2m(queued: {self._buffer[:60]})\033[0m\n"
                        )
                        sys.stderr.flush()
                        self._buffer = ""
                elif ch in ("\x7f", "\x08"):
                    if self._buffer:
                        self._buffer = self._buffer[:-1]
                elif ch == "\x03":
                    self._buffer = ""
                    self._stop.set()
                elif ch >= " ":
                    self._buffer += ch
            except (EOFError, OSError, ValueError):
                break


def chat_loop():
    config = load_config()
    provider = (config.get("provider") or "openai").lower()
    raw_fn = RAW_FNS.get(provider)
    if not raw_fn:
        print(f"conch: unknown provider {provider}", file=sys.stderr)
        sys.exit(1)

    model_name = config.get("chat_model", config.get("model", ""))
    from .prompts import get_chat_prompt
    base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name)
    system_prompt = _build_system_prompt(base_prompt)
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

    max_tool_rounds = MAX_TOOL_ROUNDS
    session_usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0}

    conv_mgr = ConversationManager()
    current_conv = conv_mgr.get_most_recent()
    if current_conv and current_conv.messages:
        messages = current_conv.messages
        # Strip any tool-call artifacts from saved history
        _clean = []
        for _m in messages:
            _role = _m.get("role", "user")
            _content = _m.get("content", "")
            if _role == "tool":
                continue
            if isinstance(_content, list):
                _tp = [b.get("text", "") for b in _content if isinstance(b, dict) and b.get("type") == "text"]
                _content = "\n".join(t for t in _tp if t).strip()
                if not _content:
                    continue
            if isinstance(_content, str):
                _s = _content.strip()
                if _s.startswith(("[Called tool:", "<tool_called", "[Tool result", "<tool_result")):
                    continue
            if _role == "assistant" and _m.get("tool_calls") and not str(_content).strip():
                continue
            _clean.append({"role": _role, "content": _content})
        messages = _clean
        current_conv.messages = messages
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

    _SLASH_COMMANDS = [
        "/help", "/models", "/model", "/provider", "/remember", "/memories",
        "/forget", "/browse", "/new", "/convos", "/switch", "/delete",
        "/agent", "/schedule", "/tasks", "/cancel", "/tools", "/enable",
        "/disable", "/connect", "/apps", "/reload", "/rounds", "/cost",
        "/profile", "/profiles",
        "/queue",
    ]

    def _completer(text, state):
        if text.startswith("/"):
            matches = [c + " " for c in _SLASH_COMMANDS if c.startswith(text)]
        else:
            matches = []
        return matches[state] if state < len(matches) else None

    readline.set_completer(_completer)
    readline.set_completer_delims(" ")
    readline.parse_and_bind("tab: complete")

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

    _typeahead = TypeaheadBuffer()
    _typeahead_enabled = True
    _typeahead_queued: list[str] = []
    _typeahead_partial = ""

    last_interrupt = 0.0
    try:
        while True:
            if _typeahead_queued:
                user_input = _typeahead_queued.pop(0)
                print(f"\033[1;33myou:\033[0m \033[2m{user_input}\033[0m")
            else:
                if _typeahead_partial:
                    prefill = _typeahead_partial
                    _typeahead_partial = ""
                    readline.set_startup_hook(lambda: readline.insert_text(prefill))
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
                finally:
                    readline.set_startup_hook()

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
                    session_usage=session_usage,
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
                if result == "queue_on":
                    _typeahead_enabled = True
                elif result == "queue_off":
                    _typeahead_enabled = False
                elif isinstance(result, int):
                    max_tool_rounds = result
                elif result is not None:
                    old_provider = provider
                    provider, model_name, raw_fn = result
                    if provider != old_provider:
                        from .runtime import normalize_messages_on_switch
                        normalize_messages_on_switch(messages, provider)
                        from .prompts import get_chat_prompt
                        base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name)
                        system_prompt = _build_system_prompt(base_prompt)
                        messages[0]["content"] = system_prompt
                continue

            turn_snapshot = copy.deepcopy(messages)
            mem_context = memory.build_context(user_input)
            messages[0]["content"] = system_prompt + ("\n\n" + mem_context if mem_context else "")
            messages.append({"role": "user", "content": user_input})

            if _typeahead_enabled:
                _typeahead.start()

            _use_streaming = sys.stdout.isatty()
            _printer = StreamPrinter() if _use_streaming else None

            if _use_streaming:
                print(f"\n\033[1;36massistant:\033[0m")

            try:
                reply, turn_usage = chat_turn(
                    config,
                    provider,
                    raw_fn,
                    messages,
                    chat_state.tools,
                    chat_state.tool_map,
                    builtin_clients,
                    max_tool_rounds=max_tool_rounds,
                    chat_state=chat_state,
                    on_token=_printer.feed if _printer else None,
                )
            except KeyboardInterrupt:
                _typeahead.stop()
                print("\n\n  \033[33m\u26a0 Interrupted\033[0m\n")
                messages.clear()
                messages.extend(turn_snapshot)
                continue

            _typeahead_partial = _typeahead.stop()
            _typeahead_queued.extend(_typeahead.get_queued())

            if reply:
                messages.append({"role": "assistant", "content": reply})
                if _printer:
                    _printer.flush()
                else:
                    print(f"\n\033[1;36massistant:\033[0m\n{highlight(reply)}\n")
            else:
                if _printer:
                    _printer.flush()
                print("\n\033[2m[no response]\033[0m\n")

            # Display token/cost info
            in_tok = turn_usage.get("input_tokens", 0)
            out_tok = turn_usage.get("output_tokens", 0)
            used_model = turn_usage.get("model", model_name)
            if in_tok or out_tok:
                from .providers import estimate_cost
                cost = estimate_cost(used_model, in_tok, out_tok)
                session_usage["input_tokens"] += in_tok
                session_usage["output_tokens"] += out_tok
                session_usage["cost"] += cost
                session_usage["turns"] += 1
                if cost > 0.0001:
                    print(f"  \033[2m{in_tok:,} in / {out_tok:,} out  ~${cost:.4f}  ({used_model})\033[0m")
                else:
                    print(f"  \033[2m{in_tok:,} in / {out_tok:,} out  free  ({used_model})\033[0m")

            _save_current()
    finally:
        _save_current()
        if session_usage["turns"] > 0:
            total_in = session_usage["input_tokens"]
            total_out = session_usage["output_tokens"]
            total_cost = session_usage["cost"]
            turns = session_usage["turns"]
            if total_cost > 0.0001:
                print(f"\n\033[2mSession: {turns} turns, {total_in:,} in / {total_out:,} out tokens, ~${total_cost:.4f}\033[0m")
            else:
                print(f"\n\033[2mSession: {turns} turns, {total_in:,} in / {total_out:,} out tokens, free\033[0m")
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
            reply, _usage = chat_turn(
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
