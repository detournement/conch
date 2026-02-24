"""Terminal rendering: ANSI syntax highlighting for markdown-ish LLM output, and spinner."""
import itertools
import re
import sys
import threading
import time

# ANSI escape codes
_RST = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_ITALIC = "\033[3m"
_UL = "\033[4m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_MAGENTA = "\033[35m"
_CYAN = "\033[36m"
_WHITE = "\033[37m"
_BG_GRAY = "\033[48;5;236m"
_FG_GREEN = "\033[38;5;114m"
_FG_ORANGE = "\033[38;5;214m"
_FG_GRAY = "\033[38;5;245m"


def highlight(text: str) -> str:
    """Apply terminal colors to markdown-like text from an LLM."""
    lines = text.split("\n")
    out = []
    in_code_block = False
    code_lang = ""

    for line in lines:
        # Code block fences
        if re.match(r"^```", line):
            if not in_code_block:
                in_code_block = True
                code_lang = line[3:].strip()
                label = f" {code_lang} " if code_lang else ""
                out.append(f"{_DIM}{'─' * 40}{label}{'─' * max(0, 10 - len(label))}{_RST}")
            else:
                in_code_block = False
                out.append(f"{_DIM}{'─' * 50}{_RST}")
            continue
        if in_code_block:
            out.append(f"  {_FG_GREEN}{line}{_RST}")
            continue

        # Headers
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            out.append(f"{_BOLD}{_CYAN}{m.group(2)}{_RST}")
            continue

        # Horizontal rules
        if re.match(r"^-{3,}$|^\*{3,}$|^_{3,}$", line.strip()):
            out.append(f"{_DIM}{'─' * 50}{_RST}")
            continue

        # Bullet lists
        m = re.match(r"^(\s*)([-*])\s+(.*)", line)
        if m:
            indent, _, content = m.groups()
            content = _inline_highlight(content)
            out.append(f"{indent}  {_CYAN}•{_RST} {content}")
            continue

        # Numbered lists
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if m:
            indent, num, content = m.groups()
            content = _inline_highlight(content)
            out.append(f"{indent}  {_CYAN}{num}.{_RST} {content}")
            continue

        # Normal line
        out.append(_inline_highlight(line))

    return "\n".join(out)


def _inline_highlight(text: str) -> str:
    """Highlight inline markdown: **bold**, `code`, *italic*."""
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", rf"{_BOLD}\1{_RST}", text)
    text = re.sub(r"__(.+?)__", rf"{_BOLD}\1{_RST}", text)
    # Inline code: `text`
    text = re.sub(r"`([^`]+)`", rf"{_FG_GREEN}\1{_RST}", text)
    # Italic: *text* or _text_ (careful not to match mid-word underscores)
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", rf"{_ITALIC}\1{_RST}", text)
    text = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", rf"{_ITALIC}\1{_RST}", text)
    return text


class Spinner:
    """Context manager that shows an animated spinner on stderr while work runs."""

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "Thinking"):
        self._message = message
        self._stop = threading.Event()
        self._thread = None

    def _spin(self) -> None:
        frames = itertools.cycle(self._FRAMES)
        while not self._stop.is_set():
            frame = next(frames)
            sys.stderr.write(f"\r{_CYAN}{frame}{_RST} {_DIM}{self._message}{_RST}  ")
            sys.stderr.flush()
            self._stop.wait(0.08)
        sys.stderr.write(f"\r{' ' * (len(self._message) + 6)}\r")
        sys.stderr.flush()

    def __enter__(self) -> "Spinner":
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
