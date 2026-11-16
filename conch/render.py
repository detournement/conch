"""Terminal rendering helpers -- syntax highlighting and streaming."""

from __future__ import annotations

import itertools
import re
import shutil
import sys
import threading
import time
from typing import Optional

try:
    from pygments import highlight as _pyg_highlight
    from pygments.lexers import get_lexer_by_name, TextLexer
    from pygments.formatters import Terminal256Formatter

    _HAS_PYGMENTS = True
except ImportError:
    _HAS_PYGMENTS = False

_CODE_BORDER = "\033[2m" + "\u2500" * 44 + "\033[0m"


def _highlight_code(code: str, lang: str) -> str:
    if _HAS_PYGMENTS:
        try:
            lexer = get_lexer_by_name(lang) if lang else TextLexer()
        except Exception:
            lexer = TextLexer()
        return _pyg_highlight(
            code, lexer, Terminal256Formatter(style="monokai")
        ).rstrip("\n")
    return "\033[2m" + code + "\033[0m"


def _format_line(line: str) -> str:
    """Apply inline markdown formatting to a single line."""
    if line.startswith("### "):
        return "\033[1;35m" + line[4:] + "\033[0m"
    if line.startswith("## "):
        return "\033[1;34m" + line[3:] + "\033[0m"
    if line.startswith("# "):
        return "\033[1;33m" + line[2:] + "\033[0m"
    if re.match(r"^[-*_]{3,}\s*$", line):
        return _CODE_BORDER

    out = line
    out = re.sub(r"\*\*(.+?)\*\*", "\033[1m" + r"\1" + "\033[22m", out)
    out = re.sub(r"`([^`]+)`", "\033[36m" + r"\1" + "\033[0m", out)
    out = re.sub(
        r"(?<!\*)\*([^*]+)\*(?!\*)", "\033[3m" + r"\1" + "\033[23m", out
    )

    if re.match(r"^\s*[-*]\s", out):
        out = re.sub(r"^(\s*)[-*]\s", r"\1" + "\033[36m\u2022\033[0m ", out)
    elif re.match(r"^\s*\d+\.\s", out):
        out = re.sub(r"^(\s*)(\d+\.)", r"\1" + "\033[36m" + r"\2" + "\033[0m", out)
    return out


def highlight(text: str) -> str:
    """Render LLM output with ANSI colors and syntax-highlighted code blocks."""
    if not text or not sys.stdout.isatty():
        return text

    lines = text.split("\n")
    output: list[str] = []
    in_code = False
    code_lang = ""
    code_buf: list[str] = []

    for line in lines:
        if not in_code and line.startswith("```"):
            in_code = True
            code_lang = line[3:].strip().split()[0] if line[3:].strip() else ""
            code_buf = []
            continue
        if in_code:
            if line.startswith("```"):
                in_code = False
                code = "\n".join(code_buf)
                output.append("  " + _CODE_BORDER)
                for hl_line in _highlight_code(code, code_lang).split("\n"):
                    output.append("  " + hl_line)
                output.append("  " + _CODE_BORDER)
                continue
            code_buf.append(line)
            continue
        output.append(_format_line(line))

    if in_code:
        output.append("  " + _CODE_BORDER)
        for raw_line in code_buf:
            output.append("  " + raw_line)
        output.append("  " + _CODE_BORDER)

    return "\n".join(output)


def _get_active_ui():
    """Consult the interactive surface without importing it in plain sessions."""
    module = sys.modules.get(f"{__package__}.terminal_ui")
    return module.get_active_ui() if module is not None else None


class StreamPrinter:
    """Incrementally render streamed LLM output with formatting.

    The interactive surface owns mutable text and final transcript commits.
    Plain terminals retain incremental line formatting and code buffering.
    """

    def __init__(self, show_waiting: bool = True):
        self._in_code = False
        self._code_lang = ""
        self._code_buf: list[str] = []
        self._line_buf = ""
        self._full_text = ""
        self._partial_written = 0
        self._started = False
        self._spinner = None
        self._ui = _get_active_ui()
        self._ui_committed = 0
        if show_waiting and (self._ui is not None or sys.stderr.isatty()):
            self._spinner = Spinner("Waiting for response")
            self._spinner.__enter__()

    def end_waiting(self) -> None:
        """Stop the stderr spinner when no tokens will arrive (API errors, empty stream)."""
        if self._spinner:
            self._spinner.__exit__(None, None, None)
            self._spinner = None

    def _clear_partial(self):
        """Erase the raw partial text, including any lines from terminal wrapping."""
        if not self._partial_written:
            return
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        wrapped = self._partial_written // max(cols, 1)
        for _ in range(wrapped):
            sys.stdout.write("\033[A\033[2K")
        sys.stdout.write("\r\033[2K")
        self._partial_written = 0

    def feed(self, chunk: str):
        if not self._started:
            self._started = True
            if self._spinner:
                self._spinner.__exit__(None, None, None)
                self._spinner = None
        self._full_text += chunk
        if self._ui is not None:
            self._ui.update_response(self._full_text[self._ui_committed:])
            return
        self._line_buf += chunk

        while "\n" in self._line_buf:
            self._clear_partial()
            line, self._line_buf = self._line_buf.split("\n", 1)
            self._emit_line(line)
            sys.stdout.write("\n")

        # Show partial line immediately so tokens are visible at once
        if self._line_buf and not self._in_code:
            new_text = self._line_buf[self._partial_written:]
            if new_text:
                sys.stdout.write(new_text)
                self._partial_written = len(self._line_buf)

        sys.stdout.flush()

    def flush(self) -> str:
        """Flush remaining buffer and return accumulated full text."""
        self.end_waiting()
        if self._ui is not None:
            pending = self._full_text[self._ui_committed:]
            if pending:
                self._ui.finish_response(highlight(pending))
                self._ui_committed = len(self._full_text)
            return self._full_text
        self._clear_partial()
        if self._line_buf:
            self._emit_line(self._line_buf)
            self._line_buf = ""
        if self._in_code:
            code = "\n".join(self._code_buf)
            sys.stdout.write("\n  " + _highlight_code(code, self._code_lang))
            sys.stdout.write("\n  " + _CODE_BORDER)
            self._in_code = False
        sys.stdout.write("\n")
        sys.stdout.flush()
        return self._full_text

    def reset(self):
        if self._ui is not None:
            # A tool boundary or failed request must never discard its prose.
            self.flush()
            self._ui_committed = 0
            self._started = False
        self._in_code = False
        self._code_lang = ""
        self._code_buf.clear()
        self._line_buf = ""
        self._full_text = ""
        self._partial_written = 0

    def _emit_line(self, line: str):
        if not self._in_code and line.startswith("```"):
            self._in_code = True
            self._code_lang = (
                line[3:].strip().split()[0] if line[3:].strip() else ""
            )
            self._code_buf = []
            sys.stdout.write("  " + _CODE_BORDER)
            return

        if self._in_code:
            if line.startswith("```"):
                self._in_code = False
                code = "\n".join(self._code_buf)
                for hl_line in _highlight_code(code, self._code_lang).split("\n"):
                    sys.stdout.write("\n  " + hl_line)
                sys.stdout.write("\n  " + _CODE_BORDER)
                return
            self._code_buf.append(line)
            return

        sys.stdout.write(_format_line(line))


_active_spinners: list["Spinner"] = []


def clear_active_spinners():
    """Force-clear the visual output of all active spinners."""
    for spinner in list(_active_spinners):
        spinner._clear_line()


class Spinner:
    """Minimal terminal spinner used while waiting on remote work."""

    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._registered = False
        self._ui = None
        self._activity = None

    def _clear_line(self):
        if self._ui is not None:
            if self._activity is not None:
                self._ui.end_activity(self._activity)
                self._activity = None
            return
        if _get_active_ui() is not None:
            return
        clear_width = len(self.label) + 6
        sys.stderr.write("\r" + " " * clear_width + "\r")
        sys.stderr.flush()

    def __enter__(self):
        self._ui = _get_active_ui()
        if self._ui is not None:
            self._activity = self._ui.start_activity(self.label)
            _active_spinners.append(self)
            self._registered = True
            return self
        if not sys.stderr.isatty():
            return self

        _active_spinners.append(self)
        self._registered = True

        def _run():
            for frame in itertools.cycle("\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"):
                if self._stop.is_set():
                    break
                sys.stderr.write(
                    "\r\033[36m" + frame + "\033[0m \033[2m"
                    + self.label + "\033[0m  "
                )
                sys.stderr.flush()
                time.sleep(0.08)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._ui is not None:
            self._clear_line()
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=0.2)
            self._clear_line()
        if self._registered:
            try:
                _active_spinners.remove(self)
            except ValueError:
                pass
            self._registered = False
        return False
