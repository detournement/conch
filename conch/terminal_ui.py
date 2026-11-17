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
from typing import Callable, Optional

from prompt_toolkit import Application
from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.run_in_terminal import in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer, Float, FloatContainer, HSplit, Layout, Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import ColorDepth, create_output
from prompt_toolkit.renderer import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

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


class _Commands(Completer):
    def __init__(self, commands):
        self.commands = commands

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text:
            return
        for spec, description in self.commands():
            command = spec.split()[0]
            if text.lower() in spec.lower() or text[1:].lower() in description.lower():
                yield Completion(command, start_position=-len(text),
                                 display=_safe(spec), display_meta=_safe(description))


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
        self._modal = None
        self._modal_result = None
        self._modal_choices = []
        self._selection = 0
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
            multiline=True, history=self._history, completer=_Commands(self._commands),
            complete_while_typing=False, prompt=[("class:prompt", "> ")],
            height=Dimension(min=1, max=7), dont_extend_height=True)
        self._query = TextArea(multiline=False, prompt="Search: ")
        self._answer = TextArea(multiline=False, prompt="Answer: ")
        self._pending_editor = TextArea(
            multiline=True, prompt="Edit: ", height=Dimension(min=1, max=7),
            dont_extend_height=True)
        self._pending_list = self._text_window(self._pending_text, 8, focusable=True)
        self._query.buffer.on_text_changed += lambda _: self._reset_selection()
        normal = HSplit([
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
            self._editor,
            self._text_window(self._hints, 1),
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
        root = FloatContainer(HSplit([
            ConditionalContainer(normal, Condition(lambda: self._modal is None)),
            ConditionalContainer(modal, Condition(lambda: self._modal is not None)),
        ]), floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8))])
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
            elif self._editor.buffer.complete_state is not None:
                self._editor.buffer.complete_state = None
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

        @keys.add("c-p", eager=True)
        def palette(event):
            if self._modal is None:
                choices = [(spec.split()[0] + (" " if " " in spec else ""), spec, desc)
                           for spec, desc in self._commands()]
                self._open_modal("choose", "Commands — select to fill the draft", choices,
                                 palette=True)

        @keys.add("up", filter=Condition(lambda: self._modal in ("choose", "queue")), eager=True)
        def up(event):
            if self._modal == "queue":
                self._move_pending(-1)
            else:
                self._selection = max(0, self._selection - 1)

        @keys.add("down", filter=Condition(lambda: self._modal in ("choose", "queue")), eager=True)
        def down(event):
            if self._modal == "queue":
                self._move_pending(1)
            else:
                self._selection = min(max(0, len(self._filtered_choices()) - 1), self._selection + 1)

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
            full_screen=False, mouse_support=False, erase_when_done=True,
            input=self._input, output=self._output, refresh_interval=0.25,
            color_depth=ColorDepth.DEPTH_1_BIT if self._no_color else None,
            style=Style.from_dict({"prompt": "bold", "title": "bold",
                                   "muted": "#888888", "status": "#88aaaa",
                                   "selected": "reverse", "error": "#ff6666"}))

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
            "prompt_all": "ask first", "safe_auto": "safe auto", "yolo": "AUTO-EXECUTE",
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
        model = "/".join(str(fields[key]) for key in ("provider", "model") if fields.get(key))
        if model:
            parts.append(model)
        if fields.get("conversation"):
            parts.append(str(fields["conversation"]))
        if fields.get("tools") is not None:
            parts.append(f"tools: {fields['tools']}")
        return [("class:status", self._clip(" · ".join(parts)))]

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

    def _hints(self):
        # Most important first: a narrow terminal drops hints from the end
        # instead of cutting one mid-word.
        with self._condition:
            pending = any(isinstance(item, _PendingMessage) for item in self._messages)
        if self._notice:
            hints = [self._notice]
        elif self._busy and self._queue_enabled:
            hints = ["Enter steers", "Ctrl+C stops", "Alt+Q follow-up"]
        elif self._busy:
            hints = ["Queue off · draft kept", "Ctrl+C stops"]
        else:
            hints = ["Enter sends", "Ctrl+P commands", "Ctrl+D exits"]
        if pending:
            hints.append("Ctrl+Q pending")
        if self._detail_text:
            hints.append("Alt+R details")
        if not self._notice:
            hints.append("Alt+Enter newline")
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

    def _send(self, followup=False):
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
        with self._condition:
            kind = ("steering" if self._busy and not followup and self._can_steer(text)
                    else "follow-up")
            self._messages.append(_PendingMessage(text, kind))
            self._condition.notify_all()

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

        def run():
            try:
                runner()
            except (EOFError, KeyboardInterrupt):
                pass  # the UI closed or the panel was cancelled
            except Exception as exc:
                print(f"  \033[31m✗ {command.split()[0]} failed: {exc}\033[0m")

        threading.Thread(target=run, name="conch-panel", daemon=True).start()
        return True

    def queue_command(self, command: str):
        """Queue *command* to run between turns: right away when idle, after
        the running turn otherwise (a panel's pick that changes the session)."""
        def queue():
            with self._condition:
                self._messages.append(_PendingMessage(command, "follow-up"))
                self._condition.notify_all()
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
                if self._messages:
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
            self._enqueue_output("> " + text + "\n", trusted=False)

    def _flush_deferred_echo(self):
        deferred, self._deferred_echo = self._deferred_echo, []
        self._accept_messages(deferred)

    def take_steering(self) -> list[str]:
        """Consume accepted guidance at a backend safe boundary, in FIFO order."""
        def take():
            messages = []
            with self._condition:
                for item in tuple(self._messages):
                    if isinstance(item, BaseException):
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
            hints = ["Enter saves", "Escape restores original", "Alt+Enter newline"]
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
        self._selection = 0

    def _filtered_choices(self):
        words = self._query.text.casefold().split()
        return [choice for choice in self._modal_choices
                if all(word in (str(choice[1]) + " " + str(choice[2])).casefold() for word in words)]

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
            _, label, description = choices[index]
            style = "class:selected" if index == self._selection else ""
            gap = " " * max(0, column - get_cwidth(str(label)))
            fragments.append((style, self._clip(
                f"{'›' if index == self._selection else ' '} {label}{gap}  {description}") + "\n"))
        return fragments

    def _open_modal(self, kind, title, choices=(), palette=False):
        if self._modal is not None:
            # Backend approval supersedes local editing, restoring its reserved
            # message without ever submitting the editor or composer as an answer.
            self._finish_modal(exception=KeyboardInterrupt() if self._modal == "ask" else None)
        self._modal, self._modal_title = kind, title
        self._modal_choices = list(choices)
        self._palette = palette
        self._modal_result = concurrent.futures.Future()
        self._query.buffer.reset()
        self._answer.buffer.reset()
        self._selection = 0
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
        palette = self._palette
        self._release_pending()
        self._queue_selection = None
        self._modal = None
        self._modal_result = None
        self._answer.buffer.reset()
        self._query.buffer.reset()
        self._app.layout.focus(self._editor)
        if palette and value is not None:
            self._editor.buffer.document = Document(value, len(value))
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

    def _modal_wait(self, kind, title, choices=()):
        future = self._change(self._open_modal, kind, title, choices)
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

    def choose(self, title: str, choices: list) -> Optional[str]:
        return self._modal_wait("choose", title, choices)

    def prefill(self, text: str):
        self._change(setattr, self._editor.buffer, "document", Document(text, len(text)))

    def set_context(self, **fields):
        self._change(self._context_fields.update, fields)

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
                    parsed = to_formatted_text(ANSI(text)) if trusted else [("", text)]
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
