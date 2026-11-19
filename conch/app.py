"""Primary Conch chat runtime built from the extracted services."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import readline
import shutil
import sys
import threading
import time
import urllib.request
from typing import List, Optional

from .bootstrap import (
    TOOL_LOAD_WAIT_SECONDS,
    StartupError,
    ToolLoader,
    apply_agent_mode_from_config,
    build_agent_session,
    load_runtime_tools as _load_runtime_tools,
    make_builtin_clients as _make_builtin_clients,
    make_scheduled_executor,
    resolve_startup_model,
    resolve_startup_provider,
    route_scheduled_output as _route_scheduled_output,
    start_remote_loop,
    start_scheduler,
    start_task_backend,
)
from .commands import handle_slash_command
from .config import display_path, get_bool, load_config
from .conversations import Conversation, ConversationManager
from .memory import MemoryStore
from .modelcheck import ensure_working_model
from .providers import (
    DEFAULT_API_KEY_ENVS, RAW_FNS, add_usage, format_session_cost, record_session_usage,
)
from .render import highlight, StreamPrinter, Spinner
from .runtime import sanitize_anthropic_messages
from .session import AgentSession
from .tooling import (
    LocalShellPolicy,
    default_permissions,
    get_agent_mode,
    set_agent_mode,
)
from . import mcp as mcp_mod
from . import multiline

# Names still importable from conch.app for backwards compatibility (tests
# and external callers); their implementations live in conch.bootstrap now.
__bootstrap_reexports__ = (
    _load_runtime_tools,
    _route_scheduled_output,
    make_scheduled_executor,
    start_scheduler,
)


from .prompts import get_chat_prompt

CHAT_SYSTEM_PROMPT = None  # resolved per-provider at startup

MAX_TOOL_ROUNDS = 25  # default, adjustable via /rounds

AGENT_MODE_CONFIG_NOTICE = (
    "Agent mode is on: shell commands run without asking. /agent off changes it."
)

# The startup banner's conch: spire, knobbed shoulder, aperture, flared lip,
# and siphonal canal.
CONCH_SHELL_ART = r"""
               /\
              /  \
             /`--'\
            / .--. \
           /`'    `'\
          /  .----.  \
     _.-^^  .'    '.  ^^-._
    <_   ^ /  .--.  \ ^   _>-._
      `-. |  /    \  |  .-'    `-.
         ||  |    |  | /          \
         ||  |    |  |/            |
         ||  \    /  |            /
         | \  '--'  /           .'
          \ '.____.'         .-'
           \               .'
            \          _.-'
             \     _.-'
              \_.-'
               V
""".strip("\n").splitlines()


def _print_conch_shell_art() -> None:
    """The conch above the startup banner, on a real terminal with room for
    it: never in pipes, and skipped when it would crowd a small window."""
    if not sys.stdout.isatty():
        return
    columns, rows = shutil.get_terminal_size(fallback=(80, 24))
    width = max(len(line) for line in CONCH_SHELL_ART)
    if columns < width + 4 or rows < len(CONCH_SHELL_ART) + 12:
        return
    plain = "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
    start, end = ("", "") if plain else ("\033[38;5;216m", "\033[0m")
    print()
    for line in CONCH_SHELL_ART:
        print(f"  {start}{line}{end}")


def _display_path(path: str, width: int) -> str:
    """Home-relative path, middle-truncated to fit on one terminal row."""
    path = display_path(path)
    if len(path) > width > 8:
        head = (width - 1) // 3
        path = path[:head] + "…" + path[-(width - 1 - head):]
    return path


def _provider_note(name: str, current: str) -> str:
    """What choosing this provider needs, for the /provider picker."""
    if name == current:
        return "Current provider"
    env_name = DEFAULT_API_KEY_ENVS.get(name)
    if env_name:
        return (f"{env_name} is set" if os.environ.get(env_name, "").strip()
                else f"Needs {env_name}")
    return {
        "claude-code": "Claude subscription sign-in, no API key",
        "codex": "ChatGPT subscription sign-in, no API key",
        "ollama": "Local models",
        "custom": "OpenAI-compatible endpoint",
    }.get(name, "")




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


def format_turn_stats_line(turn_usage: dict, cost_str: str, gauge: str,
                           used_model: str, config: dict) -> str:
    """The dim per-message stats line, or "" when suppressed.

    Gated on ``show_token_stats`` (on by default; ``/tks`` flips it for
    the session). Tokens and cost come from the turn's usage; tok/s is
    server-reported generation speed when the backend provides one
    (Ollama, llama.cpp) and a ``~``-labeled wall-clock estimate
    otherwise (see runtime.format_token_speed).
    """
    from .config import get_bool
    from .runtime import format_token_speed

    if not get_bool(config, "show_token_stats", default=True):
        return ""
    in_tok = turn_usage.get("input_tokens", 0)
    out_tok = turn_usage.get("output_tokens", 0)
    # Estimator-derived counts (backend sent no usage) carry a ~ so an
    # exact-looking number never comes from a chars-per-token guess.
    tilde = "~" if turn_usage.get("tokens_estimated") else ""
    speed = format_token_speed(turn_usage)
    speed_part = f"  {speed}" if speed else ""
    return (
        f"  \033[2m{tilde}{in_tok:,} in / {tilde}{out_tok:,} out"
        f"  {cost_str}{speed_part}  {gauge}\033[2m  ({used_model})\033[0m"
    )


def _build_system_prompt(base_prompt: str, location: str = "", provider: str = "", model: str = "", config: dict = None) -> str:
    """Build the session system prompt.

    Deliberately contains nothing volatile (no timestamp, no per-turn memory
    context): the system prompt must stay byte-stable within a session so
    Ollama's KV prefix cache survives between turns. Per-turn context rides
    on the user message instead (see _augment_user_message). The working
    directory (``send_cwd``) qualifies as stable: conch never chdir()s
    during a session, so it is fixed for the session's lifetime.
    """
    parts = []
    if location:
        parts.append(f"User location: {location}.")
    if get_bool(config or {}, "send_cwd", False):
        parts.append(f"Working directory: {os.getcwd()}.")
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
            # Repo detected → git-aware coding discipline. The nudge is
            # prompt-level; the deterministic backstop is the turn-level
            # checkpoint layer (gitcheckpoint.py), which snapshots every
            # mutating turn regardless of what the model does.
            prompt += (
                "\n\nThis is a git repository: use git deliberately when "
                "writing code. Check git status/diff before and after "
                "edits, prefer a feature branch for multi-file changes, "
                "and commit logical units with descriptive messages when "
                "the user asks for commits. Conch snapshots the worktree "
                "after every turn that changes files (/undo restores)."
            )
    # GitHub snapshot (repository, branch PR and checks, open PRs and
    # issues) once github_context.start() has fetched it.
    from .github_context import get as repo_context
    repo_ctx = repo_context()
    if repo_ctx is not None:
        section = repo_ctx.prompt_section()
        if section:
            prompt += "\n\n" + section
    # Available skills (plan 4.1): compact list so the model knows what it
    # can load with skill_manage.
    from .skills import build_skills_context
    skills_ctx = build_skills_context()
    if skills_ctx:
        prompt += "\n\n" + skills_ctx
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


SESSION_SUMMARY_MIN_TURNS_DEFAULT = 3
SESSION_SUMMARY_SIMILARITY = 0.8


def session_summary_min_turns(config: dict) -> Optional[int]:
    """The `session_summary_min_turns` setting: user turns a session needs
    before it is worth a summary. 0 or off/false/never disables session
    summaries; unparseable values fall back to the default."""
    raw = str((config or {}).get("session_summary_min_turns", "")).strip().lower()
    if raw in ("off", "false", "never", "none", "no"):
        return None
    try:
        value = int(raw) if raw else SESSION_SUMMARY_MIN_TURNS_DEFAULT
    except ValueError:
        return SESSION_SUMMARY_MIN_TURNS_DEFAULT
    return None if value <= 0 else value


def _session_used_tools(messages: List[dict]) -> bool:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            return True
        if message.get("role") == "assistant" and message.get("tool_calls"):
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result")
            for block in content
        ):
            return True
    return False


def session_worth_summarizing(messages: List[dict], config: dict) -> bool:
    """A session earns a `[Session summary]` memory when it had substance:
    at least `session_summary_min_turns` user turns, or any tool use (work
    was done on the machine). Trivial exchanges — a greeting, one question
    — are not remembered, which is what kept filling memory with noise."""
    min_turns = session_summary_min_turns(config)
    if min_turns is None:
        return False
    from .conversations import is_user_prompt, prompt_text

    user_turns = [
        m for m in messages if is_user_prompt(m) and prompt_text(m).strip()
    ]
    if not user_turns:
        return False
    return len(user_turns) >= min_turns or _session_used_tools(messages)


def _summarize_and_save(messages: List[dict], config: dict, raw_fn, memory: MemoryStore):
    if not session_worth_summarizing(messages, config):
        return
    try:
        summary_prompt = "Summarize this conversation in 2-3 concise bullet points."
        summary_messages = [{"role": "system", "content": "You summarize conversations concisely."}]
        from .conversations import is_user_prompt, prompt_text

        for message in messages[1:]:
            if is_user_prompt(message):
                # Typed text only: attached images read as [image: name].
                summary_messages.append({"role": "user", "content": prompt_text(message)[:500]})
            elif message["role"] == "assistant" and isinstance(message.get("content"), str):
                summary_messages.append({"role": "assistant", "content": message["content"][:500]})
        summary_messages.append({"role": "user", "content": summary_prompt})
        # Session summaries are a side task — use the weak model when
        # configured (plan 2.7).
        from .runtime import (
            is_error_response,
            serialized_agent_execution,
            side_task_fn,
        )
        summary_fn, summary_config = side_task_fn(config, raw_fn, config)
        if summary_fn is None:
            return
        from .ebrake import checkpoint

        checkpoint()
        with serialized_agent_execution():
            checkpoint()
            response = summary_fn(summary_config, summary_messages, None)
        if is_error_response(response):
            return  # never save provider errors as permanent memories
        summary = response.get("content", "").strip()
        if summary:
            from .secretguard import CredentialRejected

            entry_text = f"[Session summary] {summary}"
            near_duplicate = getattr(memory, "near_duplicate", None)
            if near_duplicate is not None and near_duplicate(
                entry_text, source="summary", threshold=SESSION_SUMMARY_SIMILARITY
            ):
                return  # the same session, or the same routine, is already remembered
            try:
                with serialized_agent_execution():
                    memory.add(entry_text, source="summary")
            except CredentialRejected:
                # A summary quoting credential material is dropped whole —
                # same discipline as the mission-lesson gate. Losing one
                # best-effort summary beats persisting a secret.
                return
    except Exception:
        pass


def _summarize_and_save_async(messages: List[dict], config: dict, raw_fn,
                              memory: MemoryStore, *, session=None):
    """Fire-and-forget session summary for /new: the LLM call inside
    _summarize_and_save can block for a long time on a busy backend, and
    starting a fresh conversation must not wait on it. Snapshots the
    transcript and summarizes on a daemon thread; the summary is
    best-effort, so losing it on an early exit is acceptable."""
    if not session_worth_summarizing(messages, config):
        return None
    snapshot = list(messages)

    def summarize():
        from .ebrake import AgentsCancelled

        scope = session.agent_scope() if session is not None else contextlib.nullcontext()
        try:
            with scope:
                _summarize_and_save(snapshot, config, raw_fn, memory)
        except AgentsCancelled:
            pass  # Session shutdown cancels best-effort background summaries.

    thread = threading.Thread(
        target=summarize,
        daemon=True,
    )
    thread.start()
    return thread


EXIT_SUMMARY_TIMEOUT_S = 2.5


def _summarize_and_save_bounded(
    messages: List[dict], config: dict, raw_fn, memory: MemoryStore,
    timeout: float = EXIT_SUMMARY_TIMEOUT_S, *, session=None,
):
    """Exit-path session summary: give the LLM call a short, bounded window
    instead of blocking quit for the full generation time. Tradeoff: the
    worker is a daemon thread, so if the backend hasn't answered within the
    timeout the summary is lost when the interpreter exits — acceptable for
    a best-effort memory, and far better than hanging exit on a busy
    server."""
    thread = _summarize_and_save_async(messages, config, raw_fn, memory, session=session)
    if thread is not None:
        thread.join(timeout)


def _conversation_title(conv: Conversation, messages: List[dict]) -> str:
    if conv.title and conv.title != "New conversation":
        return conv.title
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"].strip().splitlines()[0][:60] or "New conversation"
    return "New conversation"






def _startup_conversation(conv_mgr, model_name, provider, system_prompt,
                          fresh=False, resume_id=None):
    """Pick the conversation the shell starts in: *resume_id* when given
    (picked on the conversations page), else the most recent one, or a
    brand-new one when --new was passed or nothing usable exists (no
    history, or every stored file was corrupt and quarantined).
    Returns (conversation, messages) with messages ready for the loop."""
    current_conv = None
    if resume_id:
        current_conv = conv_mgr.load(resume_id)
        if current_conv is None:
            print(f"\033[33m  ⚠ Conversation {resume_id} no longer exists; "
                  "resuming the most recent one\033[0m", file=sys.stderr)
        elif not current_conv.messages:
            current_conv.messages = [{"role": "system", "content": system_prompt}]
            return current_conv, current_conv.messages
    if current_conv is None and not fresh:
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
        return current_conv, messages
    current_conv = conv_mgr.create(model=model_name, provider=provider)
    messages = [{"role": "system", "content": system_prompt}]
    current_conv.messages = messages
    return current_conv, messages


def chat_loop(new_conversation=False, interactive=True, startup_check=None,
              resume_id=None):
    # Startup wiring lives in conch.bootstrap so headless modes can reuse it;
    # this function owns only the interactive composition (warnings printed
    # to stderr, startup failures become process exits).
    #
    # ``interactive=False`` (``--non-interactive`` or a non-TTY stdin) keeps
    # the loop reading commands but makes every approval-gated tool refuse
    # instead of prompt: a pipe cannot answer an approval question.
    interactive = bool(interactive)
    _use_terminal_ui = (
        interactive and sys.stdin.isatty() and sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
    )
    _ui = None
    if not interactive:
        print(_NON_INTERACTIVE_NOTICE, file=sys.stderr)
    with Spinner("Starting Conch") if interactive else contextlib.nullcontext():
        config = load_config()
    # Repository awareness: the repo and branch now, the GitHub snapshot in
    # the background (it joins the system prompt before the first turn that
    # finds it ready, and the status bar as soon as it lands).
    from . import github_context

    _repo_ctx = github_context.start(config)
    _repo_ctx_in_prompt = _repo_ctx is None or _repo_ctx.done.is_set()
    agent_mode_from_config = apply_agent_mode_from_config(config)

    # Startup model check: verify the configured model before anything
    # calls it. A failure puts alternatives in front of the user (TTY) or
    # walks the pre-approved fallback_models list (non-interactive); it
    # never swaps a model silently. Fails closed when nothing works.
    try:
        model_check = startup_check or ensure_working_model(config, interactive=interactive)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(exc.code)
    try:
        provider, raw_fn = resolve_startup_provider(config)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(exc.code)

    model_name, model_warnings = resolve_startup_model(config, provider)
    for warning in model_warnings:
        print(f"\033[33m  ⚠ {warning}\033[0m", file=sys.stderr)
    if model_check.switched:
        provider, model_name = model_check.provider, model_check.model
        raw_fn = RAW_FNS[provider]

    from .prompts import get_chat_prompt
    base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)

    # Public-IP geolocation is explicitly opt-in.
    _location_result = [""]
    def _bg_location():
        _location_result[0] = _detect_location()
    _loc_thread = None
    if get_bool(config, "detect_location", False):
        _loc_thread = threading.Thread(target=_bg_location, daemon=True)
        _loc_thread.start()

    system_prompt = _build_system_prompt(base_prompt, provider=provider, model=model_name, config=config)
    memory = MemoryStore()
    # The interactive session wraps the process-default permission state so
    # /agent, the 'A' approval answer, and config defaults keep operating on
    # the same state the module-level helpers expose.
    session = AgentSession(
        config,
        interactive=interactive,
        permissions=default_permissions(),
        memory=memory,
    )
    session.budgets.max_tool_rounds = MAX_TOOL_ROUNDS
    builtin_clients = _make_builtin_clients(
        memory, config, interactive=interactive, permissions=session.permissions
    )
    session.attach_clients(builtin_clients)

    # Builtin tools (local_shell, ...) are usable from the first turn; MCP
    # discovery runs in the background, bounded per server, and is swapped
    # into this one live state object when it finishes (see
    # _attach_loaded_tools). The session never runs on a silently empty
    # tool set: whatever is still loading or unreachable is named.
    _tool_loader = ToolLoader(builtin_clients, config).start()
    chat_state = _tool_loader.builtin_state
    builtin_clients["manage_tools"].bind(chat_state)
    mcp_clients: dict = {}

    # Each scheduled run gets its own fresh session (bootstrap wiring); the
    # system prompt is read through a closure so provider/model switches keep
    # flowing into scheduled turns. With edge_daemon=true the kernel (and
    # the conch-edge daemon) owns scheduled tasks instead; without it this
    # is the classic in-process scheduler and conch.kernel is never imported.
    sched, sched_kind = start_task_backend(
        config, lambda: system_prompt, max_tool_rounds=MAX_TOOL_ROUNDS,
        pause_controller=session._pause_controller,
    )

    session_usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0}

    conv_mgr = ConversationManager()
    current_conv, messages = _startup_conversation(
        conv_mgr, model_name, provider, system_prompt, fresh=new_conversation,
        resume_id=resume_id,
    )

    history_file = _history_path()
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    if not _use_terminal_ui:
        try:
            readline.read_history_file(history_file)
        except (FileNotFoundError, OSError):
            pass
        readline.set_history_length(500)

    # Completion list comes from the shared command registry, plus
    # user-defined commands (~/.config/conch/commands/*.md).
    from .commands import all_slash_commands, load_user_commands
    _command_choices = all_slash_commands() + [
        ("/" + name, "Personal prompt command") for name in sorted(load_user_commands())
    ] + [("/q", "Exit Conch")]
    _SLASH_COMMANDS = list(dict.fromkeys(spec.split()[0] for spec, _ in _command_choices))

    def _completer(text, state):
        if text.startswith("/"):
            matches = [c + " " for c in _SLASH_COMMANDS if c.startswith(text)]
        else:
            matches = []
        return matches[state] if state < len(matches) else None

    if not _use_terminal_ui:
        readline.set_completer(_completer)
        readline.set_completer_delims(" ")
        readline.parse_and_bind("tab: complete")

    # Multiline input (README "Multiline input & paste"): GNU readline 8.1+
    # gets true bracketed paste; libedit (macOS system Pythons) ignores the
    # directive, so read_user_message's pending-input drain reassembles
    # pastes there instead. multiline_paste=false disables paste coalescing
    # (fences, backslash continuation, /paste, and /edit keep working).
    if not _use_terminal_ui:
        multiline.enable_bracketed_paste()
    _paste_coalesce = get_bool(config, "multiline_paste", True)
    _drain_fn = multiline.drain_pending_input if _paste_coalesce else (lambda: "")

    def _save_current():
        current_conv.messages = messages
        current_conv.provider = provider
        current_conv.model = model_name
        current_conv.title = _conversation_title(current_conv, messages)
        conv_mgr.save(current_conv)

    def _refresh_model(*, normalize_history):
        """Refresh consumers of the route that AgentSession will actually use."""
        nonlocal provider, model_name, raw_fn, base_prompt, system_prompt
        old_provider = provider
        provider, model_name, raw_fn = session.provider, session.model, session.raw_fn()
        if normalize_history and provider != old_provider:
            from .runtime import normalize_messages_on_switch
            normalize_messages_on_switch(messages, provider)
        base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
        system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        builtin_clients["conch_config"].update(provider, model_name)
        builtin_clients["conch_introspect"].update(provider, model_name)

    def _switch_to(conv: Conversation):
        nonlocal current_conv, messages
        _save_current()
        current_conv = conv
        messages = conv.messages
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        if provider == "anthropic":
            sanitize_anthropic_messages(messages)

    # Session-only profile selection (plan 1.6): a `tool_profile` config key
    # wins; otherwise local models default to the minimal profile unless the
    # user explicitly activated one (saved prefs are never overwritten here).
    # Re-applied whenever the tool list is rebuilt (startup, late MCP
    # attach, /reload), since each rebuild recomputes chat_state.tools.
    def _apply_session_profile():
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
        elif provider in ("ollama", "custom") and not active_profile_name():
            _prof_tools, _ = profile_tool_filter(
                "minimal", chat_state.all_tools, chat_state.tool_map, config
            )
            if _prof_tools is not None and len(_prof_tools) < len(chat_state.tools):
                chat_state.tools = _prof_tools
                print(
                    "\033[2mLocal model: minimal tool profile active "
                    "(/profile full to override)\033[0m"
                )

    # MCP servers whose failure has been reported once already (the banner
    # and every later attach share this so a dead server is named once).
    _reported_tool_failures: set = set()

    def _print_tool_notices():
        """The loader's one-line notices: failed servers (once each) and
        the servers still loading."""
        statuses = _tool_loader.statuses()
        for line in _tool_loader.notice_lines(
            len(chat_state.all_tools), already_reported=_reported_tool_failures
        ):
            print(f"\033[33m{line}\033[0m")
        _reported_tool_failures.update(
            name for name, status in statuses.items()
            if status.state in ("unreachable", "error")
        )

    def _attach_loaded_tools(announce: bool = True) -> bool:
        """Swap the background loader's result into the live chat_state if
        it has finished since the last check. Called only from this (the
        turn-running) thread, between turns, so a request in flight never
        has its tool list changed under it. Returns True on the swap."""
        nonlocal mcp_clients
        clients = _tool_loader.adopt(chat_state)
        if clients is None:
            return False
        mcp_clients = clients
        session.mcp_clients = mcp_clients
        _apply_session_profile()
        if chat_state.pinned_tools:
            # Skill-activated tools survive a rebuild the way they survive
            # the per-request cap.
            from .tooling import ensure_tools_active
            ensure_tools_active(chat_state, sorted(chat_state.pinned_tools))
        chat_state.needs_tool_refresh = True
        if announce:
            active, total = len(chat_state.tools), len(chat_state.all_tools)
            counts = f"{active} ready" if active == total else f"{active}/{total} ready (/tools to manage)"
            print(f"\033[2mtools: {counts}\033[0m")
            _print_tool_notices()
        return True

    def _reload_tools():
        nonlocal _tool_loader, mcp_clients
        print("  \033[2mReloading MCP tools...\033[0m")
        # Drop whatever is loading or loaded now; the session runs on the
        # builtin tools until the fresh discovery attaches (bounded per
        # server, so a dead server costs its timeout, not the session).
        _tool_loader.supersede()
        mcp_mod.close_all(mcp_clients)
        mcp_clients = {}
        session.mcp_clients = mcp_clients
        _reported_tool_failures.clear()
        _tool_loader = ToolLoader(builtin_clients, config).start()
        from .bootstrap import swap_tool_state
        swap_tool_state(chat_state, _tool_loader.builtin_state)
        if not _tool_loader.ready.is_set():
            with Spinner("Loading tools"):
                _tool_loader.wait(timeout=TOOL_LOAD_WAIT_SECONDS)
        if _attach_loaded_tools(announce=False):
            _print_tool_notices()
            print(f"  \033[1;32m{len(chat_state.tools)}/{len(chat_state.all_tools)} tools active\033[0m\n")
        else:
            _print_tool_notices()
            print()

    def _print_banner():
        from . import __version__
        _print_conch_shell_art()
        if not model_check.checked:
            check_note = ", model check off"
        elif model_check.switched:
            check_note = " ✓ verified, switched with approval"
        else:
            check_note = " ✓ verified"
        if _ui:
            print(f"\n\033[1;36mConch\033[0m \033[2mv{__version__}{check_note}\033[0m")
            columns = shutil.get_terminal_size(fallback=(80, 24)).columns
            print(f"\033[2m{_display_path(os.getcwd(), columns - 1)}\033[0m")
            if _repo_ctx is not None:
                print(_repo_ctx.banner_line())
            resumed = any(message.get("role") == "user" for message in messages)
            if resumed:
                print(f"\033[2mResumed: {current_conv.title} · /new starts fresh\033[0m")
            else:
                print("Describe what you want to do. For example:")
                print('  "Explain this project" or "Help me understand a failing command".')
                print("Use /model to choose a model; Ctrl+P finds commands.")
                if session.permissions.get_permission_mode() == "prompt_all":
                    print("Shell commands require your approval before they run.")
            _print_tool_notices()
            if agent_mode_from_config:
                print(f"\033[1;33m{AGENT_MODE_CONFIG_NOTICE}\033[0m")
            if sched_kind == "kernel" and not sched.daemon_running():
                print("\033[33mEdge is enabled but not running; scheduled missions will not fire.\033[0m")
            print()
            return
        print(
            f"\033[1;36mConch chat\033[0m \033[2mv{__version__} "
            f"({provider}/{model_name}{check_note})\033[0m"
        )
        if _repo_ctx is not None:
            print(f"\033[2m{_repo_ctx.banner_line()}\033[0m")
        if chat_state.all_tools:
            if len(chat_state.tools) < len(chat_state.all_tools):
                print(f"\033[2m{len(chat_state.tools)}/{len(chat_state.all_tools)} tools active (/tools to manage)\033[0m")
            else:
                print(f"\033[2m{len(chat_state.tools)} tools available\033[0m")
        # Dead or still-loading MCP servers are named here, never silent:
        # a session that quietly ran without its tools made the model
        # narrate commands it could not run.
        _print_tool_notices()
        memory_count = len(memory.get_all())
        if memory_count:
            print(f"\033[2m{memory_count} memor{'y' if memory_count == 1 else 'ies'} loaded\033[0m")
        from .conversations import is_user_prompt

        user_msgs = [m for m in messages if is_user_prompt(m)]
        if user_msgs:
            print(f"\033[2mResuming: {current_conv.title} ({len(user_msgs)} messages)\033[0m")
        convos = conv_mgr.list_all()
        if len(convos) > 1:
            print(f"\033[2m{len(convos)} conversations (/convos to browse, /new for fresh)\033[0m")
        active_tasks = [task for task in sched.list_tasks() if task.active]
        if active_tasks:
            print(f"\033[2m{len(active_tasks)} scheduled task{'s' if len(active_tasks) != 1 else ''} running\033[0m")
        if sched_kind == "kernel":
            if sched.daemon_running():
                print("\033[2mEdge daemon connected (/missions, /approvals)\033[0m")
            else:
                print(
                    "\033[33mEdge mode is on but conch-edge is not running — "
                    "scheduled tasks and missions won't fire until it starts\033[0m"
                )
        if agent_mode_from_config:
            print(f"\033[1;33m{AGENT_MODE_CONFIG_NOTICE}\033[0m")
        print("\033[2mType 'exit' or Ctrl+D to quit. /help for commands.\033[0m")
        print(
            "\033[2mPassword/passphrase prompts: /terminal <command>; "
            "remote hosts: /ssh help (input is never captured).\033[0m\n"
        )

    # Wait a bounded time for MCP discovery (with a brief spinner if
    # needed). When it expires the session starts on the builtin tools and
    # the loader's result is attached between turns once it arrives.
    if not _tool_loader.ready.is_set():
        with Spinner("Loading tools"):
            _tool_loader.wait(timeout=TOOL_LOAD_WAIT_SECONDS)
    session.attach_clients(
        builtin_clients, chat_state=chat_state, mcp_clients=mcp_clients
    )
    if not _attach_loaded_tools(announce=False):
        _apply_session_profile()

    # Subagent delegation reads live config/tool state (plan 3.1)
    builtin_clients["delegate_task"].bind_session(session)
    # Fan-out workers may run other models; their use joins the session cost.
    builtin_clients["fan_out"].bind_session(session, usage=session_usage)
    # skill_manage action='use' activates a skill's declared tools the
    # same way /skill does (and reports the ones this session lacks).
    builtin_clients["skill_manage"].bind_state(chat_state)

    # Remote agentic loop (plan 4.3): opt-in via remote_enabled=true.
    _remote_loop, _remote_reason = start_remote_loop(
        config, conv_mgr=conv_mgr, session=session
    )
    if _remote_loop is not None:
        print(
            f"\033[2mRemote loop active on: {', '.join(_remote_loop.manager.configured())} "
            f"(safe_auto cap, allowlisted senders only)\033[0m"
        )
    elif _remote_reason == "daemon-hosted":
        print(
            "\033[2mChannel intake is hosted by the conch-edge daemon "
            "(remote_host=daemon); the shell will not poll channels\033[0m"
        )
    elif _remote_reason == "no channel configured":
        print(
            "\033[33m  ⚠ remote_enabled is set but no channel is configured "
            "(slack/sms/email)\033[0m",
            file=sys.stderr,
        )

    # Inject location now that background thread has had time
    if _loc_thread is not None:
        _loc_thread.join(timeout=0.1)
    if _location_result[0]:
        system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt

    # Bind config client with current state
    builtin_clients["conch_config"].bind(provider, model_name, session_usage, config)
    builtin_clients["conch_introspect"].bind(provider, model_name, config, chat_state)
    builtin_clients["search_conversations"].bind(conv_mgr, memory=memory)

    _handoff_depth = 0

    # Turn-level git checkpoints (plan: maximally use local git when
    # writing code). Active only inside a git repo with git_checkpoints
    # on; every mutating turn snapshots to refs/conch/checkpoints/* so
    # /undo and /checkpoint restore work without touching user history.
    from .gitcheckpoint import GitCheckpoints

    _git_ckpt = GitCheckpoints(config)
    # Pre-turn ("before") snapshot state: taken lazily, once per turn,
    # right before the first worktree-mutating tool runs, so /undo can
    # revert the turn that just happened (a post-turn snapshot alone
    # only records the state /undo would want to leave).
    _ckpt_turn_label = ""
    _ckpt_before_taken = False
    _ckpt_lock = threading.Lock()

    def _begin_checkpoint_turn(label: str) -> None:
        nonlocal _ckpt_turn_label, _ckpt_before_taken
        with _ckpt_lock:
            _ckpt_turn_label = label
            _ckpt_before_taken = False

    def _before_mutation(what: str) -> None:
        """Hook run by local_shell / interactive_terminal before executing."""
        nonlocal _ckpt_before_taken
        with _ckpt_lock:
            if _ckpt_before_taken or not _git_ckpt.enabled():
                return
            _ckpt_before_taken = True
            label = _ckpt_turn_label or (
                str(what).strip().splitlines() or ["command"]
            )[0][:80]
            _git_ckpt.snapshot(label, kind="before")

    for _hooked in ("local_shell", "interactive_terminal", "edit_file", "write_file"):
        if hasattr(builtin_clients.get(_hooked), "set_before_run"):
            builtin_clients[_hooked].set_before_run(_before_mutation)

    def _safe_input(prompt):
        """Approval input is isolated from the composer and queued requests."""
        if _ui and not _handoff_depth:
            return _ui.ask(prompt)
        return input(prompt)

    @contextlib.contextmanager
    def _suspend_ui(discard=False):
        """Legacy prompts and external programs get exclusive terminal ownership."""
        nonlocal _handoff_depth
        context = _ui.handoff(discard=discard) if _ui else contextlib.nullcontext()
        with context:
            _handoff_depth += 1
            try:
                yield
            finally:
                _handoff_depth -= 1

    @contextlib.contextmanager
    def _terminal_handoff_context():
        # Credential-sensitive handoffs discard draft/queue before releasing stdin.
        with _suspend_ui(discard=True):
            yield

    # Only retain the latest completed turn's metrics, never relabel them as
    # usage from a different conversation or the currently selected model.
    _infobar_turn = None

    def _sync_ui_context(*, context_used=None, context_window=None):
        nonlocal _infobar_turn
        if _ui:
            from .model_tuning import describe as describe_tuning
            from .runtime import (
                calibration_key, estimate_tokens, format_context_gauge, get_context_limit,
            )
            from .themes import current_scheme

            if _infobar_turn and _infobar_turn[0] != current_conv.id:
                _infobar_turn = None
            if context_used is None:
                context_used = estimate_tokens(messages, key=calibration_key(provider, config))
            if context_window is None:
                context_window = get_context_limit(provider, config)
            gauge = format_context_gauge(context_used, context_window)
            session_cost = format_session_cost(session_usage, provider, model_name, config)
            if _infobar_turn:
                _, usage, cost, actual_model = _infobar_turn
                stats = format_turn_stats_line(usage, cost, gauge, actual_model, config).strip()
            else:
                stats = "  ".join(part for part in (session_cost, gauge, f"({model_name})") if part)
            _ui.set_colorscheme(current_scheme(config))

            _ui.set_context(
                provider=provider, model=model_name,
                tuning=describe_tuning(config, provider, model_name),
                permission=session.permissions.get_permission_mode(),
                conversation=current_conv.title, cwd=os.getcwd(),
                repo=_repo_ctx.indicator() if _repo_ctx is not None else "",
                tools=f"{len(chat_state.tools)}/{len(chat_state.all_tools)}",
                session_cost=session_cost,
                infobar=config.get("infobar", "detail"),
                show_token_stats=get_bool(config, "show_token_stats", default=True),
                infobar_stats=stats,
                context_used=context_used, context_window=context_window,
                agents_paused=session.agents_paused,
            )

    def _set_foreground_policies():
        standard = LocalShellPolicy(
            interactive=interactive,
            allow_auto_execute=get_agent_mode(),
            input_fn=_safe_input,
        )
        handoff = LocalShellPolicy(
            interactive=interactive,
            allow_auto_execute=get_agent_mode(),
            input_fn=_safe_input,
            handoff_context=_terminal_handoff_context,
        )
        builtin_clients["local_shell"].set_policy(standard)
        builtin_clients["interactive_terminal"].set_policy(handoff)
        builtin_clients["ssh_remote"].set_policy(handoff)
        for _name in ("read_file", "edit_file", "write_file", "web_fetch"):
            if _name in builtin_clients:
                builtin_clients[_name].set_policy(standard)
        builtin_clients["conch_config"].set_policy(standard)
        builtin_clients["skill_manage"].configure(
            interactive=interactive, input_fn=_safe_input
        )

    def _choose_ssh_execution(command):
        if _ui:
            return _ui.choose(f"SSH execution · {command}", [
                ("terminal", "Direct terminal", "Interactive SSH; input/output stays private"),
                ("model", "Let the model handle it", "Send to the model; normal tools and permissions"),
            ])
        if not interactive:
            return "model"
        print(
            "\n  SSH execution:\n"
            "    1. Direct terminal — interactive, non-recording handoff\n"
            "    2. Let the model handle it — normal tools and permissions"
        )
        while True:
            try:
                answer = _safe_input(
                    "  SSH execution [1/2, Enter cancels]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if answer in ("1", "terminal"):
                return "terminal"
            if answer in ("2", "model"):
                return "model"
            if not answer:
                return None
            print("  Choose 1 or 2, or press Enter to cancel.")

    def _apply_session_control(kind, argument):
        if kind == "ebrake":
            action = argument.strip().lower()
            if action in ("", "on"):
                session.pause_agents()
            elif action == "off":
                session.resume_agents()
            elif action != "status":
                print("\n  Usage: /ebrake [on|off|status]\n")
                return
            paused = session.agents_paused
            if _ui:
                _ui.set_context(agents_paused=paused)
            print(
                "\n  Emergency brake ON: session agents pause before their next "
                "request or tool. In-flight work may finish. /ebrake off resumes.\n"
                if paused else "\n  Emergency brake OFF: session agents may run.\n"
            )
            return
        from .settings_registry import current_value, set_setting

        if kind == "infobar":
            title = "Info bar"
            choices = {
                "detail": "Tokens in/out, cost, speed, context use, and model",
                "progress": "Estimated context utilization with a progress ruler",
            }
        elif kind == "notifications":
            from .notify import DEFAULT_AFTER_SECONDS, notify_after_seconds

            delay = notify_after_seconds(dict(config, notifications="on")) or DEFAULT_AFTER_SECONDS
            title = "Notifications"
            choices = {"on": f"Notify when a turn takes at least {delay:g} seconds",
                       "off": "Do not notify when turns finish"}
            current = current_value(config, kind)
            choices = {current: choices[current], **choices}
        else:
            from .themes import SCHEMES

            title, choices = "Color scheme", SCHEMES
        name = argument.strip()
        if not name:
            current = current_value(config, kind)
            if not _ui:
                print(f"\n  {title}: {current}")
                for choice, description in choices.items():
                    print(f"    {choice:<12} {description}")
                print(f"\n  /{kind} <name> selects and saves a choice.\n")
                return
            name = _ui.choose(f"{title} — Enter saves, Escape cancels", [
                (choice, choice + (" (current)" if choice == current else ""), description)
                for choice, description in choices.items()
            ], preview_colors=kind == "colorscheme")
            if name is None:
                return
        try:
            message = set_setting(config, kind, name)
        except (ValueError, OSError) as exc:
            print(f"\n  {title} unchanged: {exc}\n")
            return
        _sync_ui_context()
        print(f"\n  {message}\n")

    def _interactive_command(command):
        """Picker selections still pass through the existing validated dispatcher."""
        head, _, argument = command.partition(" ")
        head = head.lower()
        if head == "/shortcuts":
            from .shortcuts import browse_shortcuts

            _ui.run_panel(lambda: browse_shortcuts(config))
            return None
        if head in ("/help", "/h", "/?"):
            needle = argument.strip().casefold()
            options = [
                (spec.split()[0], spec, description)
                for spec, description in _command_choices
                if not needle or needle in (spec + " " + description).casefold()
            ]
            selected = _ui.choose("Commands · select to edit, Enter to send", options)
            if selected:
                _ui.prefill(selected + " ")
            return None
        if head == "/provider" and not argument.strip():
            selected = _ui.choose("Provider", [
                (name, name, _provider_note(name, provider))
                for name in sorted(RAW_FNS, key=lambda name: name != provider)
            ])
            return "/provider " + selected if selected else None
        return command

    # Read-only reports that may print while a turn runs: they only read
    # session state and never prompt.
    _LIVE_REPORTS = ("/status", "/cost", "/tools", "/profiles", "/skills",
                     "/facts", "/memories", "/mem", "/tasks")

    def _live_command(command):
        """A panel or report typed during a turn opens now, on its own thread,
        instead of waiting for the turn: the /help palette, the /model(s) and
        /provider pickers, the conversations page (/convos, /switch with no
        id), the subagent panel (/agents), and the read-only reports above. A
        pick that changes the session (model, provider, conversation) is
        queued to run when the turn ends. None for everything else, which
        still waits for the turn."""
        head, _, argument = command.partition(" ")
        head, argument = head.lower(), argument.strip()
        if head in ("/ebrake", "/colorscheme", "/infobar", "/notifications"):
            return lambda: _apply_session_control(head[1:], argument)
        if head in ("/help", "/h", "/?", "/shortcuts") or (head == "/provider" and not argument):
            def pick():
                chosen = _interactive_command(command)
                if chosen:
                    _ui.queue_command(chosen)
            return pick
        if head in ("/model", "/models", "/ls") and not argument:
            return _live_models
        if head in ("/convos", "/switch") and not argument:
            return _live_conversations
        if head in ("/agents", "/subagents") and not argument:
            def agents():
                # The subagent panel; a model setting goes through the
                # picker and back. x stops this session's running fan-out.
                from .agents_panel import admin_loop, run_agents_panel_during_turn
                from .model_picker import pick_model_during_turn

                stop = getattr(builtin_clients.get("fan_out"), "stop", None)
                admin_loop(
                    config, provider, model_name,
                    lambda select, notice: _ui.run_panel(
                        lambda: run_agents_panel_during_turn(
                            config, stop_fanout=stop, select=select, notice=notice)),
                    lambda cfg, prov, mod, purpose=None: _ui.run_panel(
                        lambda: pick_model_during_turn(cfg, prov, mod, purpose=purpose)))
            return agents
        if head in _LIVE_REPORTS and not argument:
            def report():
                handle_slash_command(
                    command, config, provider, model_name, set_agent_mode,
                    memory=memory, all_tools=chat_state.all_tools,
                    tool_map=chat_state.tool_map, sched=sched,
                    current_conv=current_conv, session_usage=session_usage,
                    messages=messages, server_status=lambda: _tool_loader.statuses(),
                )
            return report
        return None

    def _live_conversations():
        """The conversations page during a turn: its own store connection
        (the turn's thread owns conv_mgr), browse-only, and Enter queues the
        switch for when the turn ends."""
        from .convo_browser import browse_during_turn

        manager = ConversationManager()
        current_id = current_conv.id
        try:
            chosen = _ui.run_panel(
                lambda: browse_during_turn(manager, current_id, config=config))
        finally:
            manager.close()
        if chosen and chosen != current_id:
            _ui.queue_command(f"/switch {chosen}")

    def _live_models():
        """The model picker during a turn: role changes save at once; a new
        chat model is queued for when the turn ends."""
        from .model_picker import pick_model_during_turn

        def queue_primary(entry):
            _ui.queue_command(entry.switch_command())
            return True

        _ui.run_panel(lambda: pick_model_during_turn(
            config, provider, model_name, on_primary=queue_primary))

    def _run_slash_builtin(tool_name: str, arguments: dict):
        """Run a direct-user builtin through the same lifecycle hooks."""

        import json
        from .tooling import run_hook

        client = builtin_clients.get(tool_name)
        if client is None:
            print(f"\n  \033[31m{tool_name} is unavailable.\033[0m\n")
            return
        # Required policy fails closed; the user hook below stays fail-open.
        from .policy import evaluate_required_policy

        decision = evaluate_required_policy(
            "pre_tool_use",
            {"tool": tool_name, "arguments": arguments, "slash_command": True},
        )
        if not decision.allowed:
            print(
                "\n  \033[31mDenied by required policy:\033[0m "
                f"{decision.reason or decision.check or 'no reason given'}\n"
            )
            return
        allowed, hook_out = run_hook(
            "pre_tool_use",
            {"tool": tool_name, "arguments": arguments, "slash_command": True},
            config,
        )
        if not allowed:
            print(
                f"\n  \033[31mBlocked by pre_tool_use hook:\033[0m "
                f"{hook_out or 'no reason provided'}\n"
            )
            return
        if hook_out:
            try:
                rewritten = json.loads(hook_out)
            except json.JSONDecodeError:
                rewritten = None
            if not isinstance(rewritten, dict):
                print(
                    "\n  \033[31mHook returned invalid replacement "
                    "arguments; nothing ran.\033[0m\n"
                )
                return
            arguments = rewritten
        try:
            raw_result = client.call_tool(tool_name, arguments)
            blocks = raw_result.get("content", [])
            if isinstance(blocks, list):
                result_text = "\n".join(
                    str(
                        block.get("text", block.get("content", ""))
                        if isinstance(block, dict)
                        else block
                    )
                    for block in blocks
                ).strip()
            else:
                result_text = str(blocks)
        except Exception as exc:
            result_text = f"Error executing {tool_name}: {exc}"
        result_text = result_text or "(no output)"
        run_hook(
            "post_tool_use",
            {
                "tool": tool_name,
                "arguments": arguments,
                "result": result_text,
                "slash_command": True,
            },
            config,
        )
        color = "\033[31m" if result_text.startswith(("Error", "Refused")) else "\033[2m"
        print(f"\n  {color}{result_text}\033[0m\n")

    _set_foreground_policies()

    last_interrupt = 0.0
    _backend_failed = False  # preflight the server after a failed turn (plan 3.3)
    # /retry sets this to the previous user message; the next turn sends it
    # verbatim (attachments included) instead of building one from input.
    _replay_message = None
    # /image attachments waiting for the next message (canonical blocks).
    _pending_images: List[dict] = []
    _pending_update = None
    try:
        if _use_terminal_ui:
            from .terminal_ui import TerminalUI
            from .runtime import set_verbose_tools
            _ui = TerminalUI(lambda: _command_choices, history_file,
                             live_command=_live_command)
            _ui.start()
            set_verbose_tools(False)
            _sync_ui_context()
            if _repo_ctx is not None:
                # The snapshot usually lands while the user types the first
                # message: show its PR and check state without waiting.
                _repo_ctx.on_ready(lambda: _ui.set_context(repo=_repo_ctx.indicator()))
        _print_banner()
        while True:
            # MCP tools that finished loading since the last turn attach
            # here, before the prompt (and again after input is read, for
            # a message typed while they were still loading).
            _attach_loaded_tools()
            _sync_ui_context()
            try:
                if _ui:
                    user_input = _ui.read_message()
                else:
                    user_input = multiline.read_user_message(
                        "\033[1;33myou:\033[0m ", drain_fn=_drain_fn,
                    )
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                now = datetime.datetime.now().timestamp()
                if now - last_interrupt < 1.5:
                    print()
                    break
                last_interrupt = now
                print("\n  \033[2m(Ctrl+C again to exit)\033[0m\n")
                continue
            if user_input is None:
                print("  \033[2m(cancelled — nothing sent)\033[0m\n")
                continue

            stripped = user_input.strip()
            if not stripped:
                continue
            _attach_loaded_tools()
            _literal_block = False
            if multiline.is_ssh_command(user_input):
                execution = _choose_ssh_execution(stripped)
                if execution == "terminal":
                    user_input = stripped = f"/terminal {stripped}"
                elif execution == "model":
                    # Absolute SSH paths must remain model input, not slash commands.
                    _literal_block = True
                else:
                    print("  \033[2m(cancelled — nothing sent)\033[0m\n")
                    continue

            # /paste and /edit compose a literal message: the block they
            # return is never re-parsed as commands or exit words, and
            # multiline input (pasted or composed) is always chat — interior
            # lines must never dispatch as slash commands.
            if not _literal_block and multiline.is_slash_command(user_input):
                _head = stripped.split(maxsplit=1)
                _name = _head[0].lower()
                _arg = _head[1].strip() if len(_head) > 1 else ""
                if _name in ("/paste", "/edit"):
                    if _name == "/edit" or _arg in ("--editor", "-e"):
                        # Same gate as /notes' editor verbs: the editor only
                        # takes the terminal in an interactive local session
                        # with a live TTY (remote/channel sessions refuse).
                        _term = chat_state.tool_map.get("interactive_terminal")
                        _check = getattr(_term, "handoff_available", None)
                        if not callable(_check) or not _check():
                            print(
                                "  \033[31m/edit needs the interactive local"
                                " session — the same gate as /terminal."
                                "\033[0m\n  \033[2m/paste composes a literal"
                                " block without the editor.\033[0m\n"
                            )
                            continue
                        with _suspend_ui(discard=True):
                            block = multiline.edit_in_editor(config)
                        if block:
                            multiline.echo_message_block(
                                block, "\033[1;33myou:\033[0m "
                            )
                    else:
                        print(
                            "  \033[2mPaste now — end with a line that is "
                            "exactly '.' or Ctrl+D; Ctrl+C cancels.\033[0m"
                        )
                        with _suspend_ui():
                            block = multiline.read_paste_block()
                    if not block or not block.strip():
                        print("  \033[2m(nothing to send)\033[0m\n")
                        continue
                    user_input = block
                    stripped = user_input.strip()
                    _literal_block = True

            if not _literal_block and stripped.lower() in ("exit", "quit", "/q"):
                break

            _set_foreground_policies()

            if not _literal_block and multiline.is_slash_command(user_input):
                if _ui:
                    stripped = _interactive_command(stripped)
                    if stripped is None:
                        continue
                credential_command = stripped.split()[0].lower() in ("/keys", "/connect")
                with _suspend_ui(discard=credential_command):
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
                        server_status=lambda: _tool_loader.statuses(),
                    )
                if isinstance(result, tuple) and result[0] == "update":
                    _pending_update = result[1]
                    break
                if isinstance(result, tuple) and result[0] == "eval":
                    from .evals.ui import run_eval_command

                    # Non-live slash commands already queue until the active
                    # turn ends. Evaluation owns stdin, never the chat session.
                    with _suspend_ui():
                        run_eval_command(result[1], config, provider, model_name,
                                         input_fn=_safe_input)
                    continue
                if isinstance(result, tuple) and result[0] in ("ebrake", "colorscheme", "infobar", "notifications"):
                    _apply_session_control(*result)
                    continue
                # User-defined slash command: the rendered template becomes
                # this turn's user message (handled by the normal flow below).
                _custom_prompt = None
                if (
                    isinstance(result, tuple)
                    and result[0] == "run_builtin_tool"
                ):
                    _run_slash_builtin(result[1], result[2])
                    continue
                if isinstance(result, tuple) and result[0] in (
                    "attach_image", "attach_image_clear"
                ):
                    from .images import handle_attach_command
                    handle_attach_command(result, _pending_images)
                    continue
                if isinstance(result, tuple) and result[0] == "user_prompt":
                    _custom_prompt = result[1]
                    _prompt_meta = result[2] if len(result) > 2 else None
                    result = None
                    preview = _custom_prompt.strip().splitlines()[0][:70]
                    print(f"  \033[2m→ {preview}\033[0m")
                    if isinstance(_prompt_meta, dict) and _prompt_meta.get("tools"):
                        # A skill's declared tools are the operator's
                        # explicit offer: activate any the profile hid and
                        # pin them for the rest of the session.
                        from .tooling import ensure_tools_active
                        _added = ensure_tools_active(
                            chat_state, _prompt_meta["tools"]
                        )
                        if _added:
                            print(
                                f"  \033[2m(skill {_prompt_meta.get('skill')}: "
                                f"enabled {', '.join(_added)} for this "
                                "session)\033[0m"
                            )
                    if isinstance(_prompt_meta, dict) and _prompt_meta.get("rounds"):
                        # A skill's rounds: line is its declared budget
                        # (delegate_task honours it); the main-session
                        # default of 25 cut a scaffold+verify arc short.
                        _wanted = int(_prompt_meta["rounds"])
                        if _wanted > session.budgets.max_tool_rounds:
                            session.budgets.max_tool_rounds = _wanted
                            print(
                                f"  \033[2m(skill {_prompt_meta.get('skill')}: "
                                f"tool round budget raised to {_wanted} for "
                                "this session; /rounds to change)\033[0m"
                            )
                if result == "retry_last_prompt":
                    # Resend the last prompt exactly as stored (attachments
                    # included): drop it and everything after it, then fall
                    # through to the turn path like a user_prompt result.
                    from .conversations import prompt_text, retry_point
                    result = None
                    _retry_at = retry_point(messages)
                    if _retry_at is None:
                        print("\n  \033[2mNothing to retry\033[0m\n")
                        continue
                    _replay_message = messages[_retry_at]
                    del messages[_retry_at:]
                    current_conv.messages = messages
                    _custom_prompt = prompt_text(_replay_message).strip() or "(retry)"
                    print(f"  \033[2m↻ Retrying: "
                          f"{_custom_prompt.splitlines()[0][:70]}\033[0m")
                if result == "fork_conversation":
                    _save_current()
                    _parent_id = current_conv.id
                    current_conv = conv_mgr.fork(current_conv)
                    messages = current_conv.messages
                    builtin_clients["todo_list"].clear()
                    print(f"\n  \033[1;32m⑂ Forked into {current_conv.id}; "
                          f"{_parent_id} is unchanged\033[0m\n")
                    continue
                if isinstance(result, tuple) and result[0] == "compact_conversation":
                    if session.agents_paused:
                        print("\n  Agents are paused; /ebrake off before compacting.\n")
                        continue
                    from .runtime import (
                        auto_compact, calibration_key, can_compact, estimate_tokens,
                    )
                    if not can_compact(messages):
                        print("\n  \033[2mToo little history to compact yet.\033[0m\n")
                        continue
                    _est_key = calibration_key(provider, config)
                    _before = estimate_tokens(messages, key=_est_key)
                    if _ui:
                        _ui.set_busy(True)
                    try:
                        with session.agent_scope(), Spinner("Compacting history"):
                            _compacted = auto_compact(
                                messages, chat_state.tools, provider, config, raw_fn,
                                force=True, focus=result[1],
                            )
                    except KeyboardInterrupt:
                        _compacted = False
                    finally:
                        if _ui:
                            _ui.set_busy(False)
                    if not _compacted:
                        print("\n  \033[31mNothing compacted: the summary call failed "
                              "or was cancelled; history is unchanged.\033[0m\n")
                        continue
                    current_conv.messages = messages
                    _save_current()
                    _after = estimate_tokens(messages, key=_est_key)
                    print(f"\n  \033[1;32m✓ Compacted history\033[0m "
                          f"\033[2m(~{_before:,} → ~{_after:,} tokens)\033[0m\n")
                    continue
                if result == "new_conversation":
                    _save_current()
                    # Summarize in the background: the LLM call would
                    # otherwise block /new for the full generation time.
                    _summarize_and_save_async(
                        messages, config, raw_fn, memory, session=session)
                    current_conv = conv_mgr.create(
                        model=model_name, provider=provider, previous=current_conv)
                    messages = [{"role": "system", "content": system_prompt}]
                    current_conv.messages = messages
                    builtin_clients["todo_list"].clear()
                    print("\n  \033[1;32m✓ New conversation started\033[0m\n")
                    continue
                if result == "clear_conversation":
                    _infobar_turn = None
                    old_count = len([m for m in messages if m.get("role") == "user"])
                    messages.clear()
                    messages.append({"role": "system", "content": system_prompt})
                    current_conv.messages = messages
                    current_conv.previous_context = ""
                    builtin_clients["todo_list"].clear()
                    _save_current()
                    print(f"\n  \033[1;32m\u2713 Cleared {old_count} messages\033[0m\n")
                    continue
                if result == "reset_tool_calling":
                    from .runtime import reset_tool_calling
                    scrubbed = reset_tool_calling(messages)
                    chat_state.textual_tool_calls = 0
                    chat_state.force_tool_reminder = True
                    current_conv.messages = messages
                    _save_current()
                    print(
                        f"\n  \033[1;32m\u2713 Tool-calling reset\033[0m "
                        f"\033[2m({scrubbed} textual tool-call message(s) scrubbed; "
                        f"native tool-calling will be reinforced next turn)\033[0m\n"
                    )
                    continue
                if isinstance(result, tuple) and result[0] == "switch_conversation":
                    conv = conv_mgr.load(result[1])
                    if conv:
                        _switch_to(conv)
                        from .conversations import is_user_prompt

                        _prompts = sum(1 for m in messages if is_user_prompt(m))
                        print(f"\n  \033[1;32m\u2713 Resumed: {current_conv.title}\033[0m"
                              f" \033[2m({_prompts} messages)\033[0m\n")
                    else:
                        print(f"\n  \033[31mNo conversation with ID {result[1]}\033[0m\n")
                    continue
                if result == "reload_tools":
                    # /reload also re-reads ~/.config/conch/env so a key
                    # rotated from another terminal reaches this session
                    # (and the MCP servers restarted below) without a restart.
                    from .config import reload_env_file
                    reload_env_file()
                    _reload_tools()
                    continue
                if result == "agent_mode_changed":
                    _set_foreground_policies()
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
                    if _ui:
                        _ui.set_queue_enabled(True)
                elif result == "queue_off":
                    if _ui:
                        _ui.set_queue_enabled(False)
                elif isinstance(result, int):
                    session.budgets.max_tool_rounds = result
                elif result is not None:
                    # /model (including a picker choice) and /provider have
                    # validated and committed the route to session.config.
                    _refresh_model(normalize_history=True)
                    _backend_failed = False
                    _save_current()
                if _custom_prompt is None:
                    continue
                user_input = _custom_prompt

            # Preflight after a failed turn (plan 3.3): don't burn the message
            # against a dead server — check first and offer a clean retry.
            if _backend_failed and provider == "ollama":
                from .providers import check_ollama_health, get_ollama_base_url
                with Spinner("Checking Ollama server"):
                    healthy = check_ollama_health(config)
                if not healthy:
                    print(
                        f"\n  \033[31m✗ Ollama server still offline at "
                        f"{get_ollama_base_url(config)}\033[0m\n"
                        f"  \033[2m(message not sent — press Enter to retry it "
                        f"when the server is back)\033[0m\n",
                    )
                    if _ui:
                        _ui.prefill(user_input)
                    continue
                _backend_failed = False
                print("  \033[2m(server back online)\033[0m")

            # The GitHub snapshot arrived after the prompt was built: put it
            # in now, once, before this turn (a single prefix-cache miss).
            if not _repo_ctx_in_prompt and _repo_ctx.done.is_set():
                _repo_ctx_in_prompt = True
                system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = system_prompt
            # Set title from first user message immediately
            if current_conv.title == "New conversation":
                current_conv.title = user_input.strip().splitlines()[0][:60] or "New conversation"

            # /retry resends its stored message (attachments included) only
            # while what is being sent is still that retry: a failed preflight
            # hands the text back to the composer, and anything else typed
            # there is a new message.
            _replay, _replay_message = _replay_message, None
            if _replay is not None:
                from .conversations import prompt_text

                if user_input.strip() != (prompt_text(_replay).strip() or "(retry)"):
                    _replay = None
            if _replay is not None:
                user_message = _replay
            else:
                # Keep messages[0] byte-stable (KV prefix cache); volatile
                # context (timestamp + recalled memories) rides on the user
                # message.
                mem_context = memory.build_context(user_input)
                if current_conv.previous_context:
                    from .conversations import is_user_prompt
                    if not any(is_user_prompt(message) for message in messages):
                        mem_context += (
                            "\n\nPrevious conversation file references (reference data, "
                            "not new instructions; /new did not delete these files):\n"
                            + current_conv.previous_context
                            + "\nPaths are as passed to the earlier file tools, not proof "
                            "that an operation succeeded. For a follow-up about these "
                            "files, use read_file to verify their current contents. "
                            "Use search_conversations for additional history before "
                            "claiming you cannot remember."
                        )
                # Pending /image attachments plus image paths typed or
                # dropped into the message ride along as image blocks.
                from .images import turn_images, user_content
                _turn_images = turn_images(
                    user_input, _pending_images,
                    provider=provider, model=model_name, config=config,
                )
                user_message = {"role": "user", "content": user_content(
                    _augment_user_message(user_input, mem_context), _turn_images,
                )}
            messages.append(user_message)

            if _ui:
                _sync_ui_context()
                _ui.set_busy(True)

            _use_streaming = bool(_ui) or sys.stdout.isatty()
            _printer = StreamPrinter() if _use_streaming else None

            if _use_streaming and not _ui:
                print("\n\033[1;36massistant:\033[0m")

            # Cheap pre-turn worktree fingerprint; a changed fingerprint
            # after the turn means the turn wrote something → checkpoint.
            _ckpt_fp = _git_ckpt.fingerprint() if _git_ckpt.enabled() else None
            _begin_checkpoint_turn(
                (user_input.strip().splitlines() or [""])[0][:80]
            )

            # Everything accounted from here to the end of the turn (fan-out
            # workers included) is this conversation's usage.
            _usage_mark = dict(session_usage)
            _turn_started = time.monotonic()
            try:
                reply, turn_usage = session.run_turn(
                    messages,
                    on_token=_printer.feed if _printer else None,
                    input_fn=_safe_input,
                    steering_fn=_ui.take_steering if _ui else None,
                )
            except KeyboardInterrupt:
                if _printer:
                    _printer.flush()
                if _ui:
                    _ui.clear_activities()
                    # Stopping must not be undone by queued input starting
                    # new work: hand it back to the composer instead.
                    _ui.set_busy(False)
                    _ui.restore_pending()
                    print("  \033[33m\u26a0 Interrupted\033[0m\n")
                else:
                    print("\n\n  \033[33m\u26a0 Interrupted\033[0m\n")
                add_usage(current_conv.usage, session_usage, since=_usage_mark)
                _save_current()
                continue
            except BaseException:
                if _printer:
                    _printer.flush()
                raise
            finally:
                if _ui:
                    _ui.set_busy(False)

            _backend_failed = bool(turn_usage.get("error"))

            # A committed fallback already normalized the history in chat_turn.
            if (session.provider, session.model) != (provider, model_name):
                _refresh_model(normalize_history=False)

            if reply:
                messages.append({"role": "assistant", "content": reply})
                if _printer:
                    streamed = _printer.flush()
                    if not streamed.strip():
                        if _ui:
                            _ui.finish_response(highlight(reply))
                        else:
                            print(highlight(reply))
                            print()
                else:
                    print(f"\n\033[1;36massistant:\033[0m\n{highlight(reply)}\n")
            else:
                if _printer:
                    _printer.flush()
                if not _backend_failed:
                    # A failed request already reported itself in full.
                    print("\n\033[2m[no response]\033[0m\n")
                else:
                    print()

            # Display token/cost/speed info with a context-usage gauge.
            # Session accounting and the context warning always run; only
            # the stats line itself is gated (show_token_stats / /tks).
            in_tok = turn_usage.get("input_tokens", 0)
            out_tok = turn_usage.get("output_tokens", 0)
            used_model = turn_usage.get("model", model_name)
            if not (in_tok or out_tok) and reply:
                # The backend reported no usage (an OpenAI-compatible
                # server ignoring stream_options): fall back to the
                # calibrated estimator so the stats line still appears,
                # ~-labeled throughout (counts and tok/s alike).
                from .runtime import calibration_key, estimate_tokens

                _est_key = calibration_key(provider, config)
                out_tok = estimate_tokens(
                    [{"role": "assistant", "content": reply}], key=_est_key
                )
                in_tok = max(
                    estimate_tokens(messages, key=_est_key) - out_tok, 0
                )
                turn_usage = dict(
                    turn_usage,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    tokens_estimated=True,
                    speed_estimated=True,
                )
            if in_tok or out_tok:
                from .runtime import (
                    calibration_key,
                    estimate_tokens,
                    format_context_gauge,
                    get_context_limit,
                )
                cost_str = record_session_usage(
                    session_usage, provider, used_model, in_tok, out_tok, config
                )
                ctx_used = estimate_tokens(
                    messages, key=calibration_key(provider, config)
                )
                ctx_window = get_context_limit(provider, config)
                if _ui:
                    _infobar_turn = (current_conv.id, turn_usage, cost_str, used_model)
                    _sync_ui_context(context_used=ctx_used, context_window=ctx_window)
                else:
                    gauge = format_context_gauge(ctx_used, ctx_window)
                    stats_line = format_turn_stats_line(
                        turn_usage, cost_str, gauge, used_model, config
                    )
                    if stats_line:
                        print(stats_line)
                if ctx_window and ctx_used / ctx_window >= 0.8:
                    print(
                        f"  \033[33m⚠ Context {ctx_used / ctx_window * 100:.0f}% full — "
                        f"older history will be compacted soon (/clear or /new to reset)\033[0m"
                    )
            add_usage(current_conv.usage, session_usage, since=_usage_mark)
            if interactive:
                # A long turn may have outlasted the user's attention: tell
                # the desktop it ended (notify_after_seconds; 0 = off).
                from .notify import turn_finished

                turn_finished(config, time.monotonic() - _turn_started,
                              reply=reply or "", failed=_backend_failed,
                              conversation=current_conv.title)

            # Git checkpoint when the turn mutated the worktree.
            if _ckpt_fp is not None and _git_ckpt.fingerprint() != _ckpt_fp:
                _label = user_input.strip().splitlines()[0][:80]
                _snap = _git_ckpt.snapshot(_label)
                if _snap:
                    _n = len(_git_ckpt.list())
                    print(
                        f"  \033[2mcheckpoint #{_n} saved ({_snap['stat']})"
                        f" — /undo reverts, /checkpoint lists\033[0m"
                    )

            # Process any config changes made by the LLM via conch_config tool
            _cfg_client = builtin_clients["conch_config"]
            for _action in _cfg_client.pending_actions:
                if _action[0] == "set_model":
                    _new_prov, _new_mod = _action[1], _action[2]
                    # Registry-selected models (llamaidx/...) carry the
                    # adapter overrides (base_url, api_key_env name, model
                    # ids) resolved at selection time; they win over the
                    # provider defaults below. The registry name rides along
                    # for the switch note in the history.
                    _overrides = _action[3] if len(_action) > 3 else None
                    _registry_name = _action[4] if len(_action) > 4 else ""
                    _new_fn = RAW_FNS.get(_new_prov)
                    if _new_fn:
                        config["provider"] = _new_prov
                        config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(_new_prov, "")
                        config["chat_model"] = _new_mod
                        config["model"] = _new_mod
                        if _new_prov == "custom":
                            config["custom_model"] = _new_mod
                        if _overrides:
                            config.update(_overrides)
                        _refresh_model(normalize_history=True)
                        from .runtime import append_model_switch_note
                        append_model_switch_note(
                            messages,
                            provider=provider,
                            model=model_name,
                            config=config,
                            registry_name=_registry_name,
                        )
                        print(f"  \033[1;32m\u2713 Now using {provider}/{model_name}\033[0m")
                elif _action[0] == "set_rounds":
                    session.budgets.max_tool_rounds = _action[1]
                elif _action[0] == "clear_history":
                    old_count = len([m for m in messages if m.get("role") == "user"])
                    messages.clear()
                    messages.append({"role": "system", "content": system_prompt})
                    current_conv.messages = messages
                    current_conv.previous_context = ""
                    print(f"  \033[1;32m\u2713 Cleared {old_count} messages\033[0m")
                elif _action[0] == "new_conversation":
                    _save_current()
                    current_conv = conv_mgr.create(
                        model=model_name, provider=provider, previous=current_conv)
                    messages = [{"role": "system", "content": system_prompt}]
                    current_conv.messages = messages
                    print("  \033[1;32m\u2713 New conversation started\033[0m")
            _cfg_client.pending_actions.clear()

            _save_current()
    finally:
        if _ui:
            _ui.close()
        _save_current()
        if session_usage["turns"] > 0:
            total_in = session_usage["input_tokens"]
            total_out = session_usage["output_tokens"]
            turns = session_usage["turns"]
            cost_label = format_session_cost(session_usage, provider, model_name, config)
            print(f"\n\033[2mSession: {turns} turns, {total_in:,} in / {total_out:,} out tokens, {cost_label}\033[0m")
        try:
            if not session.agents_paused:
                _summarize_and_save_bounded(
                    messages, config, raw_fn, memory, session=session)
        except KeyboardInterrupt:
            pass
        sched.stop()
        if _remote_loop is not None:
            _remote_loop.stop()
        if not _use_terminal_ui:
            try:
                readline.write_history_file(history_file)
            except OSError:
                pass
        session.mcp_clients = mcp_clients
        session.close()
        conv_mgr.close()

    if _pending_update is not None:
        from .update import main as update_main

        print("Conversation saved. Run conch again after the update to resume.")
        raise SystemExit(update_main(_pending_update, prog="/update"))


class _ArgumentParser(argparse.ArgumentParser):
    """argparse with a usage error that also explains ``--``: a prompt
    whose first word starts with a dash is the one case a user would
    otherwise be stuck on."""

    def error(self, message):
        self.print_usage(sys.stderr)
        hint = ""
        if "unrecognized arguments" in message:
            hint = ("\n  (options are recognised only before the prompt;"
                    " put -- before a prompt that starts with a dash)")
        print(f"{self.prog}: error: {message}{hint}", file=sys.stderr)
        sys.exit(2)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="conch",
        usage="conch [-n | --new] [--non-interactive] [--] [prompt ...]\n"
              "       conch convos\n"
              "       conch auth [login|status|logout] [claude-code|codex]\n"
              "       conch keys [list|check|set NAME|rotate|doctor|...]\n"
              "       conch setup [--check]\n"
              "       conch update [--check] [--merge | --rebase]",
        description=(
            "The LLM-assisted shell. With no arguments, resumes your most"
            " recent\nconversation; any other arguments are sent as a"
            " one-shot prompt.\n\n"
            "conch convos browses, searches, and resumes saved conversations"
            " and shows\ntheir token use and cost (see conch convos --help).\n"
            "conch auth signs into Claude or ChatGPT seats through their"
            " official CLIs.\nconch keys manages API and integration keys"
            " (see conch keys --help).\n"
            "conch setup verifies your model and repairs sign-in interactively.\n"
            "conch update updates this installation, preserving local Git work."
        ),
        epilog=(
            "Options are recognised only before the prompt, so words inside"
            " a prompt are\nnever treated as options. Put -- before a prompt"
            " whose first word starts\nwith a dash:  conch -- -rf is the"
            " dangerous part, right?"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "-h", "--help", action="store_true",
        help="Show this help and exit.",
    )
    parser.add_argument(
        "-V", "--version", action="store_true",
        help="Show the version and exit.",
    )
    parser.add_argument(
        "-n", "--new", action="store_true",
        help=("Start the interactive shell with a fresh conversation\n"
              "instead of resuming the most recent one (same as /new\n"
              "inside the shell). Takes no prompt."),
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help=("Never prompt for approval: any command that would need a\n"
              "y/n answer is refused instead of run (fail closed), and the\n"
              "first-run wizard is skipped. Implied automatically when\n"
              "stdin is not a terminal (pipes, redirects, cron); the flag\n"
              "and the auto-detection are OR'd, so either one makes the\n"
              "session non-interactive and nothing re-enables prompting."),
    )
    return parser


def split_leading_options(argv):
    """Split argv into (option tokens, prompt words).

    Only tokens *before* the first non-option word are options; everything
    from that word on is the prompt, verbatim, so a prompt like
    ``what does -n mean`` keeps its ``-n``. A bare ``--`` ends the
    options and is dropped, which is how a prompt may start with a dash.
    """
    for index, token in enumerate(argv):
        if token == "--":
            return list(argv[:index]), list(argv[index + 1:])
        if not (token.startswith("-") and len(token) > 1):
            return list(argv[:index]), list(argv[index:])
    return list(argv), []


def parse_argv(argv):
    """-> (namespace, prompt words); exits 2 on an unknown leading option."""
    leading, prompt = split_leading_options(argv)
    namespace = build_arg_parser().parse_args(leading)
    return namespace, prompt


_USAGE = build_arg_parser().format_help()


def session_is_interactive(argv_flag: bool = False, stdin=None) -> bool:
    """Decide whether this process may prompt the operator for approvals.

    Non-interactive wins whenever either signal says so: an explicit
    ``--non-interactive`` flag, or a stdin that is not a terminal (a pipe,
    a redirect, ``< /dev/null``, cron). There is deliberately no way to
    force prompting back on — without a TTY an approval prompt cannot get
    a real answer, and no answer must never count as consent.
    """
    if argv_flag:
        return False
    stream = stdin if stdin is not None else sys.stdin
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


#: First words of argv that are subcommands, not prompts.
SUBCOMMANDS = {"keys": "conch.keys", "auth": "conch.auth",
               "convos": "conch.convo_browser", "update": "conch.update",
               "setup": "conch.onboarding"}


def main():
    raw_argv = sys.argv[1:]
    if raw_argv and raw_argv[0] in SUBCOMMANDS:
        # Administrative commands run before configuration or inference startup.
        import importlib

        command = importlib.import_module(SUBCOMMANDS[raw_argv[0]])
        sys.exit(command.main(raw_argv[1:]))
    options, argv = parse_argv(raw_argv)
    if options.version:
        from . import __version__
        print(f"conch {__version__}")
        return
    if options.help:
        print(_USAGE, end="")
        return
    new_conversation = options.new
    interactive = session_is_interactive(options.non_interactive)
    initial_check = None
    # First-run onboarding: on a truly unconfigured interactive launch
    # (real TTY, no config anywhere, no provider key), walk through
    # provider + key setup before anything else loads config. Pipes,
    # daemons, --non-interactive, and configured installs never see it.
    if interactive:
        from .onboarding import maybe_run_first_run_wizard

        initial_check = maybe_run_first_run_wizard()
    if new_conversation and argv:
        print("conch: --new starts the interactive shell and takes no "
              "prompt", file=sys.stderr)
        sys.exit(2)
    if argv:
        with Spinner("Starting Conch") if interactive else contextlib.nullcontext():
            config = load_config()
        apply_agent_mode_from_config(config)
        # Same startup model check as the shell: a one-shot in a terminal
        # may ask the user to pick an alternative; a piped/--non-interactive
        # one-shot walks fallback_models or fails closed. No silent swap.
        try:
            model_check = initial_check or ensure_working_model(config, interactive=interactive)
        except StartupError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(exc.code)
        try:
            provider, raw_fn = resolve_startup_provider(config)
        except StartupError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(exc.code)
        model_name, _warnings = resolve_startup_model(config, provider)
        if model_check.switched:
            provider, model_name = model_check.provider, model_check.model
        base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
        # Only a working model earns the GitHub fetch; a one-shot has a
        # single turn, so it waits a moment for the snapshot.
        from . import github_context

        _repo_ctx = github_context.start(config)
        if _repo_ctx is not None:
            _repo_ctx.done.wait(timeout=2.0)
        location = (
            _detect_location()
            if get_bool(config, "detect_location", False)
            else ""
        )
        system_prompt = _build_system_prompt(
            base_prompt, location, provider, model_name, config
        )
        user_text = " ".join(argv)
        memory = MemoryStore()
        mem_context = memory.build_context(user_text)
        if not interactive:
            print(_NON_INTERACTIVE_NOTICE, file=sys.stderr)
        session = build_agent_session(
            config,
            interactive=interactive,
            permissions=default_permissions(),
            memory=memory,
        )
        # Image paths in a one-shot prompt attach too; the 📎 lines go to
        # stderr so a piped reply on stdout stays clean.
        from .images import turn_images, user_content
        _turn_images = turn_images(
            user_text, [], provider=provider, model=model_name, config=config,
            out=lambda line: print(line, file=sys.stderr),
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content(
                _augment_user_message(user_text, mem_context), _turn_images,
            )},
        ]
        try:
            reply, _usage = session.run_turn(
                messages, max_tool_rounds=MAX_TOOL_ROUNDS
            )
            if reply:
                print(highlight(reply))
            else:
                print("[no response]", file=sys.stderr)
                sys.exit(1)
        finally:
            session.close()
    else:
        chat_loop(new_conversation=new_conversation, interactive=interactive,
                  startup_check=initial_check)


_NON_INTERACTIVE_NOTICE = (
    "\033[2m  (non-interactive: commands that need approval are refused, "
    "not run)\033[0m"
)
