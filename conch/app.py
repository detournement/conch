"""Primary Conch chat runtime built from the extracted services."""

from __future__ import annotations

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
from .config import get_bool, load_config
from .conversations import Conversation, ConversationManager
from .memory import MemoryStore
from .providers import DEFAULT_API_KEY_ENVS, RAW_FNS
from .render import highlight, StreamPrinter
from .runtime import chat_turn, sanitize_anthropic_messages
from .scheduler import Scheduler
from .tooling import (
    ApiLayerClient,
    ConchConfigClient,
    DelegateTaskClient,
    PublicApiClient,
    LocalShellClient,
    LocalShellPolicy,
    ManageToolsClient,
    SaveMemoryClient,
    SearchConversationsClient,
    TodoListClient,
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

AGENT_MODE_CONFIG_NOTICE = (
    "agent mode is ON by default (shell commands run without confirmation) — "
    "/agent to turn it off, or remove agent_mode from ~/.config/conch/config"
)


def apply_agent_mode_from_config(config: dict) -> bool:
    """Enable agent mode when the config file asks for it.

    Returns True only when agent mode was turned on by the config default,
    so the caller knows to show the startup notice (manual /agent toggles
    mid-session never go through here).
    """
    from .tooling import set_permission_mode
    set_permission_mode(config.get("permission_mode", ""))
    if get_bool(config, "agent_mode"):
        set_agent_mode(True)
        return True
    return False

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


def _build_system_prompt(base_prompt: str, location: str = "", provider: str = "", model: str = "", config: dict = None) -> str:
    """Build the session system prompt.

    Deliberately contains nothing volatile (no timestamp, no per-turn memory
    context): the system prompt must stay byte-stable within a session so
    Ollama's KV prefix cache survives between turns. Per-turn context rides
    on the user message instead (see _augment_user_message).
    """
    parts = []
    if location:
        parts.append(f"User location: {location}.")
    if provider and model:
        from .prompts import build_self_description
        parts.append(build_self_description(provider, model, config))
    prompt = base_prompt + "\n\n" + " ".join(parts) if parts else base_prompt
    # Project-level instructions (CONCH.md / AGENTS.md, plan 1.8) are
    # persistent context that lives outside compactable history.
    from .config import load_project_context
    project_ctx = load_project_context()
    if project_ctx:
        prompt += "\n\n" + project_ctx
    # Always-loaded facts tier (plan 2.8): bounded, user-editable.
    from .memory import load_facts
    facts = load_facts()
    if facts:
        prompt += "\n\n" + facts
    # Repo-map orientation context (plan 3.2), budgeted at ~1k tokens.
    # Disable with repo_map=false in config.
    if get_bool(config or {}, "repo_map", True):
        from .repomap import get_repo_map
        repo_map = get_repo_map()
        if repo_map:
            prompt += "\n\n" + repo_map
    return prompt


def _augment_user_message(user_input: str, mem_context: str = "") -> str:
    """Attach per-turn volatile context (timestamp, recalled memories) to the
    user message so the system prompt can stay byte-stable (KV-cache reuse)."""
    now = datetime.datetime.now()
    tz_name = datetime.datetime.now(datetime.timezone.utc).astimezone().tzname()
    parts = [
        user_input,
        "",
        f"[context] Current date/time: {now.strftime('%A, %B %d, %Y %I:%M %p')} ({tz_name})",
    ]
    if mem_context:
        parts.append(mem_context)
    return "\n".join(parts)


def _history_path() -> str:
    return os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
        "conch",
        "chat_history",
    )


def _make_builtin_clients(memory: MemoryStore, config: dict, interactive: bool = True) -> Dict[str, Any]:
    local_shell = LocalShellClient()
    local_shell.set_policy(LocalShellPolicy(interactive=interactive, allow_auto_execute=get_agent_mode()))
    # Scale shell-output budget to the active model's context window (plan 1.5)
    from .runtime import tool_result_char_budget
    local_shell.set_result_budget(
        tool_result_char_budget((config.get("provider") or "").lower(), config)
    )
    # Seed the always-allow prefix list from config (plan 2.1)
    allow = (config.get("allow_prefixes") or "").strip()
    if allow:
        local_shell.allow_prefixes(p.strip() for p in allow.split(","))
    manage_tools = ManageToolsClient()
    save_memory = SaveMemoryClient()
    save_memory.bind(memory)
    conch_config = ConchConfigClient()
    public_api = PublicApiClient()
    search_convos = SearchConversationsClient()
    clients: Dict[str, Any] = {
        "local_shell": local_shell,
        "manage_tools": manage_tools,
        "save_memory": save_memory,
        "conch_config": conch_config,
        "public_api": public_api,
        "search_conversations": search_convos,
        "todo_list": TodoListClient(),
        "delegate_task": DelegateTaskClient(),
    }
    api_layer_key = config.get("API_LAYER_KEY", "") or os.environ.get("API_LAYER_KEY", "")
    if api_layer_key:
        api_layer = ApiLayerClient()
        api_layer.bind(api_layer_key)
        clients["api_layer"] = api_layer
    return clients


def _load_runtime_tools(builtin_clients: Dict[str, Any], use_cache: bool = False):
    if use_cache:
        cached = mcp_mod.load_cached_tools()
        if cached is not None:
            mcp_clients = mcp_mod.create_clients()
            all_tools = list(cached)
            tool_map: Dict[str, Any] = {}
            # Map cached tools to clients by server name
            for client in mcp_clients.values():
                try:
                    live = client.list_tools()
                except Exception:
                    live = []
                for t in live:
                    tool_map[t["function"]["name"]] = client
            inject_builtin_tools(all_tools, tool_map, builtin_clients)
            prefs = load_tool_prefs()
            prefs, _ = auto_disable_oversized_groups(all_tools, tool_map, prefs)
            tools = cap_tools(apply_filter(all_tools, tool_map, prefs))
            state = ToolRuntimeState(all_tools=all_tools, tool_map=tool_map, tools=tools)
            builtin_clients["manage_tools"].bind(state)
            return mcp_clients, state
    mcp_clients = mcp_mod.create_clients()
    all_tools, tool_map = mcp_mod.collect_tools(mcp_clients)
    mcp_mod.save_tool_cache(all_tools)
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
        # Session summaries are a side task — use the weak model when
        # configured (plan 2.7).
        from .runtime import is_error_response, side_task_fn
        summary_fn, summary_config = side_task_fn(config, raw_fn, config)
        if summary_fn is None:
            return
        response = summary_fn(summary_config, summary_messages, None)
        if is_error_response(response):
            return  # never save provider errors as permanent memories
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


def resolve_ollama_startup_model(config: dict, model_name: str) -> tuple:
    """Verify the configured Ollama model at startup, substituting one that
    exists on the server when possible.

    Returns (model_name, warnings). Never raises and never dead-ends: an
    unreachable server, a missing configured model, or an empty server all
    degrade to a warning so the session still starts.
    """
    from .providers import (
        get_fallback_model,
        get_ollama_base_url,
        list_ollama_models,
        ollama_model_matches,
    )

    live_models = list_ollama_models(config)
    if live_models is None:
        return model_name, [
            f"Ollama server unreachable at {get_ollama_base_url(config)} — "
            f"model '{model_name}' unverified"
        ]
    if ollama_model_matches(model_name, live_models):
        return model_name, []
    replacement = get_fallback_model("ollama", config)
    if replacement:
        return replacement, [
            f"Model '{model_name}' is not available/tool-capable on the "
            f"Ollama server — using '{replacement}' instead"
        ]
    installed = list_ollama_models(config, tool_capable_only=False) or []
    if installed:
        warning = (
            "No tool-capable models installed on the Ollama server — chat "
            "needs tool support (try `ollama pull qwen2.5`, or /provider to "
            "switch)"
        )
    else:
        warning = (
            "No models installed on the Ollama server — pull one (e.g. "
            "`ollama pull qwen2.5`) or /provider to switch"
        )
    return model_name, [warning]


def chat_loop():
    config = load_config()
    agent_mode_from_config = apply_agent_mode_from_config(config)
    provider = (config.get("provider") or "openai").lower()
    raw_fn = RAW_FNS.get(provider)
    if not raw_fn:
        print(f"conch: unknown provider {provider}", file=sys.stderr)
        sys.exit(1)

    model_name = config.get("chat_model", config.get("model", ""))

    # Don't blindly trust a configured/default Ollama model — verify it exists
    # on the server (and supports tools); otherwise pick one that does.
    if provider == "ollama":
        resolved, warnings = resolve_ollama_startup_model(config, model_name)
        for warning in warnings:
            print(f"\033[33m  ⚠ {warning}\033[0m", file=sys.stderr)
        if resolved != model_name:
            model_name = resolved
            config["model"] = resolved
            config["chat_model"] = resolved
    elif provider == "custom":
        # Custom endpoints are assumed tool-capable, verified by a probe.
        from .providers import probe_custom_provider
        ok, reason = probe_custom_provider(config, timeout=5.0)
        if not ok:
            print(f"\033[33m  ⚠ Custom endpoint probe failed: {reason}\033[0m",
                  file=sys.stderr)

    from .prompts import get_chat_prompt
    base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)

    # Detect location in background — inject into prompt when ready
    _location_result = [""]
    def _bg_location():
        _location_result[0] = _detect_location()
    _loc_thread = threading.Thread(target=_bg_location, daemon=True)
    _loc_thread.start()

    system_prompt = _build_system_prompt(base_prompt, provider=provider, model=model_name, config=config)
    memory = MemoryStore()
    builtin_clients = _make_builtin_clients(memory, config, interactive=True)

    # Load tools in background; show prompt immediately
    _tools_ready = threading.Event()
    _bg_mcp_clients: list = [None]
    _bg_chat_state: list = [None]

    def _bg_load_tools():
        mc, cs = _load_runtime_tools(builtin_clients)
        _bg_mcp_clients[0] = mc
        _bg_chat_state[0] = cs
        _tools_ready.set()

    _tools_thread = threading.Thread(target=_bg_load_tools, daemon=True)
    _tools_thread.start()

    sched = Scheduler()

    def _scheduled_executor(prompt: str, _task):
        scheduled_memory = MemoryStore()
        scheduled_builtins = _make_builtin_clients(scheduled_memory, config, interactive=False)
        scheduled_clients, scheduled_state = _load_runtime_tools(scheduled_builtins)
        scheduled_builtins["delegate_task"].bind(config, scheduled_state, scheduled_builtins)
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
        messages = list(current_conv.messages)
        # Only strip textual tool-call artifacts (model-emitted junk);
        # preserve real tool messages, tool_calls, and list content so
        # they remain searchable on disk.
        _clean = []
        for _m in messages:
            _content = _m.get("content", "")
            if isinstance(_content, str):
                _s = _content.strip()
                if _s.startswith(("[Called tool:", "<tool_called", "[Tool result", "<tool_result")):
                    continue
            _clean.append(_m)
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
        "/search", "/agent", "/yolo", "/verbose", "/schedule", "/tasks",
        "/cancel", "/tools", "/enable", "/disable", "/connect", "/apps",
        "/reload", "/rounds", "/cost", "/status", "/profile", "/profiles",
        "/clear", "/queue", "/fact", "/facts",
    ]
    # User-defined commands (~/.config/conch/commands/*.md) complete too
    from .commands import load_user_commands
    _SLASH_COMMANDS += sorted(
        "/" + name for name in load_user_commands() if "/" + name not in _SLASH_COMMANDS
    )

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
        if agent_mode_from_config:
            print(f"\033[1;33m{AGENT_MODE_CONFIG_NOTICE}\033[0m")
        print("\033[2mType 'exit' or Ctrl+D to quit. /help for commands.\033[0m\n")

    # Wait for background tool loading (with a brief spinner if needed)
    if not _tools_ready.is_set():
        from .render import Spinner
        with Spinner("Loading tools"):
            _tools_ready.wait(timeout=30)
    mcp_clients = _bg_mcp_clients[0] or {}
    chat_state = _bg_chat_state[0] or ToolRuntimeState(all_tools=[], tool_map={}, tools=[])

    # Session-only profile selection (plan 1.6): a `tool_profile` config key
    # wins; otherwise local models default to the minimal profile unless the
    # user explicitly activated one (saved prefs are never overwritten here).
    from .tooling import active_profile_name, profile_tool_filter
    _config_profile = (config.get("tool_profile") or "").strip().lower()
    if _config_profile:
        _prof_tools, _prof_desc = profile_tool_filter(
            _config_profile, chat_state.all_tools, chat_state.tool_map, config
        )
        if _prof_tools is not None:
            chat_state.tools = _prof_tools
            print(f"\033[2mProfile '{_config_profile}' from config — {_prof_desc}\033[0m")
        else:
            print(f"\033[33m  ⚠ {_prof_desc}\033[0m", file=sys.stderr)
    elif provider == "ollama" and not active_profile_name():
        _prof_tools, _ = profile_tool_filter(
            "minimal", chat_state.all_tools, chat_state.tool_map, config
        )
        if _prof_tools is not None and len(_prof_tools) < len(chat_state.tools):
            chat_state.tools = _prof_tools
            print(
                "\033[2mLocal model: minimal tool profile active "
                "(/profile full to override)\033[0m"
            )

    # Subagent delegation reads live config/tool state (plan 3.1)
    builtin_clients["delegate_task"].bind(config, chat_state, builtin_clients)

    # Inject location now that background thread has had time
    _loc_thread.join(timeout=0.1)
    if _location_result[0]:
        system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt

    # Bind config client with current state
    builtin_clients["conch_config"].bind(provider, model_name, session_usage, config)
    builtin_clients["search_conversations"].bind(conv_mgr, memory=memory)

    _print_banner()

    _typeahead = TypeaheadBuffer()
    _typeahead_enabled = True
    _typeahead_queued: list[str] = []
    _typeahead_partial = ""

    def _safe_input(prompt):
        """Pause typeahead so input() can read stdin normally."""
        _typeahead.stop()
        try:
            return input(prompt)
        finally:
            if _typeahead_enabled:
                _typeahead.start()

    last_interrupt = 0.0
    _backend_failed = False  # preflight the server after a failed turn (plan 3.3)
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
                LocalShellPolicy(interactive=True, allow_auto_execute=get_agent_mode(), input_fn=_safe_input)
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
                    messages=messages,
                )
                # User-defined slash command: the rendered template becomes
                # this turn's user message (handled by the normal flow below).
                _custom_prompt = None
                if isinstance(result, tuple) and result[0] == "user_prompt":
                    _custom_prompt = result[1]
                    result = None
                    preview = _custom_prompt.strip().splitlines()[0][:70]
                    print(f"  \033[2m→ {preview}\033[0m")
                if result == "new_conversation":
                    _save_current()
                    _summarize_and_save(messages, config, raw_fn, memory)
                    current_conv = conv_mgr.create(model=model_name, provider=provider)
                    messages = [{"role": "system", "content": system_prompt}]
                    current_conv.messages = messages
                    builtin_clients["todo_list"].clear()
                    print("\n  \033[1;32m✓ New conversation started\033[0m\n")
                    continue
                if result == "clear_conversation":
                    old_count = len([m for m in messages if m.get("role") == "user"])
                    messages.clear()
                    messages.append({"role": "system", "content": system_prompt})
                    current_conv.messages = messages
                    builtin_clients["todo_list"].clear()
                    _save_current()
                    print(f"\n  \033[1;32m\u2713 Cleared {old_count} messages\033[0m\n")
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
                if result == "agent_mode_changed":
                    builtin_clients["local_shell"].set_policy(
                        LocalShellPolicy(interactive=True, allow_auto_execute=get_agent_mode(), input_fn=_safe_input)
                    )
                elif result in ("verbose_on", "verbose_off", "verbose_toggle"):
                    from .runtime import get_verbose_tools, set_verbose_tools
                    if result == "verbose_on":
                        set_verbose_tools(True)
                    elif result == "verbose_off":
                        set_verbose_tools(False)
                    else:
                        set_verbose_tools(not get_verbose_tools())
                    status = "\033[1;32mON\033[0m" if get_verbose_tools() else "\033[31mOFF\033[0m"
                    print(f"\n  Verbose tool output: {status}\n")
                elif result == "queue_on":
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
                    # Rebuild the system prompt on any switch so the model's
                    # self-description (provider/model/context window) stays true.
                    from .prompts import get_chat_prompt
                    base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
                    system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
                    if messages and messages[0].get("role") == "system":
                        messages[0]["content"] = system_prompt
                if _custom_prompt is None:
                    continue
                user_input = _custom_prompt

            # Preflight after a failed turn (plan 3.3): don't burn the message
            # against a dead server — check first and offer a clean retry.
            if _backend_failed and provider == "ollama":
                from .providers import check_ollama_health, get_ollama_base_url
                from .render import Spinner
                with Spinner("Checking Ollama server"):
                    healthy = check_ollama_health(config)
                if not healthy:
                    print(
                        f"\n  \033[31m✗ Ollama server still offline at "
                        f"{get_ollama_base_url(config)}\033[0m\n"
                        f"  \033[2m(message not sent — press Enter to retry it "
                        f"when the server is back)\033[0m\n",
                    )
                    _typeahead_partial = user_input  # prefill for easy retry
                    continue
                _backend_failed = False
                print("  \033[2m(server back online)\033[0m")

            # Set title from first user message immediately
            if current_conv.title == "New conversation":
                current_conv.title = user_input.strip().splitlines()[0][:60] or "New conversation"

            # Keep messages[0] byte-stable (KV prefix cache); volatile context
            # (timestamp + recalled memories) rides on the user message.
            mem_context = memory.build_context(user_input)
            messages.append({"role": "user", "content": _augment_user_message(user_input, mem_context)})

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
                    input_fn=_safe_input,
                )
            except KeyboardInterrupt:
                if _printer and _printer._spinner:
                    _printer._spinner.__exit__(None, None, None)
                    _printer._spinner = None
                _typeahead.stop()
                print("\n\n  \033[33m\u26a0 Interrupted\033[0m\n")
                _save_current()
                continue

            _backend_failed = bool(turn_usage.get("error"))

            # API fallback inside chat_turn may switch provider/model via config only
            _pre_fb = (provider, model_name)
            provider = (config.get("provider") or provider).lower()
            model_name = config.get("chat_model") or config.get("model") or model_name
            _sync_fn = RAW_FNS.get(provider)
            if _sync_fn:
                raw_fn = _sync_fn
            builtin_clients["conch_config"].update(provider, model_name)
            if (provider, model_name) != _pre_fb:
                # Keep the self-description accurate after automatic fallback.
                base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
                system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = system_prompt

            _typeahead_partial = _typeahead.stop()
            _typeahead_queued.extend(_typeahead.get_queued())

            if reply:
                messages.append({"role": "assistant", "content": reply})
                if _printer:
                    streamed = _printer.flush()
                    if not streamed.strip():
                        print(highlight(reply))
                        print()
                else:
                    print(f"\n\033[1;36massistant:\033[0m\n{highlight(reply)}\n")
            else:
                if _printer:
                    _printer.flush()
                print("\n\033[2m[no response]\033[0m\n")

            # Display token/cost info with a context-usage gauge
            in_tok = turn_usage.get("input_tokens", 0)
            out_tok = turn_usage.get("output_tokens", 0)
            used_model = turn_usage.get("model", model_name)
            if in_tok or out_tok:
                from .providers import estimate_cost
                from .runtime import estimate_tokens, format_context_gauge, get_context_limit
                cost = estimate_cost(used_model, in_tok, out_tok)
                session_usage["input_tokens"] += in_tok
                session_usage["output_tokens"] += out_tok
                session_usage["cost"] += cost
                session_usage["turns"] += 1
                ctx_used = estimate_tokens(messages)
                ctx_window = get_context_limit(provider, config)
                gauge = format_context_gauge(ctx_used, ctx_window)
                cost_str = f"~${cost:.4f}" if cost > 0.0001 else "free"
                print(f"  \033[2m{in_tok:,} in / {out_tok:,} out  {cost_str}  {gauge}\033[2m  ({used_model})\033[0m")
                if ctx_window and ctx_used / ctx_window >= 0.8:
                    print(
                        f"  \033[33m⚠ Context {ctx_used / ctx_window * 100:.0f}% full — "
                        f"older history will be compacted soon (/clear or /new to reset)\033[0m"
                    )

            # Process any config changes made by the LLM via conch_config tool
            _cfg_client = builtin_clients["conch_config"]
            for _action in _cfg_client.pending_actions:
                if _action[0] == "set_model":
                    _new_prov, _new_mod = _action[1], _action[2]
                    _new_fn = RAW_FNS.get(_new_prov)
                    if _new_fn:
                        old_provider = provider
                        provider = _new_prov
                        model_name = _new_mod
                        raw_fn = _new_fn
                        config["provider"] = _new_prov
                        config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(_new_prov, "")
                        config["chat_model"] = _new_mod
                        config["model"] = _new_mod
                        if provider != old_provider:
                            from .runtime import normalize_messages_on_switch
                            normalize_messages_on_switch(messages, provider)
                        # Any switch (even same-provider model change) must
                        # refresh the self-description in the system prompt.
                        base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
                        system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] = system_prompt
                        _cfg_client.update(provider, model_name)
                        print(f"  \033[1;32m\u2713 Now using {provider}/{model_name}\033[0m")
                elif _action[0] == "set_rounds":
                    max_tool_rounds = _action[1]
                elif _action[0] == "clear_history":
                    old_count = len([m for m in messages if m.get("role") == "user"])
                    messages.clear()
                    messages.append({"role": "system", "content": system_prompt})
                    current_conv.messages = messages
                    print(f"  \033[1;32m\u2713 Cleared {old_count} messages\033[0m")
                elif _action[0] == "new_conversation":
                    _save_current()
                    current_conv = conv_mgr.create(model=model_name, provider=provider)
                    messages = [{"role": "system", "content": system_prompt}]
                    current_conv.messages = messages
                    print("  \033[1;32m\u2713 New conversation started\033[0m")
            _cfg_client.pending_actions.clear()

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
        try:
            _summarize_and_save(messages, config, raw_fn, memory)
        except KeyboardInterrupt:
            pass
        sched.stop()
        try:
            readline.write_history_file(history_file)
        except OSError:
            pass
        mcp_mod.close_all(mcp_clients)


def main():
    if len(sys.argv) > 1:
        config = load_config()
        apply_agent_mode_from_config(config)
        provider = (config.get("provider") or "openai").lower()
        raw_fn = RAW_FNS.get(provider)
        if not raw_fn:
            print(f"conch: unknown provider {provider}", file=sys.stderr)
            sys.exit(1)
        model_name = config.get("chat_model", config.get("model", ""))
        base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
        system_prompt = _build_system_prompt(base_prompt, _detect_location(), provider, model_name, config)
        user_text = " ".join(sys.argv[1:])
        memory = MemoryStore()
        mem_context = memory.build_context(user_text)
        builtin_clients = _make_builtin_clients(memory, config, interactive=True)
        mcp_clients, chat_state = _load_runtime_tools(builtin_clients)
        builtin_clients["delegate_task"].bind(config, chat_state, builtin_clients)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _augment_user_message(user_text, mem_context)},
        ]
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
