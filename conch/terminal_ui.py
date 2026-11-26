"""Inline prompt-toolkit surface for the foreground interactive conversation.

The backend owns this object on its main thread. A single UI thread owns all
widgets and terminal modes; terminal handoffs rendezvous with that thread before
allowing any other reader to touch stdin.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import os
import signal
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Callable, Optional

from prompt_toolkit import Application
from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.run_in_terminal import in_terminal
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.output import ColorDepth, create_output
from prompt_toolkit.renderer import print_formatted_text
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from .shortcuts import meta_key
from .themes import style_for

_active: Optional["TerminalUI"] = None


def get_active_ui() -> Optional["TerminalUI"]:
    """Only the foreground backend may publish response/activity state."""
    ui = _active
    if (ui is not None and threading.get_ident() == ui._owner
            and not ui._suspended and not ui._stopped.is_set()):
        return ui
    return None


def _safe(text: object) -> str:
    """Keep widget and transcript text inert, including OSC/CSI introducers."""
    return "".join(c for c in str(text)
                   if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160))



class _Capture:
    """Line-buffer legacy output; flush is a real terminal-write barrier.

    Unlike StdoutProxy's asynchronous flush, the explicit barrier makes it
    possible to hand the terminal to a credential reader without pending writes.
    Ordinary output keeps the editor's input attached and in raw mode.

    Partial lines are buffered per thread: a panel command printing while a
    turn streams must not splice its half-written line into the turn's.
    """
    def __init__(self, ui, original):
        self.ui = ui
        self.original = original
        self.pending = {}

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        written = len(text)
        with self.ui._routing:
            if self.ui._direct or self.ui._stopped.is_set():
                return self.original.write(text)
            thread = threading.get_ident()
            pending = self.pending.pop(thread, "") + text
            if "\n" in pending:
                end = pending.rfind("\n") + 1
                text, pending = pending[:end], pending[end:]
                self.ui._enqueue_output(text, trusted=True)
            if pending:
                self.pending[thread] = pending
        return written

    def flush(self):
        with self.ui._routing:
            pending, self.pending = self.pending, {}
            for text in pending.values():
                if self.ui._direct or self.ui._stopped.is_set():
                    self.original.write(text)
                else:
                    self.ui._enqueue_output(text, trusted=True)
            if self.ui._direct or self.ui._stopped.is_set():
                self.original.flush()
            elif threading.get_ident() != self.ui._thread.ident:
                self.ui._call(self.ui._drain_writes).result()

    def __getattr__(self, name):
        return getattr(self.original, name)


def _composer_history(filename):
    """Import readline history once without changing its original file."""
    target = os.fspath(filename) + ".composer"
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return FileHistory(target)
    try:
        try:
            with open(filename, encoding="utf-8", errors="replace") as old:
                entries = old.read().splitlines()
        except FileNotFoundError:
            entries = []
        with os.fdopen(fd, "w", encoding="utf-8") as new:
            fd = None
            for entry in entries:
                if entry:
                    new.write("\n# Imported readline entry\n")
                    for line in entry.replace(" ⏎ ", "\n").split("\n"):
                        new.write("+" + line + "\n")
    except BaseException:
        if fd is not None:
            os.close(fd)
        os.unlink(target)
        raise
    return FileHistory(target)


@dataclass(eq=False)
class _PendingMessage:
    text: str
    kind: str
    reserved: bool = False

    @property
    def selects_model(self):
        if "\n" in self.text:
            return False
        head = self.text.split(None, 1)
        return bool(head and head[0].lower() in ("/model", "/models", "/ls"))


class UpdateRequested(Exception):
    """The user requested an update at a safe composer boundary."""


class TerminalUI:
    def __init__(self, commands: Callable, history_file: str,
                 live_command: Optional[Callable[[str], Optional[Callable[[], None]]]] = None):
        self._commands = commands
        # Panels and read-only commands typed during a turn: given the
        # command, returns a runner to start now (on its own thread) or None
        # to queue the command for when the turn ends, as before.
        self._live_command = live_command
        # True while a full-screen panel owns the terminal mid-turn: turn
        # output waits in the queue instead of painting over the panel.
        self._holding_output = False
        self._history = _composer_history(history_file)
        self._owner = threading.get_ident()
        self._routing = threading.RLock()
        self._condition = threading.Condition()
        self._messages = deque()
        self._open_pickers = 0
        self._queue_selection = None
        self._editing_message = None
        self._detail_text = ""
        self._detail_kind = None
        self._detail_reasoning = False
        self._deferred_echo = []
        self._details_visible = True
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._failure = None
        self._direct = True
        self._suspended = False
        self._handoff_depth = 0
        self._busy = False
        self._busy_started = 0.0
        self._queue_enabled = True
        self._context_fields = {}
        self._activities = {}
        self._tools = {}
        self._response = ""
        self._notice = ""
        self._update_available = ""
        self._update_requested = False
        self._modal = None
        self._modal_result = None
        self._modal_choices = []
        self._selection = 0
        self._command_selection = 0
        self._command_dismissed = None
        self._command_restore = None
        self._output_queue = deque()
        self._write_task = None
        self._futures = set()
        self._future_lock = threading.Lock()
        self._thread = None
        self._app = None
        self._input = None
        self._old_stdout = None
        self._old_stderr = None
        self._no_color = "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
        self._colorscheme = "default"
        self._preview_colorscheme = None

    def start(self):
        global _active
        if self._thread is not None:
            raise RuntimeError("TerminalUI cannot be started twice")
        if _active is not None:
            raise RuntimeError("An interactive terminal surface is already active")
        self._old_stdout, self._old_stderr = sys.stdout, sys.stderr
        self._stdout = _Capture(self, self._old_stdout)
        self._stderr = _Capture(self, self._old_stderr)
        self._thread = threading.Thread(target=self._run, name="conch-terminal", daemon=True)
        self._thread.start()
        try:
            self._ready.wait()
            self._raise_failure()
            if self._stopped.is_set():
                raise RuntimeError("Terminal UI stopped during startup")
            with self._routing:
                self._direct = False
                sys.stdout, sys.stderr = self._stdout, self._stderr
                _active = self
            return self
        except BaseException:
            self.close()
            raise

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._input = create_input(stdin=sys.stdin)
            self._output = create_output(stdout=self._old_stdout)
            with create_app_session(input=self._input, output=self._output):
                self._build()
                self._loop.run_until_complete(self._app.run_async(
                    pre_run=self._started, handle_sigint=False, set_exception_handler=False))
        except BaseException as exc:
            self._failure = exc
        finally:
            self._stopped.set()
            self._ready.set()
            with self._condition:
                self._condition.notify_all()
            if self._modal_result is not None and not self._modal_result.done():
                self._modal_result.set_exception(self._failure or EOFError())
            with self._future_lock:
                for future in self._futures:
                    if not future.done():
                        future.set_exception(self._failure or EOFError())
            if hasattr(self, "_loop"):
                tasks = asyncio.all_tasks(self._loop)
                for task in tasks:
                    task.cancel()
                if tasks:
                    self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                self._loop.close()
            if self._input is not None:
                self._input.close()
            self._restore_streams()

    def _started(self):
        self._ui_context = contextvars.copy_context()
        self._loop.set_exception_handler(self._loop_error)
        # This callback runs after run_async has entered raw mode and attached input.
        self._loop.call_soon(self._ready.set)

    def _loop_error(self, loop, context):
        self._failure = context.get("exception") or RuntimeError(context["message"])
        if not self._app.is_done:
            self._app.exit(exception=self._failure)

    def _restore_streams(self):
        global _active
        if sys.stdout is getattr(self, "_stdout", None):
            sys.stdout = self._old_stdout
        if sys.stderr is getattr(self, "_stderr", None):
            sys.stderr = self._old_stderr
        if _active is self:
            _active = None
        self._direct = True

    def _raise_failure(self):
        if self._failure is not None:
            raise self._failure

    def _call(self, function, *args):
        """Schedule work in the application's context and propagate failures."""
        result = concurrent.futures.Future()
        with self._future_lock:
            if self._stopped.is_set():
                result.set_exception(self._failure or EOFError())
                return result
            self._futures.add(result)

        async def invoke():
            try:
                value = function(*args)
                if asyncio.iscoroutine(value):
                    value = await value
                if not result.done():
                    result.set_result(value)
            except BaseException as exc:
                if not result.done():
                    result.set_exception(exc)
            finally:
                with self._future_lock:
                    self._futures.discard(result)

        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(invoke()), context=self._ui_context.copy())
        return result

    def _change(self, function, *args):
        self._raise_failure()
        def change():
            value = function(*args)
            self._app.invalidate()
            return value
        return self._call(change).result()

    def _build(self):
        self._editor = TextArea(
            multiline=True, history=self._history, prompt=[("class:prompt", "> ")],
            height=Dimension(min=1, max=7), dont_extend_height=True,
            read_only=Condition(lambda: self._update_requested))
        self._editor.buffer.on_text_changed += self._command_changed
        self._query = TextArea(multiline=False, prompt="Search: ")
        self._answer = TextArea(multiline=False, prompt="Answer: ")
        self._pending_editor = TextArea(
            multiline=True, prompt="Edit: ", height=Dimension(min=1, max=7),
            dont_extend_height=True)
        self._pending_list = self._text_window(self._pending_text, 8, focusable=True)
        self._query.buffer.on_text_changed += lambda _: self._reset_selection()
        self._command_window = Window(
            FormattedTextControl(self._command_text),
            height=self._command_height, dont_extend_height=True, wrap_lines=False)
        normal = HSplit([
            # Browsing temporarily lends the live panels' rows to the command
            # list. Their state and routing keep running, even on short screens.
            ConditionalContainer(HSplit([
                ConditionalContainer(self._text_window(self._response_text, 6),
                                     Condition(lambda: bool(self._response))),
                ConditionalContainer(HSplit([
                    Window(FormattedTextControl([("class:title", "Reasoning / progress")]),
                           height=1, wrap_lines=False),
                    self._text_window(self._details_text, 4),
                ]), Condition(lambda: bool(self._detail_text) and self._details_visible)),
                ConditionalContainer(self._text_window(self._activity_text, 4),
                                     Condition(lambda: bool(self._busy or self._activities or self._tools))),
                self._text_window(self._status_text, 3),
            ]), Condition(self._live_panels_visible)),
            ConditionalContainer(
                Window(FormattedTextControl(self._pause_text), height=1, wrap_lines=False),
                Condition(self._pause_visible)),
            ConditionalContainer(
                Window(FormattedTextControl(self._infobar_text),
                       height=self._infobar_height, dont_extend_height=True,
                       wrap_lines=False),
                Condition(lambda: self._infobar_height() > 0)),
            ConditionalContainer(
                Window(FormattedTextControl(self._update_text), height=1, wrap_lines=False),
                Condition(self._update_visible)),
            self._editor,
            ConditionalContainer(self._command_window, Condition(self._command_visible)),
            ConditionalContainer(self._text_window(self._hints, 1),
                                 Condition(self._hints_visible)),
        ])
        modal = HSplit([
            self._text_window(lambda: [("class:title", _safe(self._modal_title))], 5),
            ConditionalContainer(self._query, Condition(lambda: self._modal == "choose")),
            ConditionalContainer(self._text_window(self._choice_text, 8),
                                 Condition(lambda: self._modal == "choose")),
            ConditionalContainer(self._answer, Condition(lambda: self._modal == "ask")),
            ConditionalContainer(self._pending_list, Condition(lambda: self._modal == "queue")),
            ConditionalContainer(self._pending_editor,
                                 Condition(lambda: self._modal == "queue-edit")),
            self._text_window(self._modal_hints, 2),
        ])
        root = HSplit([
            ConditionalContainer(normal, Condition(lambda: self._modal is None)),
            ConditionalContainer(modal, Condition(lambda: self._modal is not None)),
        ])
        keys = KeyBindings()

        @keys.add("enter", eager=True)
        def enter(event):
            if self._modal == "ask":
                self._finish_modal(self._answer.text)
            elif self._modal == "choose":
                choices = self._filtered_choices()
                if choices:
                    self._finish_modal(choices[self._selection][0])
            elif self._modal == "queue":
                self._edit_pending()
            elif self._modal == "queue-edit":
                self._save_pending()
            elif self._command_visible() and self._command_matches():
                self._accept_command(execute=True)
            else:
                self._send()

        @keys.add("escape", "enter", eager=True)
        def newline(event):
            if self._modal is None:
                self._editor.buffer.insert_text("\n")
            elif self._modal == "queue-edit":
                self._pending_editor.buffer.insert_text("\n")

        @keys.add("escape", "q", eager=True)
        def followup(event):
            if self._modal is None:
                self._send(followup=True)

        @keys.add("c-q", eager=True)
        def pending(event):
            if self._modal is None:
                self._open_modal("queue", "Pending messages")
            elif self._modal in ("queue", "queue-edit"):
                self._finish_modal()

        # Mac laptops send backspace for the key labelled "delete".
        @keys.add("delete", filter=Condition(lambda: self._modal == "queue"), eager=True)
        @keys.add("backspace", filter=Condition(lambda: self._modal == "queue"), eager=True)
        def delete_pending(event):
            self._delete_pending()

        @keys.add("escape", "r", eager=True)
        def toggle_details(event):
            if self._modal is None:
                self._details_visible = not self._details_visible

        @keys.add("escape", "u", eager=True,
                  filter=Condition(lambda: self._modal is None and bool(self._update_available)))
        def update_and_restart(event):
            self._request_update()

        @keys.add("c-p", eager=True)
        def commands(event):
            if self._modal is None:
                self._open_commands()

        @keys.add("tab", filter=Condition(self._command_visible), eager=True)
        def complete_command(event):
            self._accept_command(execute=False)

        @keys.add("up", filter=Condition(self._command_visible), eager=True)
        def previous_command(event):
            self._move_command(-1)

        @keys.add("down", filter=Condition(self._command_visible), eager=True)
        def next_command(event):
            self._move_command(1)

        @keys.add("pageup", filter=Condition(self._command_visible), eager=True)
        def previous_command_page(event):
            self._move_command(-self._command_page_size())

        @keys.add("pagedown", filter=Condition(self._command_visible), eager=True)
        def next_command_page(event):
            self._move_command(self._command_page_size())

        # Do not make this eager: Escape is also the Alt+Enter/Alt+Q prefix.
        @keys.add("escape", filter=Condition(self._command_visible))
        def dismiss_commands(event):
            self._dismiss_commands()

        @keys.add("up", filter=Condition(lambda: self._modal in ("choose", "queue")), eager=True)
        def up(event):
            if self._modal == "queue":
                self._move_pending(-1)
            else:
                self._select_choice(self._selection - 1)

        @keys.add("down", filter=Condition(lambda: self._modal in ("choose", "queue")), eager=True)
        def down(event):
            if self._modal == "queue":
                self._move_pending(1)
            else:
                self._select_choice(self._selection + 1)

        # Eager wherever Escape is not also the Alt prefix of a live binding;
        # otherwise cancelling waits a full second for a possible next key.
        @keys.add("escape", eager=True,
                  filter=Condition(lambda: self._modal not in (None, "queue-edit")))
        @keys.add("escape", filter=Condition(lambda: self._modal == "queue-edit"))
        def escape(event):
            self._cancel_modal()

        @keys.add("c-c", eager=True)
        def interrupt(event):
            if self._modal is not None:
                self._cancel_modal()
            elif self._busy:
                os.kill(os.getpid(), signal.SIGINT)
            else:
                self._editor.buffer.reset()
                with self._condition:
                    self._messages.append(KeyboardInterrupt())
                    self._condition.notify_all()

        @keys.add("c-d", eager=True)
        def eof(event):
            if self._modal is not None:
                self._cancel_modal()
            elif not self._editor.text:
                with self._condition:
                    self._messages.append(EOFError())
                    self._condition.notify_all()
            else:
                self._editor.buffer.delete()

        self._app = Application(
            layout=Layout(root, focused_element=self._editor), key_bindings=keys,
            full_screen=False,
            mouse_support=Condition(lambda: self._preview_colorscheme is not None),
            erase_when_done=True,
            input=self._input, output=self._output, refresh_interval=0.25,
            color_depth=lambda: (ColorDepth.DEPTH_1_BIT
                                 if self._no_color or (self._preview_colorscheme
                                                       or self._colorscheme) == "monochrome"
                                 else None),
            style=style_for(self._colorscheme))
        self._app.ttimeoutlen = 0.05
        self._app.timeoutlen = 0.3

    def _command_query(self):
        document = self._editor.buffer.document
        text = document.text
        if (self._modal is not None or not text.startswith("/")
                or any(char.isspace() for char in text)
                or document.cursor_position != len(text)
                or self._command_dismissed == text):
            return None
        return text[1:].casefold()

    def _command_visible(self):
        return self._command_query() is not None

    def _command_changed(self, buffer):
        self._command_selection = 0
        self._command_dismissed = None
        # Once a temporary Ctrl+P query becomes ordinary input, it is the
        # user's new draft; Escape must no longer restore an obsolete draft.
        if not buffer.text.startswith("/") or any(c.isspace() for c in buffer.text):
            self._command_restore = None

    def _command_matches(self):
        query = self._command_query()
        if query is None:
            return []
        matches = []
        for spec, description in self._commands():
            parts = spec.split()
            if not parts:
                continue
            name = parts[0].lstrip("/").casefold()
            if query not in name and query not in description.casefold():
                continue
            rank = (0 if name == query else 1 if name.startswith(query)
                    else 2 if query in name else 3)
            matches.append((rank, spec, description))
        # Stable sorting preserves the catalog's order within each rank.
        matches.sort(key=lambda match: match[0])
        return [(spec, description) for _, spec, description in matches]

    def _open_commands(self):
        document = self._editor.buffer.document
        self._command_dismissed = None
        if self._command_query() is None:
            self._command_restore = document
            self._editor.buffer.document = Document("/", 1)
        self._command_selection = 0

    def _dismiss_commands(self):
        restore = self._command_restore
        self._command_restore = None
        if restore is not None:
            self._editor.buffer.document = restore
        self._command_dismissed = self._editor.text

    def _move_command(self, amount):
        self._command_selection = max(
            0, min(len(self._command_matches()) - 1, self._command_selection + amount))

    def _accept_command(self, execute):
        matches = self._command_matches()
        if not matches:
            return
        index = min(self._command_selection, len(matches) - 1)
        parts = matches[index][0].split()
        text = parts[0] + (" " if not execute and len(parts) > 1 else "")
        self._command_restore = None
        self._editor.buffer.document = Document(text, len(text))
        self._command_dismissed = text
        if execute:
            self._send()

    def _command_budget(self):
        # Reserve the actual bar height, not a fixed row: detail can wrap.
        rows = self._output.get_size().rows
        reserved = (1 + self._hints_visible() + self._pause_visible()
                    + self._infobar_height() + self._update_visible())
        return min(11, max(0, rows - reserved))

    def _command_page_size(self):
        return max(1, self._command_budget() - 1)

    def _command_height(self):
        count = len(self._command_matches())
        return min(self._command_budget(), count + 1 if count else 1)

    @staticmethod
    def _command_clip(text, width):
        """Sanitize to a single row and truncate by terminal cells."""
        text = "".join(" " if char.isspace() else char for char in _safe(text))
        if width <= 0:
            return ""
        if get_cwidth(text) <= width:
            return text
        visible, used = [], 0
        for char in text:
            cells = get_cwidth(char)
            if used + cells > width - 1:
                break
            visible.append(char)
            used += cells
        return "".join(visible) + "…"

    def _command_text(self):
        width = max(0, self._output.get_size().columns - 1)
        choices = self._command_matches()
        if not choices:
            return [("class:muted", self._command_clip("No matches", width))]
        self._command_selection = min(self._command_selection, len(choices) - 1)
        page = self._command_page_size()
        start = min(max(0, self._command_selection - page + 1),
                    max(0, len(choices) - page))
        visible = choices[start:start + page]
        labels = [" ".join(_safe(spec).split()) for spec, _ in choices]
        column = min(28, max(get_cwidth(label) for label in labels), max(1, width // 2))
        fragments = []
        for index, (_, description) in enumerate(visible, start):
            selected = index == self._command_selection
            marker = "> " if selected else "  "
            if width < 12:
                text = marker + labels[index]
            else:
                label = self._command_clip(labels[index], column)
                gap = " " * (column - get_cwidth(label) + 2)
                text = marker + label + gap + " ".join(_safe(description).split())
            fragments.append(("class:selected" if selected else "",
                              self._command_clip(text, width)))
            if index != start + len(visible) - 1:
                fragments.append(("", "\n"))
        if self._command_budget() > 1:
            position = f"{self._command_selection + 1}/{len(choices)}"
            fragments.extend([
                ("", "\n"),
                ("class:muted", self._command_clip(
                    f"{position} · {start + 1}–{start + len(visible)}", width)),
            ])
        return fragments

    @staticmethod
    def _text_window(text, maximum, focusable=False):
        return Window(FormattedTextControl(text, focusable=focusable),
                      height=Dimension(min=1, max=maximum),
                      dont_extend_height=True, wrap_lines=True)

    def _tail_text(self, text, rows):
        """Keep a tail of display rows, not characters or logical lines."""
        width = max(1, self._output.get_size().columns)
        lines = deque(maxlen=rows)
        # Expanding tabs also keeps the control's measured width equal to the
        # width of the text it draws. Wide glyphs must not straddle a row.
        for line in text.splitlines()[-rows:]:
            line = _safe(line).expandtabs()
            current, used = [], 0
            for char in line:
                cells = get_cwidth(char)
                if used + cells > width and current:
                    lines.append("".join(current))
                    current, used = [], 0
                current.append(char)
                used += cells
            lines.append("".join(current))
        return "\n".join(lines)

    def _response_text(self):
        # The final answer goes to scrollback; only its bounded tail stays live.
        return [("", self._tail_text(self._response, 6)),
                ("[SetCursorPosition]", "")]

    def _activity_text(self):
        now = time.monotonic()
        lines = [f"{label} · {int(now - started)}s"
                 for label, started in self._activities.values()]
        lines += [f"{name}{': ' + summary if summary else ''} · {int(now - started)}s"
                  for name, summary, started in self._tools.values()]
        if self._busy and not lines:
            lines.append(f"Generating · {int(now - self._busy_started)}s")
        if len(lines) > 4:
            lines = lines[-3:] + [f"{len(lines) - 3} more active"]
        return [("class:muted", _safe("\n".join(lines)))]

    def _status_text(self):
        fields = self._context_fields
        permission = {
            "prompt_all": "ask first", "safe_auto": "safe auto",
            "accept_edits": "accept edits", "yolo": "AUTO-EXECUTE",
        }.get(fields.get("permission"), fields.get("permission", ""))
        parts = [permission] if permission else []
        with self._condition:
            steering = sum(isinstance(item, _PendingMessage) and item.kind == "steering"
                           for item in self._messages)
            followups = sum(isinstance(item, _PendingMessage) and item.kind == "follow-up"
                            for item in self._messages)
        if steering:
            parts.append(f"{steering} steering")
        if followups:
            parts.append(f"{followups} follow-up")
        width = self._output.get_size().columns
        if fields.get("session_cost"):
            parts.append(f"cost {fields['session_cost']}")
            if get_cwidth(_safe(" · ".join(parts))) > width - 1:
                # Keep billing, permissions and queue visible before spending
                # scarce columns on the model and conversation title.
                priority = " · ".join(parts[:-1])
                text = f"{priority}\n{parts[-1]}" if priority else parts[-1]
                return [("class:status", _safe(text))]
        if fields.get("repo"):
            # Where the session is (🐙 owner/repo ⎇ branch #PR ✓) before
            # the model: the clip drops the title and tool count first.
            parts.append(str(fields["repo"]))
        model = "/".join(str(fields[key]) for key in ("provider", "model") if fields.get(key))
        if model and fields.get("tuning"):
            # Effort/context chosen for this model in the picker.
            model += f" ({fields['tuning']})"
        if model:
            parts.append(model)
        if fields.get("conversation"):
            parts.append(str(fields["conversation"]))
        if fields.get("tools") is not None:
            parts.append(f"tools: {fields['tools']}")
        return [("class:status", self._clip(" · ".join(parts)))]

    def _hints_visible(self):
        return self._output.get_size().rows >= 4

    def _update_visible(self):
        return bool(self._update_available) and self._output.get_size().rows >= 5

    def _pause_visible(self):
        return (bool(self._context_fields.get("agents_paused"))
                and self._output.get_size().rows >= (4 if self._command_visible() else 3))

    def _live_panels_visible(self):
        if self._command_visible():
            return False
        minimum = (1 + bool(self._response)
                   + 2 * bool(self._detail_text and self._details_visible)
                   + bool(self._busy or self._activities or self._tools))
        reserved = (1 + self._hints_visible() + self._pause_visible()
                    + self._infobar_height() + self._update_visible())
        return minimum <= self._output.get_size().rows - reserved

    @staticmethod
    def _wrap_infobar(fragments, width, maximum):
        """Word-wrap styled text by display cells, bounding both dimensions."""
        if width <= 0 or maximum <= 0:
            return []
        words, word = [], []
        for style, text in fragments:
            for char in _safe(text):
                if char.isspace():
                    if word:
                        words.append(word)
                        word = []
                else:
                    word.append((style, char))
        if word:
            words.append(word)
        rows, row, used = [], [], 0
        for word in words:
            cells = sum(get_cwidth(char) for _, char in word)
            if row and used + 1 + cells > width:
                rows.append(row)
                row, used = [], 0
            elif row:
                row.append(("class:status", " "))
                used += 1
            for style, char in word:
                cells = get_cwidth(char)
                if used + cells > width:
                    rows.append(row)
                    row, used = [], 0
                if cells <= width:
                    row.append((style, char))
                    used += cells
        if row:
            rows.append(row)
        if len(rows) > maximum:
            rows = rows[:maximum]
            last = rows[-1]
            while last and sum(get_cwidth(char) for _, char in last) >= width:
                last.pop()
            last.append(("class:muted", "…"))
        return rows

    def _infobar_rows(self):
        fields = self._context_fields
        if not fields.get("show_token_stats", True):
            return []
        size = self._output.get_size()
        width = max(0, size.columns - 1)
        # On very short screens the composer and selected command win.
        maximum = min(2, size.rows - 1 - self._hints_visible()
                      - self._pause_visible() - self._command_visible()
                      - self._update_visible())
        if width <= 0 or maximum <= 0:
            return []
        if fields.get("infobar", "detail") == "progress":
            capacity = fields.get("context_window")
            if not capacity or capacity <= 0:
                return self._wrap_infobar([("class:muted", "ctx unknown")], width, maximum)
            used = max(0, fields.get("context_used", 0))
            percent = used / capacity * 100
            style = ("class:error" if percent >= 80 else
                     "class:warning" if percent >= 60 else "class:ok")
            summary = f"~{percent:.0f}% / {capacity:,}"
            if len(summary) + 10 <= width:
                ruler_width = width - len(summary) - 7
                prefix, suffix = "ctx [", f"] {summary}"
            else:
                if len(summary) > width:
                    unit, divisor = ("m", 1_000_000) if capacity >= 1_000_000 else ("k", 1_000)
                    budget = f"{capacity / divisor:g}{unit}" if capacity >= 1_000 else str(capacity)
                    summary = f"~{percent:.0f}% / {budget}"
                rows = self._wrap_infobar([(style, summary)], width, 1)
                if maximum == 1:
                    return rows
                ruler_width = max(0, width - 2)
                prefix, suffix = "[", "]"
            filled = min(ruler_width, int(ruler_width * min(1, used / capacity)))
            ruler = [("class:muted", prefix), (style, "=" * filled),
                     ("class:rule", "-" * (ruler_width - filled)),
                     ("class:muted", suffix)]
            if prefix == "[":
                return rows + [ruler]
            return [ruler]
        # ANSI is a transport format only. Semantic classes keep severity while
        # allowing live theme switches and color-free output to work normally.
        fragments = []
        for ansi_style, text in to_formatted_text(ANSI(fields.get("infobar_stats", ""))):
            if "[ZeroWidthEscape]" in ansi_style:
                continue
            colors = ansi_style.split()
            style = ("class:error" if "ansired" in colors else
                     "class:warning" if "ansiyellow" in colors else
                     "class:ok" if "ansigreen" in colors else "class:status")
            fragments.append((style, text))
        return self._wrap_infobar(fragments, width, maximum)

    def _infobar_height(self):
        return len(self._infobar_rows())

    def _infobar_text(self):
        fragments = []
        for row in self._infobar_rows():
            if fragments:
                fragments.append(("", "\n"))
            fragments.extend(row)
        return fragments

    def _pause_text(self):
        return [("class:warning", self._clip("PAUSED · /ebrake off resumes"))]

    def _clip(self, text):
        """One terminal row of text, ending in an ellipsis when cut."""
        width = self._output.get_size().columns
        visible, used = [], 0
        for char in _safe(text):
            cells = get_cwidth(char)
            if used + cells > width - 1:
                visible.append("…")
                break
            visible.append(char)
            used += cells
        return "".join(visible)

    def _details_text(self):
        return [("class:muted", self._tail_text(self._detail_text, 4)),
                ("[SetCursorPosition]", "")]

    def _update_text(self):
        action = "Updating..." if self._update_requested else f"{meta_key('U')} update & restart"
        return [("class:warning", self._fit_hints(
            [action, f"Update available: {self._update_available}"]))]

    def _request_update(self):
        if self._update_requested or not self._update_available or self._modal is not None:
            return
        if self._busy:
            self._notice = "Finish or stop the current turn before updating"
        elif self._editor.text:
            self._notice = "Send or clear the draft before updating"
        elif self._context_fields.get("pending_attachments"):
            self._notice = "Send attached images or /image clear before updating"
        else:
            with self._condition:
                if self._messages or self._open_pickers:
                    self._notice = "Finish pending messages and close pickers before updating"
                else:
                    self._update_requested = True
                    self._messages.append(UpdateRequested())
                    self._condition.notify_all()

    def set_update_available(self, label: str):
        """Publish the background result, even if shutdown won the race."""
        if self._stopped.is_set():
            return
        try:
            self._change(setattr, self, "_update_available", " ".join(_safe(label).split()))
        except (EOFError, RuntimeError):
            if not self._stopped.is_set():
                raise

    def _cancel_queued_update(self):
        with self._condition:
            for message in self._messages:
                if isinstance(message, UpdateRequested):
                    self._messages.remove(message)
                    break
            self._update_requested = False

    def cancel_update_request(self, reason: str):
        """Return to editing if backend-only pending input prevents shutdown."""
        def cancel():
            self._cancel_queued_update()
            self._notice = _safe(reason)
        self._change(cancel)

    def _hints(self):
        # Most important first: a narrow terminal drops hints from the end
        # instead of cutting one mid-word.
        if self._command_visible():
            return [("class:muted", self._fit_hints(
                ["Enter runs", "Tab fills", "Escape closes", "Up/Down selects"]))]
        with self._condition:
            pending = any(isinstance(item, _PendingMessage) for item in self._messages)
        if self._notice:
            hints = [self._notice]
        elif self._context_fields.get("agents_paused"):
            hints = ["/ebrake off resumes", "Ctrl+C stops", "Ctrl+P commands"]
        elif self._busy and self._queue_enabled:
            hints = ["Enter steers", "Ctrl+C stops", f"{meta_key('Q')} follow-up"]
        elif self._busy:
            hints = ["Queue off · draft kept", "Ctrl+C stops"]
        else:
            hints = ["Enter sends", "Ctrl+P commands", "Ctrl+D exits"]
        if pending:
            hints.append("Ctrl+Q pending")
        if self._detail_text:
            hints.append(f"{meta_key('R')} details")
        if not self._notice:
            hints.append(f"{meta_key('Enter')} newline")
        return [("class:muted", self._fit_hints(hints))]

    def _fit_hints(self, hints):
        """One row of hints, most important first, dropped from the end."""
        width = self._output.get_size().columns - 1
        while len(hints) > 1 and get_cwidth(" · ".join(hints)) > width:
            hints.pop()
        return self._clip(" · ".join(hints))

    @staticmethod
    def _can_steer(text):
        # Like native command dispatch, multiline blocks are always literal.
        return ("\n" in text or
                not (text.lstrip().startswith("/") or text.strip().lower() in ("exit", "quit")))

    def _enqueue_message(self, text, kind, *, between_turns=False):
        item = _PendingMessage(text, kind)
        with self._condition:
            at = len(self._messages)
            if between_turns and item.selects_model:
                # Apply the selection before still-undelivered chat, but
                # never reorder slash commands, exit requests or interrupts.
                while at and isinstance(self._messages[at - 1], _PendingMessage):
                    if not self._can_steer(self._messages[at - 1].text):
                        break
                    at -= 1
            self._messages.insert(at, item)
            self._condition.notify_all()

    def _send(self, followup=False):
        if self._update_requested:
            return
        text = self._editor.text
        if not text.strip():
            self._editor.buffer.reset()
            return
        if self._busy and not followup and self._start_live(text):
            return
        if self._busy and not self._queue_enabled:
            self._notice = "Queue off — draft kept until this turn finishes · Ctrl+Q pending"
            return
        self._notice = ""
        self._editor.buffer.reset()
        kind = ("steering" if self._busy and not followup and self._can_steer(text)
                else "follow-up")
        self._enqueue_message(text, kind, between_turns=self._busy)

    def _start_live(self, text) -> bool:
        """Run a panel or read-only command now instead of after the turn.
        Only single-line slash commands qualify (multiline is always chat)."""
        command = text.strip()
        if self._live_command is None or "\n" in command or not command.startswith("/"):
            return False
        runner = self._live_command(command)
        if runner is None:
            return False
        self._notice = ""
        self._editor.buffer.reset()
        self._accept_messages([command])
        picker = command.split(maxsplit=1)[0].lower() in (
            "/model", "/models", "/ls", "/provider", "/convos", "/switch",
            "/help", "/h", "/?")
        if picker:
            # Reserve the turn boundary before starting the panel thread.
            # The request may finish while the operator is still choosing.
            with self._condition:
                self._open_pickers += 1

        def run():
            try:
                runner()
            except (EOFError, KeyboardInterrupt):
                pass  # the UI closed or the panel was cancelled
            except Exception as exc:
                print(f"  \033[31m✗ {command.split()[0]} failed: {exc}\033[0m")
            finally:
                if picker:
                    with self._condition:
                        self._open_pickers -= 1
                        self._condition.notify_all()

        threading.Thread(target=run, name="conch-panel", daemon=True).start()
        return True

    def queue_command(self, command: str):
        """Queue *command* to run between turns: right away when idle, after
        the running turn otherwise (a panel's pick that changes the session)."""
        def queue():
            self._enqueue_message(command, "follow-up", between_turns=True)
            if self._busy:
                self._notice = f"{command} runs when this turn ends · Ctrl+Q pending"
        self._change(queue)

    def run_panel(self, panel):
        """Give the terminal to a full-screen panel while a turn may still be
        running. *panel* is an async callable that runs a prompt_toolkit
        Application on this UI's loop (its input and output are the session
        defaults). The composer is suspended, turn output is held and
        published when the panel closes, and an approval prompt arriving
        meanwhile is shown then. Call from any thread but the UI's."""
        async def suspended():
            async with in_terminal():
                self._holding_output = True
                try:
                    return await panel()
                finally:
                    self._holding_output = False
                    self._flush_input()

        async def run():
            try:
                return await suspended()
            finally:
                self._schedule_publish()

        return self._call(run).result()

    def read_message(self) -> str:
        with self._condition:
            while not self._stopped.is_set():
                if self._messages and not self._open_pickers:
                    first = self._messages[0]
                    if not isinstance(first, _PendingMessage) or not first.reserved:
                        break
                self._condition.wait()
            self._raise_failure()
            if self._stopped.is_set():
                raise EOFError()
            message = self._messages.popleft()
        if isinstance(message, BaseException):
            raise message
        self._change(self._accept_messages, [message.text])
        return message.text

    def _accept_messages(self, messages):
        for text in messages:
            self._history.append_string(text)
            width = max(1, self._output.get_size().columns - 1)
            label = self._clip("── You ")
            # One queue entry keeps the boundary and literal body together.
            # Leave wrapping to the terminal; never clip or reflow user text.
            self._enqueue_output([
                ("", "\n\n"),
                ("class:user", label),
                ("class:rule", "─" * max(0, width - get_cwidth(label))),
                ("", "\n" + text + "\n"),
                ("class:rule", "─" * width),
                ("", "\n\n"),
            ], trusted=False)

    def _flush_deferred_echo(self):
        deferred, self._deferred_echo = self._deferred_echo, []
        self._accept_messages(deferred)

    def take_steering(self) -> list[str]:
        """Consume accepted guidance at a backend safe boundary, in FIFO order."""
        def take():
            messages = []
            with self._condition:
                if self._open_pickers:
                    return []
                for item in tuple(self._messages):
                    if isinstance(item, BaseException) or item.selects_model:
                        break
                    if item.kind != "steering":
                        continue
                    if item.reserved:
                        break
                    self._messages.remove(item)
                    messages.append(item.text)
                if messages:
                    self._condition.notify_all()
            if self._response:
                # The reply still on screen was written before this guidance
                # took effect: echo it only after that reply is committed.
                self._deferred_echo.extend(messages)
            else:
                self._accept_messages(messages)
            return messages
        return self._change(take)

    def _pending_items(self):
        return [item for item in self._messages if isinstance(item, _PendingMessage)]

    def _pending_text(self):
        with self._condition:
            items = self._pending_items()
            if not items:
                return [("class:muted", "No pending messages")]
            selected = (items.index(self._queue_selection)
                        if self._queue_selection in items else -1)
            start = max(0, selected - 6)
            fragments = []
            for item in items[start:start + 8]:
                chosen = item is self._queue_selection
                preview = _safe(item.text).replace("\n", " ↵ ")
                fragments.append(("class:selected" if chosen else "", self._clip(
                    f"{'›' if chosen else ' '} {item.kind}: {preview}") + "\n"))
            return fragments

    def _modal_hints(self):
        # Ordered by importance: leaving and confirming outlast navigation.
        if self._modal == "queue":
            hints = ["Escape closes", "Enter edits", "Delete removes", "Up/Down selects"]
        elif self._modal == "queue-edit":
            hints = ["Enter saves", "Escape restores original", f"{meta_key('Enter')} newline"]
        elif self._preview_colorscheme is not None:
            hints = ["Escape cancels", "Click/Enter saves", "Hover/Up/Down previews", "Type to filter"]
        elif self._modal == "choose":
            hints = ["Enter chooses", "Escape cancels", "Up/Down selects", "Type to filter"]
        else:
            hints = ["Enter submits", "Escape cancels"]
        return [("class:muted", self._fit_hints(hints))]

    def _move_pending(self, direction):
        with self._condition:
            items = self._pending_items()
            if not items:
                self._queue_selection = None
            elif self._queue_selection not in items:
                self._queue_selection = items[0]
            else:
                index = items.index(self._queue_selection)
                self._queue_selection = items[max(0, min(len(items) - 1, index + direction))]

    def _edit_pending(self):
        with self._condition:
            item = self._queue_selection
            if item not in self._messages or not isinstance(item, _PendingMessage):
                return
            item.reserved = True
            self._editing_message = item
            text = item.text
        self._modal = "queue-edit"
        self._modal_title = "Edit pending " + item.kind
        self._pending_editor.buffer.document = Document(text, len(text))
        self._app.layout.focus(self._pending_editor)

    def _release_pending(self, save=False):
        with self._condition:
            item = self._editing_message
            if item is not None and item in self._messages:
                if save:
                    item.text = self._pending_editor.text
                    if item.kind == "steering" and not self._can_steer(item.text):
                        item.kind = "follow-up"
                item.reserved = False
                self._condition.notify_all()
            self._editing_message = None
        self._pending_editor.buffer.reset()

    def _show_pending(self):
        self._modal = "queue"
        self._modal_title = "Pending messages"
        self._app.layout.focus(self._pending_list)

    def _save_pending(self):
        if not self._pending_editor.text.strip():
            return
        self._release_pending(save=True)
        self._show_pending()

    def _delete_pending(self):
        with self._condition:
            items = self._pending_items()
            item = self._queue_selection
            if item not in items:
                return
            index = items.index(item)
            self._messages.remove(item)
            items.pop(index)
            self._queue_selection = items[min(index, len(items) - 1)] if items else None
            self._condition.notify_all()

    def restore_pending(self) -> int:
        """Move every undelivered message back into the composer, ahead of
        the current draft. Returns how many were moved."""
        def restore():
            with self._condition:
                items = self._pending_items()
                for item in items:
                    self._messages.remove(item)
                self._editing_message = None
                self._queue_selection = None
            if items:
                draft = self._editor.text
                text = "\n".join([item.text for item in items] + ([draft] if draft else []))
                self._editor.buffer.document = Document(text, len(text))
                self._notice = (f"Stopped · {len(items)} pending "
                                f"message{'' if len(items) == 1 else 's'} returned to the draft")
            return len(items)
        return self._change(restore)

    def _reset_selection(self):
        self._select_choice(0)

    def _filtered_choices(self):
        words = self._query.text.casefold().split()
        return [choice for choice in self._modal_choices
                if all(word in (str(choice[1]) + " " + str(choice[2])).casefold() for word in words)]

    def _select_choice(self, index):
        choices = self._filtered_choices()
        self._selection = min(max(0, index), max(0, len(choices) - 1))
        if self._preview_colorscheme is not None:
            name = choices[self._selection][0] if choices else self._colorscheme
            self._preview_colorscheme = name
            self._app.style = style_for(name)

    def _choice_mouse(self, value, event):
        if self._modal != "choose" or self._preview_colorscheme is None:
            return NotImplemented
        if event.event_type != MouseEventType.MOUSE_MOVE and not (
                event.button == MouseButton.LEFT
                and event.event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP)):
            return NotImplemented
        for index, choice in enumerate(self._filtered_choices()):
            if choice[0] == value:
                self._select_choice(index)
                if event.event_type == MouseEventType.MOUSE_UP:
                    self._finish_modal(value)
                return None
        return NotImplemented

    def _choice_text(self):
        choices = self._filtered_choices()
        if not choices:
            return [("class:muted", "No matches")]
        self._selection = min(self._selection, len(choices) - 1)
        start = max(0, self._selection - 6)
        visible = range(start, min(len(choices), start + 8))
        # Descriptions share one column; a single long label is capped so it
        # cannot push every description off the row.
        column = min(28, max(get_cwidth(str(choices[index][1])) for index in visible))
        fragments = []
        for index in visible:
            value, label, description = choices[index]
            style = "class:selected" if index == self._selection else ""
            gap = " " * max(0, column - get_cwidth(str(label)))
            text = self._clip(
                f"{'›' if index == self._selection else ' '} {label}{gap}  {description}") + "\n"
            fragments.append((style, text, partial(self._choice_mouse, value))
                             if self._preview_colorscheme is not None else (style, text))
        return fragments

    def _open_modal(self, kind, title, choices=(), preview_colors=False):
        if self._modal is not None:
            # Backend approval supersedes local editing, restoring its reserved
            # message without ever submitting the editor or composer as an answer.
            self._finish_modal(exception=KeyboardInterrupt() if self._modal == "ask" else None)
        self._modal, self._modal_title = kind, title
        self._modal_choices = list(choices)
        self._modal_result = concurrent.futures.Future()
        self._query.buffer.reset()
        self._answer.buffer.reset()
        self._preview_colorscheme = self._colorscheme if preview_colors else None
        self._select_choice(next((index for index, choice in enumerate(self._modal_choices)
                                  if preview_colors and choice[0] == self._colorscheme), 0))
        if kind == "queue":
            with self._condition:
                items = self._pending_items()
                self._queue_selection = items[0] if items else None
            self._app.layout.focus(self._pending_list)
        else:
            self._app.layout.focus(self._answer if kind == "ask" else self._query)
        self._app.invalidate()
        return self._modal_result

    def _finish_modal(self, value=None, exception=None):
        result = self._modal_result
        self._release_pending()
        self._queue_selection = None
        self._modal = None
        self._modal_result = None
        if self._preview_colorscheme is not None:
            self._preview_colorscheme = None
            self._app.style = style_for(self._colorscheme)
        self._answer.buffer.reset()
        self._query.buffer.reset()
        self._app.layout.focus(self._editor)
        if result is not None and not result.done():
            if exception is not None:
                result.set_exception(exception)
            else:
                result.set_result(value)
        self._app.invalidate()

    def _cancel_modal(self):
        if self._modal == "queue-edit":
            self._release_pending()
            self._show_pending()
        else:
            self._finish_modal(exception=KeyboardInterrupt() if self._modal == "ask" else None)

    def _modal_wait(self, kind, title, choices=(), preview_colors=False):
        future = self._change(self._open_modal, kind, title, choices, preview_colors)
        try:
            return future.result()
        finally:
            if self._modal_result is future and not self._stopped.is_set():
                self._change(self._cancel_modal)

    def ask(self, prompt: str) -> str:
        prompt = "".join(
            text for style, text, *rest in to_formatted_text(ANSI(prompt))
            if style != "[ZeroWidthEscape]"
        )
        return self._modal_wait("ask", "Input required\n" + _safe(prompt))

    def choose(self, title: str, choices: list, *, preview_colors=False) -> Optional[str]:
        return self._modal_wait("choose", title, choices, preview_colors)

    def prefill(self, text: str):
        def restore():
            self._cancel_queued_update()
            self._editor.buffer.document = Document(text, len(text))
        self._change(restore)

    def set_context(self, **fields):
        self._change(self._context_fields.update, fields)

    def set_colorscheme(self, name):
        if name == self._colorscheme:
            return

        def update():
            self._colorscheme = name
            if self._preview_colorscheme is None:
                self._app.style = style_for(name)
        self._change(update)

    def set_busy(self, busy: bool):
        def update():
            if busy and not self._busy:
                self._busy_started = time.monotonic()
                self._detail_text = ""
                self._detail_kind = None
                self._detail_reasoning = False
            elif self._busy and not busy and not self._detail_reasoning:
                # Lifecycle progress says nothing once the turn is over;
                # published reasoning stays readable until the next turn.
                self._detail_text = ""
                self._detail_kind = None
            if busy != self._busy:
                self._notice = ""
            self._busy = busy
            self._flush_deferred_echo()
        self._change(update)

    def feed_detail(self, kind: str, text: str):
        """Retain bounded, provider-supplied reasoning/progress for this turn."""
        if kind not in ("reasoning", "progress") or not text:
            return

        def append():
            separate = kind == "progress" or kind != self._detail_kind
            prefix = ("\n" if self._detail_text else "") + kind.title() + ": " if separate else ""
            self._detail_text = (self._detail_text + prefix + _safe(text))[-8000:]
            self._detail_kind = kind
            self._detail_reasoning = self._detail_reasoning or kind == "reasoning"
        self._change(append)

    def set_queue_enabled(self, enabled: bool):
        self._change(setattr, self, "_queue_enabled", enabled)

    def start_activity(self, label: str):
        token = object()
        self._change(self._activities.__setitem__, token, (label, time.monotonic()))
        return token

    def end_activity(self, token):
        self._change(self._activities.pop, token, None)

    def clear_activities(self):
        def clear():
            self._activities.clear()
            self._tools.clear()
        self._change(clear)

    def update_response(self, full_text: str):
        self._change(setattr, self, "_response", full_text)

    def finish_response(self, ansi_text: str):
        def finish():
            self._response = ""
            if ansi_text:
                self._enqueue_output("\n" + ansi_text.rstrip("\n") + "\n\n", trusted=True)
            self._flush_deferred_echo()
        self._change(finish)

    def tool_started(self, name: str, summary: str = ""):
        token = object()
        self._change(self._tools.__setitem__, token, (name, summary, time.monotonic()))
        return token

    def tool_finished(self, token, result: str = "", error: bool = False, verbose: bool = False):
        def finish():
            tool = self._tools.pop(token, None)
            if tool is None:
                return
            name, summary, started = tool
            declined = result.startswith(("User declined", "User did not approve"))
            if declined:
                # Not a failure, and the elapsed time was spent at the prompt.
                label = f"{name} · declined"
            else:
                label = f"{name} · {'error' if error else 'done'} · {time.monotonic() - started:.1f}s"
            detail = result if verbose else " ".join(result.split())[:180]
            if declined and not verbose:
                detail = " ".join(result.partition("Feedback: ")[2].split())[:180]
            if detail:
                label += "\n" + detail if verbose else ": " + detail
            self._enqueue_output(label + "\n", trusted=False)
        self._change(finish)

    def _enqueue_output(self, text, trusted=False):
        def schedule():
            self._output_queue.append((text, trusted))
            self._schedule_publish()
        if threading.get_ident() == self._thread.ident:
            schedule()
        else:
            self._loop.call_soon_threadsafe(schedule, context=self._ui_context.copy())

    def _schedule_publish(self):
        """Start publishing queued output unless a write is in flight or a
        panel owns the terminal (UI thread only)."""
        if self._write_task is not None or self._holding_output or not self._output_queue:
            return
        task = self._loop.create_task(self._publish_output())
        self._write_task = task

        def done(completed):
            if not completed.cancelled() and completed.exception() is not None:
                self._loop_error(self._loop, {"exception": completed.exception()})
        task.add_done_callback(done)

    async def _publish_output(self):
        try:
            # Finish earlier cursor reports before resetting their origin.
            # Keep input attached/raw while waiting, and batch everything that
            # arrives meanwhile into one erase/write/redraw transaction.
            if self._output.responds_to_cpr:
                await self._app.renderer.wait_for_cpr_responses()
            if self._holding_output:
                return  # a panel took the terminal meanwhile; it republishes
            self._app.renderer.erase()
            try:
                fragments = []
                while self._output_queue:
                    text, trusted = self._output_queue.popleft()
                    parsed = to_formatted_text(ANSI(text) if trusted else text)
                    # Parse styles only: never replay arbitrary terminal controls.
                    fragments.extend((style, _safe(value))
                                     for style, value, *rest in parsed
                                     if style != "[ZeroWidthEscape]")
                # The shortcuts helper defers into run_in_terminal, which would
                # release raw input and escape our drain barrier. Write directly.
                print_formatted_text(
                    self._output, fragments, self._app.style,
                    color_depth=ColorDepth.DEPTH_1_BIT if self._no_color else None,
                )
            finally:
                # There is deliberately no await between erasing and redrawing:
                # refresh/resize/input callbacks cannot paint into the transcript.
                # in_terminal is reserved for a real handoff, where cooked input
                # and detaching the editor are required, not for printing logs.
                self._app._request_absolute_cursor_position()
                self._app._redraw()
        finally:
            self._write_task = None

    async def _drain_writes(self):
        while self._write_task is not None:
            await self._write_task
        self._raise_failure()

    def _discard(self):
        discarded = bool(self._editor.text or self._pending_editor.text)
        self._editor.buffer.reset()
        self._app.key_processor.reset()
        with self._condition:
            discarded = discarded or bool(self._messages)
            self._messages.clear()
            self._update_requested = False
            self._editing_message = None
            self._condition.notify_all()
        if self._modal is not None:
            self._finish_modal(exception=KeyboardInterrupt() if self._modal == "ask" else None)
        return discarded

    def _flush_input(self):
        # Only called inside in_terminal, after detach/cooked-mode handshake.
        import termios
        termios.tcflush(self._input.fileno(), termios.TCIFLUSH)
        self._input.flush_keys()
        self._app.key_processor.reset()

    @contextmanager
    def handoff(self, discard: bool = True):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Only the foreground backend may hand off the terminal")
        if self._handoff_depth:
            self._handoff_depth += 1
            try:
                if discard and self._change(self._discard):
                    self._old_stdout.write("Discarded queued input and draft before terminal handoff.\n")
                    self._old_stdout.flush()
                yield
            finally:
                self._handoff_depth -= 1
            return
        entered = concurrent.futures.Future()
        release = concurrent.futures.Future()

        async def suspend():
            try:
                await self._drain_writes()
                async with in_terminal():
                    self._flush_input()
                    if discard and self._discard():
                        self._old_stdout.write("Discarded queued input and draft before terminal handoff.\n")
                        self._old_stdout.flush()
                    entered.set_result(None)
                    try:
                        await asyncio.wrap_future(release)
                    finally:
                        self._flush_input()
            except BaseException as exc:
                if not entered.done():
                    entered.set_exception(exc)
                raise

        suspension = None
        try:
            with self._routing:
                self._stdout.flush()
                self._stderr.flush()
                self._raise_failure()
                suspension = self._call(suspend)
                def propagate_failure(future):
                    if not entered.done():
                        error = future.exception()
                        entered.set_exception(error or RuntimeError("Terminal suspension ended early"))
                suspension.add_done_callback(propagate_failure)
                # No child may run until in_terminal has detached stdin and
                # stopped rendering. There is deliberately no unsafe timeout.
                entered.result()
                self._suspended = True
                self._direct = True
                self._handoff_depth = 1
            yield
        finally:
            if suspension is not None:
                with self._routing:
                    try:
                        self._old_stdout.flush()
                        self._old_stderr.flush()
                    finally:
                        try:
                            if not release.done():
                                release.set_result(None)
                            suspension.result()
                        finally:
                            self._handoff_depth = 0
                            self._suspended = False
                            self._direct = self._stopped.is_set()

    def close(self):
        if self._thread is None:
            return
        self._ready.wait()
        try:
            if not self._stopped.is_set():
                try:
                    with self._routing:
                        self._stdout.flush()
                        self._stderr.flush()
                finally:
                    if not self._stopped.is_set():
                        self._call(self._app.exit).result()
        finally:
            self._thread.join()
            self._restore_streams()
        self._raise_failure()
