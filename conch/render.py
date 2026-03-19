"""Terminal rendering helpers -- syntax highlighting and streaming."""

from __future__ import annotations

import itertools
import re
import sys
import threading
import time

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


class StreamPrinter:
    """Incrementally render streamed LLM output with formatting.

    Buffers code blocks until complete so they can be syntax-highlighted.
    Applies inline markdown formatting to regular lines as they arrive.
    """

    def __init__(self):
        self._in_code = False
        self._code_lang = ""
        self._code_buf: list[str] = []
        self._line_buf = ""
        self._full_text = ""

    def feed(self, chunk: str):
        self._full_text += chunk
        self._line_buf += chunk
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            self._emit_line(line)
            sys.stdout.write("\n")
        sys.stdout.flush()

    def flush(self) -> str:
        """Flush remaining buffer and return accumulated full text."""
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
        self._in_code = False
        self._code_lang = ""
        self._code_buf.clear()
        self._line_buf = ""
        self._full_text = ""

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


class Spinner:
    """Minimal terminal spinner used while waiting on remote work."""

    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if not sys.stderr.isatty():
            return self

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
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=0.2)
            sys.stderr.write("\r               \r")
            sys.stderr.flush()
        return False
