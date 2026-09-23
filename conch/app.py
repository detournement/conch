"""Primary Conch chat runtime built from the extracted services."""

from __future__ import annotations

import codecs
import contextlib
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
from typing import List, Optional

from .bootstrap import (
    StartupError,
    apply_agent_mode_from_config,
    build_agent_session,
    load_runtime_tools as _load_runtime_tools,
    make_builtin_clients as _make_builtin_clients,
    make_scheduled_executor,
    resolve_ollama_startup_model,
    resolve_startup_model,
    resolve_startup_provider,
    route_scheduled_output as _route_scheduled_output,
    start_remote_loop,
    start_scheduler,
    start_task_backend,
    warn_unknown_cloud_model,
)
from .commands import handle_slash_command
from .config import get_bool, load_config
from .conversations import Conversation, ConversationManager
from .memory import MemoryStore
from .providers import DEFAULT_API_KEY_ENVS, RAW_FNS
from .render import highlight, StreamPrinter
from .runtime import sanitize_anthropic_messages
from .session import AgentSession
from .tooling import (
    LocalShellPolicy,
    ToolRuntimeState,
    default_permissions,
    get_agent_mode,
    set_agent_mode,
)
from . import mcp as mcp_mod
from . import multiline

# Names still importable from conch.app for backwards compatibility (tests
# and external callers); their implementations live in conch.bootstrap now.
__bootstrap_reexports__ = (
    resolve_ollama_startup_model,
    _route_scheduled_output,
    make_scheduled_executor,
    start_scheduler,
)


from .prompts import get_chat_prompt

CHAT_SYSTEM_PROMPT = None  # resolved per-provider at startup

MAX_TOOL_ROUNDS = 25  # default, adjustable via /rounds

AGENT_MODE_CONFIG_NOTICE = (
    "agent mode is ON by default (shell commands run without confirmation) — "
    "/agent to turn it off, or remove agent_mode from ~/.config/conch/config"
)


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
        from .runtime import (
            is_error_response,
            serialized_agent_execution,
            side_task_fn,
        )
        summary_fn, summary_config = side_task_fn(config, raw_fn, config)
        if summary_fn is None:
            return
        with serialized_agent_execution():
            response = summary_fn(summary_config, summary_messages, None)
        if is_error_response(response):
            return  # never save provider errors as permanent memories
        summary = response.get("content", "").strip()
        if summary:
            from .secretguard import CredentialRejected

            try:
                with serialized_agent_execution():
                    memory.add(
                        f"[Session summary] {summary}", source="summary"
                    )
            except CredentialRejected:
                # A summary quoting credential material is dropped whole —
                # same discipline as the mission-lesson gate. Losing one
                # best-effort summary beats persisting a secret.
                return
    except Exception:
        pass


def _summarize_and_save_async(messages: List[dict], config: dict, raw_fn, memory: MemoryStore):
    """Fire-and-forget session summary for /new: the LLM call inside
    _summarize_and_save can block for a long time on a busy backend, and
    starting a fresh conversation must not wait on it. Snapshots the
    transcript and summarizes on a daemon thread; the summary is
    best-effort, so losing it on an early exit is acceptable."""
    user_turns = [m for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if len(user_turns) < 2:
        return None
    snapshot = list(messages)
    thread = threading.Thread(
        target=_summarize_and_save,
        args=(snapshot, config, raw_fn, memory),
        daemon=True,
    )
    thread.start()
    return thread


EXIT_SUMMARY_TIMEOUT_S = 2.5


def _summarize_and_save_bounded(
    messages: List[dict], config: dict, raw_fn, memory: MemoryStore,
    timeout: float = EXIT_SUMMARY_TIMEOUT_S,
):
    """Exit-path session summary: give the LLM call a short, bounded window
    instead of blocking quit for the full generation time. Tradeoff: the
    worker is a daemon thread, so if the backend hasn't answered within the
    timeout the summary is lost when the interpreter exits — acceptable for
    a best-effort memory, and far better than hanging exit on a busy
    server."""
    thread = _summarize_and_save_async(messages, config, raw_fn, memory)
    if thread is not None:
        thread.join(timeout)


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
        self._thread: Optional[threading.Thread] = None
        self._old_settings = None
        self._suspended = False
        # Self-pipe: stop() writes one byte so the reader's select() wakes
        # immediately instead of finishing a 100ms tick, and the loop exits
        # without ever touching the stdin fd again.
        self._wake_r, self._wake_w = os.pipe()
        # Newlines with more input already pending are interior to a paste;
        # coalesce them so a block pasted mid-turn queues as one message.
        self.coalesce_pastes = True

    def start(self):
        if self._suspended:
            # A terminal handoff owns stdin: restarting capture here would
            # race the child for keystrokes (credential prompts included).
            return
        if not sys.stdin.isatty():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = None
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
        try:
            os.write(self._wake_w, b"\0")
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=1.0)
            if not self._thread.is_alive():
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

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop_for_handoff(self) -> str:
        partial = self.stop()
        if self.is_running():
            raise RuntimeError(
                "Conch input reader did not stop; terminal handoff refused"
            )
        return partial

    def suspend_for_handoff(self) -> tuple[str, list[str]]:
        """Park capture before a terminal handoff, or refuse the handoff.

        The latch is raised first so any concurrent ``start()`` becomes a
        no-op, then the reader is stopped and joined — if it cannot be
        proven parked, the latch is dropped and the handoff is refused.
        Returns the (partial, queued) input captured *before* the handoff
        so the caller can discard it explicitly.
        """
        self._suspended = True
        try:
            partial = self.stop()
        except BaseException:
            self._suspended = False
            raise
        if self.is_running():
            self._suspended = False
            raise RuntimeError(
                "Conch input reader did not stop; terminal handoff refused"
            )
        return partial, self.get_queued()

    def resume_after_handoff(self) -> None:
        """Purge the handoff window and lift the latch.

        Anything that reached these buffers while a child owned the terminal
        is credential-adjacent by definition: it is discarded, never echoed,
        and never queued as a message. The caller decides whether to
        ``start()`` capture again.
        """
        self._buffer = ""
        self._queued.clear()
        self._suspended = False

    @contextlib.contextmanager
    def handoff_guard(self, notify=None):
        """Bracket a terminal handoff with the full input/output handshake.

        Entry parks the reader (fail-closed), reports discarded pre-handoff
        input through ``notify``, and flushes Conch's stdout/stderr so no
        buffered text can surface inside the child's session. Exit purges
        anything captured during the window and lifts the suspension latch.
        """
        partial, queued = self.suspend_for_handoff()
        if (partial or queued) and notify is not None:
            notify()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError, ValueError):
                pass
        try:
            yield
        finally:
            self.resume_after_handoff()

    def get_queued(self) -> list[str]:
        lines = list(self._queued)
        self._queued.clear()
        return lines

    def _loop(self):
        # Read whole chunks at the fd level: sys.stdin.read(1) would slurp a
        # pasted block into Python's userspace buffer, after which select()
        # on the fd never fires and the rest of the paste is stranded.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        fd = sys.stdin.fileno()
        wake = self._wake_r
        prev_cr = False
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd, wake], [], [], 0.1)
                if wake in ready:
                    # stop() poked the self-pipe: drain it and exit at the
                    # loop check without touching the stdin fd again.
                    try:
                        os.read(wake, 64)
                    except OSError:
                        pass
                    continue
                if not ready or self._stop.is_set():
                    continue
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                if self._suspended:
                    # Handoff window: these bytes belong to the child's
                    # session (possibly credentials). Drop them unprocessed —
                    # never buffered, never queued, never echoed.
                    continue
                text = decoder.decode(data)
                idx = 0
                while idx < len(text):
                    ch = text[idx]
                    idx += 1
                    if ch == "\n" and prev_cr:
                        prev_cr = False
                        continue  # CRLF: the CR already ended this line
                    prev_cr = ch == "\r"
                    if ch in ("\r", "\n"):
                        pending = idx < len(text) or bool(
                            select.select([fd], [], [], 0.02)[0]
                        )
                        if self.coalesce_pastes and pending:
                            self._buffer += "\n"  # mid-paste newline: one block
                        elif self._buffer:
                            self._queued.append(self._buffer)
                            # Budget the preview to the terminal minus the
                            # "  (queued: )" chrome; format_queued_preview
                            # adds an explicit "… (+N …)" marker when it has
                            # to cut, so a long capture never looks lost.
                            # A stopping reader stays silent: printing while
                            # the main thread hands the terminal to a child
                            # (or to input()) would interleave with it.
                            if not self._stop.is_set():
                                cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                                preview = multiline.format_queued_preview(
                                    self._buffer, max(20, cols - 12)
                                )
                                sys.stderr.write(
                                    f"\r\033[K  \033[2m(queued: {preview})\033[0m\n"
                                )
                                sys.stderr.flush()
                            self._buffer = ""
                    elif ch in ("\x7f", "\x08"):
                        if self._buffer:
                            self._buffer = self._buffer[:-1]
                    elif ch == "\x03":
                        self._buffer = ""
                        self._stop.set()
                        break
                    elif ch >= " " or ch == "\t":
                        self._buffer += ch
            except (EOFError, OSError, ValueError):
                break


def _startup_conversation(conv_mgr, model_name, provider, system_prompt,
                          fresh=False):
    """Pick the conversation the shell starts in: the most recent one by
    default, a brand-new one when --new was passed or nothing usable
    exists (no history, or every stored file was corrupt and quarantined).
    Returns (conversation, messages) with messages ready for the loop."""
    current_conv = None if fresh else conv_mgr.get_most_recent()
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


def chat_loop(new_conversation=False):
    # Startup wiring lives in conch.bootstrap so headless modes can reuse it;
    # this function owns only the interactive composition (warnings printed
    # to stderr, startup failures become process exits).
    config = load_config()
    agent_mode_from_config = apply_agent_mode_from_config(config)
    try:
        provider, raw_fn = resolve_startup_provider(config)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(exc.code)

    model_name, model_warnings = resolve_startup_model(config, provider)
    for warning in model_warnings:
        print(f"\033[33m  ⚠ {warning}\033[0m", file=sys.stderr)

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
        interactive=True,
        permissions=default_permissions(),
        memory=memory,
    )
    session.budgets.max_tool_rounds = MAX_TOOL_ROUNDS
    builtin_clients = _make_builtin_clients(
        memory, config, interactive=True, permissions=session.permissions
    )
    session.attach_clients(builtin_clients)

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

    # Each scheduled run gets its own fresh session (bootstrap wiring); the
    # system prompt is read through a closure so provider/model switches keep
    # flowing into scheduled turns. With edge_daemon=true the kernel (and
    # the conch-edge daemon) owns scheduled tasks instead; without it this
    # is the classic in-process scheduler and conch.kernel is never imported.
    sched, sched_kind = start_task_backend(
        config, lambda: system_prompt, max_tool_rounds=MAX_TOOL_ROUNDS
    )

    session_usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0}

    conv_mgr = ConversationManager()
    current_conv, messages = _startup_conversation(
        conv_mgr, model_name, provider, system_prompt, fresh=new_conversation
    )

    history_file = _history_path()
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    try:
        readline.read_history_file(history_file)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(500)

    # Completion list comes from the shared command registry, plus
    # user-defined commands (~/.config/conch/commands/*.md).
    from .commands import load_user_commands, slash_command_names
    _SLASH_COMMANDS = list(dict.fromkeys(slash_command_names()))
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

    # Multiline input (README "Multiline input & paste"): GNU readline 8.1+
    # gets true bracketed paste; libedit (macOS system Pythons) ignores the
    # directive, so read_user_message's pending-input drain reassembles
    # pastes there instead. multiline_paste=false disables paste coalescing
    # (fences, backslash continuation, /paste, and /edit keep working).
    multiline.enable_bracketed_paste()
    _paste_coalesce = get_bool(config, "multiline_paste", True)
    _drain_fn = multiline.drain_pending_input if _paste_coalesce else (lambda: "")

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
        session.mcp_clients = mcp_clients
        chat_state.all_tools = new_state.all_tools
        chat_state.tool_map = new_state.tool_map
        chat_state.tools = new_state.tools
        chat_state.needs_tool_refresh = False
        print(f"  \033[1;32m{len(chat_state.tools)}/{len(chat_state.all_tools)} tools active\033[0m\n")

    def _print_banner():
        _print_conch_shell_art()
        from . import __version__
        print(f"\033[1;36mConch chat\033[0m \033[2mv{__version__} ({provider}/{model_name})\033[0m")
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

    # Wait for background tool loading (with a brief spinner if needed)
    if not _tools_ready.is_set():
        from .render import Spinner
        with Spinner("Loading tools"):
            _tools_ready.wait(timeout=30)
    mcp_clients = _bg_mcp_clients[0] or {}
    chat_state = _bg_chat_state[0] or ToolRuntimeState(all_tools=[], tool_map={}, tools=[])
    session.attach_clients(
        builtin_clients, chat_state=chat_state, mcp_clients=mcp_clients
    )

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

    # Subagent delegation reads live config/tool state (plan 3.1)
    builtin_clients["delegate_task"].bind_session(session)

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

    _print_banner()

    _typeahead = TypeaheadBuffer()
    _typeahead.coalesce_pastes = _paste_coalesce
    _typeahead_enabled = True
    _typeahead_queued: list[str] = []
    _typeahead_partial = ""
    _handoff_depth = 0

    # Turn-level git checkpoints (plan: maximally use local git when
    # writing code). Active only inside a git repo with git_checkpoints
    # on; every mutating turn snapshots to refs/conch/checkpoints/* so
    # /undo and /checkpoint restore work without touching user history.
    from .gitcheckpoint import GitCheckpoints

    _git_ckpt = GitCheckpoints(config)

    def _pause_typeahead():
        nonlocal _typeahead_partial
        partial = _typeahead.stop_for_handoff()
        queued = _typeahead.get_queued()
        if partial:
            _typeahead_partial += partial
        _typeahead_queued.extend(queued)

    def _safe_input(prompt):
        """Pause typeahead so input() can read stdin normally."""
        if _handoff_depth:
            return input(prompt)
        _pause_typeahead()
        try:
            return input(prompt)
        finally:
            if _typeahead_enabled:
                _typeahead.start()

    def _discard_notice():
        print(
            "  \033[2m(discarded pre-handoff typeahead for credential "
            "safety)\033[0m"
        )

    @contextlib.contextmanager
    def _terminal_handoff_context():
        """Suspend every Conch stdin reader while a child owns the terminal.

        The guard parks the reader fail-closed, discards pre-handoff
        typeahead with a visible notice, flushes Conch's own output so
        nothing buffered leaks into the child's session, and purges anything
        that reached the capture buffers during the window before capture is
        allowed to restart.
        """

        nonlocal _handoff_depth
        with _typeahead.handoff_guard(notify=_discard_notice):
            _handoff_depth += 1
            try:
                yield
            finally:
                _handoff_depth -= 1
        if _typeahead_enabled:
            _typeahead.start()

    def _set_foreground_policies():
        standard = LocalShellPolicy(
            interactive=True,
            allow_auto_execute=get_agent_mode(),
            input_fn=_safe_input,
        )
        handoff = LocalShellPolicy(
            interactive=True,
            allow_auto_execute=get_agent_mode(),
            input_fn=_safe_input,
            handoff_context=_terminal_handoff_context,
        )
        builtin_clients["local_shell"].set_policy(standard)
        builtin_clients["interactive_terminal"].set_policy(handoff)
        builtin_clients["ssh_remote"].set_policy(handoff)
        builtin_clients["skill_manage"].configure(
            interactive=True, input_fn=_safe_input
        )

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
    try:
        while True:
            if _typeahead_partial and "\n" in _typeahead_partial:
                # A block pasted while the model was streaming: libedit's
                # insert_text silently drops text containing newlines
                # (verified on macOS 3.9–3.14), so it can't be prefilled —
                # queue the whole block as one message instead.
                _typeahead_queued.append(_typeahead_partial)
                _typeahead_partial = ""
            if _typeahead_queued:
                user_input = _typeahead_queued.pop(0)
                multiline.echo_message_block(user_input, "\033[1;33myou:\033[0m ")
            else:
                if _typeahead_partial:
                    prefill = _typeahead_partial
                    _typeahead_partial = ""
                    readline.set_startup_hook(lambda: readline.insert_text(prefill))

                def _prompt_input(prompt):
                    # The typeahead prefill must ride on the first physical
                    # line only, not re-insert on every continuation read.
                    try:
                        return input(prompt)
                    finally:
                        readline.set_startup_hook()

                try:
                    user_input = multiline.read_user_message(
                        "\033[1;33myou:\033[0m ",
                        input_fn=_prompt_input,
                        drain_fn=_drain_fn,
                    )
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
                if user_input is None:
                    print("  \033[2m(cancelled — nothing sent)\033[0m\n")
                    continue

            stripped = user_input.strip()
            if not stripped:
                continue

            # /paste and /edit compose a literal message: the block they
            # return is never re-parsed as commands or exit words, and
            # multiline input (pasted or composed) is always chat — interior
            # lines must never dispatch as slash commands.
            _literal_block = False
            if multiline.is_slash_command(user_input):
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
                if (
                    isinstance(result, tuple)
                    and result[0] == "run_builtin_tool"
                ):
                    _run_slash_builtin(result[1], result[2])
                    continue
                if isinstance(result, tuple) and result[0] == "user_prompt":
                    _custom_prompt = result[1]
                    result = None
                    preview = _custom_prompt.strip().splitlines()[0][:70]
                    print(f"  \033[2m→ {preview}\033[0m")
                if result == "new_conversation":
                    _save_current()
                    # Summarize in the background: the LLM call would
                    # otherwise block /new for the full generation time.
                    _summarize_and_save_async(messages, config, raw_fn, memory)
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
                    else:
                        print(f"\n  \033[31mNo conversation with ID {result[1]}\033[0m\n")
                    continue
                if result == "reload_tools":
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
                    _typeahead_enabled = True
                elif result == "queue_off":
                    _typeahead_enabled = False
                elif isinstance(result, int):
                    session.budgets.max_tool_rounds = result
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
                print("\n\033[1;36massistant:\033[0m")

            # Cheap pre-turn worktree fingerprint; a changed fingerprint
            # after the turn means the turn wrote something → checkpoint.
            _ckpt_fp = _git_ckpt.fingerprint() if _git_ckpt.enabled() else None

            try:
                reply, turn_usage = session.run_turn(
                    messages,
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
            builtin_clients["conch_introspect"].update(provider, model_name)
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
                from .providers import estimate_cost
                from .runtime import (
                    calibration_key,
                    estimate_tokens,
                    format_context_gauge,
                    get_context_limit,
                )
                cost = estimate_cost(used_model, in_tok, out_tok)
                session_usage["input_tokens"] += in_tok
                session_usage["output_tokens"] += out_tok
                session_usage["cost"] += cost
                session_usage["turns"] += 1
                ctx_used = estimate_tokens(
                    messages, key=calibration_key(provider, config)
                )
                ctx_window = get_context_limit(provider, config)
                gauge = format_context_gauge(ctx_used, ctx_window)
                cost_str = f"~${cost:.4f}" if cost > 0.0001 else "free"
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
                        old_provider = provider
                        provider = _new_prov
                        model_name = _new_mod
                        raw_fn = _new_fn
                        config["provider"] = _new_prov
                        config["api_key_env"] = DEFAULT_API_KEY_ENVS.get(_new_prov, "")
                        config["chat_model"] = _new_mod
                        config["model"] = _new_mod
                        if _new_prov == "custom":
                            config["custom_model"] = _new_mod
                        if _overrides:
                            config.update(_overrides)
                        if provider != old_provider:
                            from .runtime import normalize_messages_on_switch
                            normalize_messages_on_switch(messages, provider)
                        # Any switch (even same-provider model change) must
                        # refresh the self-description in the system prompt.
                        base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
                        system_prompt = _build_system_prompt(base_prompt, _location_result[0], provider, model_name, config)
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] = system_prompt
                        from .runtime import append_model_switch_note
                        append_model_switch_note(
                            messages,
                            provider=provider,
                            model=model_name,
                            config=config,
                            registry_name=_registry_name,
                        )
                        _cfg_client.update(provider, model_name)
                        print(f"  \033[1;32m\u2713 Now using {provider}/{model_name}\033[0m")
                elif _action[0] == "set_rounds":
                    session.budgets.max_tool_rounds = _action[1]
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
        _typeahead.stop()
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
            _summarize_and_save_bounded(messages, config, raw_fn, memory)
        except KeyboardInterrupt:
            pass
        sched.stop()
        if _remote_loop is not None:
            _remote_loop.stop()
        try:
            readline.write_history_file(history_file)
        except OSError:
            pass
        session.mcp_clients = mcp_clients
        session.close()
        conv_mgr.close()


_USAGE = """\
usage: conch [--new] [prompt ...]

The LLM-assisted shell. With no arguments, resumes your most recent
conversation; any other arguments are sent as a one-shot prompt.

options:
  -h, --help     Show this help and exit.
  -V, --version  Show the version and exit.
  -n, --new      Start the interactive shell with a fresh conversation
                 instead of resuming the most recent one (same as /new
                 inside the shell)."""


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        from . import __version__
        print(f"conch {__version__}")
        return
    if argv and argv[0] in ("--help", "-h"):
        print(_USAGE)
        return
    # First-run onboarding: on a truly unconfigured interactive launch
    # (real TTY, no config anywhere, no provider key), walk through
    # provider + key setup before anything else loads config. Pipes,
    # daemons, and configured installs never see it.
    from .onboarding import maybe_run_first_run_wizard

    maybe_run_first_run_wizard()
    new_conversation = False
    if argv and argv[0] in ("--new", "-n"):
        new_conversation = True
        argv = argv[1:]
        if argv:
            print("conch: --new starts the interactive shell and takes no "
                  "prompt", file=sys.stderr)
            sys.exit(2)
    if argv:
        config = load_config()
        apply_agent_mode_from_config(config)
        try:
            provider, raw_fn = resolve_startup_provider(config)
        except StartupError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(exc.code)
        model_name = config.get("chat_model", config.get("model", ""))
        _model_warning = warn_unknown_cloud_model(provider, model_name)
        if _model_warning:
            print(f"\033[33m  ⚠ {_model_warning}\033[0m", file=sys.stderr)
            from .providers import get_fallback_model

            model_name = get_fallback_model(provider, config)
            config["model"] = model_name
            config["chat_model"] = model_name
        base_prompt = config.get("chat_system_prompt") or get_chat_prompt(provider, model_name, config)
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
        session = build_agent_session(
            config,
            interactive=True,
            permissions=default_permissions(),
            memory=memory,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _augment_user_message(user_text, mem_context)},
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
        chat_loop(new_conversation=new_conversation)
