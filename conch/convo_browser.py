"""The conversations page: a full-screen terminal browser over saved
conversations.

``conch convos`` opens it standalone and starts the shell in the
conversation picked with Enter; ``/convos`` (or ``/switch`` without an id)
opens it inside the shell and switches to the pick. The page lists every
conversation with its age, size, and recorded cost; filters by title, id,
model, and full message text as you type; previews and reads transcripts;
renames, forks (a copy to continue in), and exports to Markdown; deletes
after a confirmation; and totals tokens and estimated API cost across all
conversations and across the current matches.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from prompt_toolkit import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.output import ColorDepth, create_output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from .conversations import (
    Conversation,
    ConversationManager,
    _extract_searchable_text,
    _extract_snippet,
    display_blocks,
    export_conversation,
    normalize_title,
    strip_turn_context,
)
from .providers import add_usage, format_session_cost
from .terminal_ui import _safe

Line = Tuple[str, str]  # (style, text) for one display row

USAGE = """\
usage: conch convos

Browse saved conversations full-screen: search their titles and full text,
read transcripts, see tokens and estimated cost per conversation and in
total, rename, fork, export, or delete conversations, and press Enter to
resume one in the shell.

  Enter       resume the selected conversation
  /           search (Enter keeps the filter, Esc clears it)
  v, Right    read the transcript (Esc goes back)
  s           sort: recent, cost, messages (relevance while searching)
  r           rename (Enter saves, Esc cancels)
  f           fork: continue in a copy; the original stays as it is
  e           export to Markdown in the current directory
  d           delete (asks first)
  q, Esc      quit
"""

_SORTS = ("recent", "cost", "messages")
_SPLIT_MIN_COLUMNS = 100
_TOOL_PREVIEW_CHARS = 2000


def can_browse() -> bool:
    """Whether this process has a real terminal to draw the page on."""
    try:
        return (sys.stdin.isatty() and sys.stdout.isatty()
                and os.environ.get("TERM") != "dumb")
    except (AttributeError, ValueError):
        return False


def browse(manager: ConversationManager, current_id: Optional[str] = None,
           *, input=None, output=None) -> Optional[str]:
    """Run the page; the id of the conversation chosen with Enter, or None.

    *current_id* marks the conversation the calling shell is in (it cannot
    be deleted from here). *input*/*output* default to the real terminal.
    """
    return ConversationsPage(manager, current_id).run(input=input, output=output)


def main(argv: Optional[List[str]] = None) -> int:
    """``conch convos``: the page standalone; Enter resumes in the shell."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    if argv:
        print(USAGE.splitlines()[0], file=sys.stderr)
        print("conch convos: takes no arguments", file=sys.stderr)
        return 2
    if not can_browse():
        print("conch convos: needs an interactive terminal", file=sys.stderr)
        return 2
    manager = ConversationManager()
    try:
        chosen = browse(manager)
    finally:
        manager.close()
    if chosen is None:
        return 0
    from .app import chat_loop

    chat_loop(resume_id=chosen)
    return 0


# --- formatting helpers -------------------------------------------------------


def _num(value: Any) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _has_usage(usage: Any) -> bool:
    return isinstance(usage, dict) and any(
        _num(usage.get(key)) for key in ("turns", "input_tokens", "output_tokens"))


def _total_usage(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    total: Dict[str, Any] = {}
    for entry in entries:
        usage = entry.get("usage")
        if isinstance(usage, dict):
            add_usage(total, usage)
    return total


def _compact(count: float) -> str:
    count = int(count)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= divisor:
            value = count / divisor
            return f"{value:.1f}{suffix}" if value < 100 else f"{value:.0f}{suffix}"
    return str(count)


def _money(cost: float) -> str:
    return f"${cost:.4f}" if cost < 100 else f"${cost:,.2f}"


def _cost_cell(usage: Any) -> str:
    """Compact cost column: API dollars, else the kind of billing."""
    if not _has_usage(usage):
        return "—"
    others = [label for kind, label in (("seat", "seat"), ("local", "local"),
                                        ("unknown", "?"))
              if _num(usage.get(f"{kind}_turns"))]
    cost = _num(usage.get("cost"))
    if cost or _num(usage.get("api_turns")):
        return _money(cost) + ("+" if others else "")
    return "+".join(others) or "?"


def _cost_label(usage: Any) -> str:
    return format_session_cost(usage) if _has_usage(usage) else "not recorded"


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _ago(value: Any, now: datetime) -> str:
    when = _parse_time(value)
    if when is None:
        return ""
    try:
        seconds = (now - when).total_seconds()
    except TypeError:  # an aware timestamp from elsewhere
        seconds = None
    if seconds is not None and seconds < 7 * 86400:
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86400)}d ago"
    return when.strftime("%b %d" if when.year == now.year else "%Y-%m-%d")


def _stamp(value: Any) -> str:
    when = _parse_time(value)
    return when.strftime("%Y-%m-%d %H:%M") if when else "unknown"


def _fit(text: str, width: int) -> str:
    """Exactly *width* cells: padded, or cut with an ellipsis."""
    if width <= 0:
        return ""
    text = _safe(text).replace("\n", " ").replace("\t", " ")
    used = get_cwidth(text)
    if used <= width:
        return text + " " * (width - used)
    kept, used = [], 0
    for char in text:
        cells = get_cwidth(char)
        if used + cells > width - 1:
            break
        kept.append(char)
        used += cells
    return "".join(kept) + "…" + " " * (width - 1 - used)


def _fit_right(text: str, width: int) -> str:
    used = get_cwidth(text)
    return " " * (width - used) + text if used <= width else _fit(text, width)


def _wrap_line(line: str, width: int) -> List[str]:
    """Rows of at most *width* cells, broken at a space when one falls in
    the back half of the row, else mid-word."""
    ascii_only = line.isascii()
    rows = []
    while (len(line) if ascii_only else get_cwidth(line)) > width:
        if ascii_only:
            cut = width
        else:
            cut, used = 0, 0
            for char in line:
                used += get_cwidth(char)
                if used > width:
                    break
                cut += 1
            cut = max(cut, 1)  # a glyph wider than the row still advances
        space = line.rfind(" ", cut // 2, cut + 1)
        if space > 0:
            rows.append(line[:space])
            line = line[space + 1:]
        else:
            rows.append(line[:cut])
            line = line[cut:]
    rows.append(line)
    return rows


def _para(text: str, width: int, style: str = "", indent: int = 0,
          max_lines: Optional[int] = None) -> List[Line]:
    """*text* wrapped to *width* cells under an *indent*; a cut ends in …."""
    inner = max(8, width - indent)
    rows: List[str] = []
    for line in _safe(text).expandtabs(4).split("\n"):
        rows.extend(_wrap_line(line, inner))
        if max_lines is not None and len(rows) > max_lines:
            break
    if max_lines is not None and len(rows) > max_lines:
        rows = rows[:max_lines - 1] + [_fit(rows[max_lines - 1] + "…", inner).rstrip()]
    pad = " " * indent
    return [(style, pad + row) for row in rows]


def _hanging(marker: str, text: str, width: int, style: str, indent: int,
             max_lines: Optional[int] = None) -> List[Line]:
    """Like _para, with *marker* leading the first row and later rows
    aligned under the text after it."""
    rows = _para(text, width, style, indent + len(marker), max_lines)
    first_style, first = rows[0]
    rows[0] = (first_style, " " * indent + marker + first[indent + len(marker):])
    return rows


def _highlight(text: str, style: str, pattern: Optional[re.Pattern]) -> List[Line]:
    if pattern is None or not text:
        return [(style, text)]
    fragments, position = [], 0
    for match in pattern.finditer(text):
        if match.start() > position:
            fragments.append((style, text[position:match.start()]))
        fragments.append((f"{style} class:match", match.group()))
        position = match.end()
    if position < len(text):
        fragments.append((style, text[position:]))
    return fragments


def _fit_hints(hints: List[str], width: int) -> str:
    """One row of hints, most important first, dropped from the end."""
    while len(hints) > 1 and get_cwidth(" · ".join(hints)) > width:
        hints = hints[:-1]
    return _fit(" · ".join(hints), width)


# --- message display ----------------------------------------------------------


_ROLE_LABELS = {"user": ("class:user", " You"), "assistant": ("class:assistant", " Assistant")}
_TOOL_LABEL = ("class:tool", " Tool")


def _message_lines(message: Dict[str, Any], width: int) -> List[Line]:
    """Display rows for one message: a role label, then its blocks."""
    blocks = display_blocks(message)
    if not blocks:
        return []
    rows: List[Line] = []
    label = _ROLE_LABELS.get(message.get("role"))
    if label and any(kind != "tool" for kind, _ in blocks):
        rows.append(label)
    for kind, text in blocks:
        if kind == "call":
            rows += _hanging("→ ", text, width, "class:tool", indent=3, max_lines=2)
        elif kind == "tool":
            body = text[:_TOOL_PREVIEW_CHARS] or "(no output)"
            rows += _hanging("⎿ ", body, width, "class:tool", indent=3, max_lines=4)
        else:
            rows += _para(text, width, "", indent=3)
    return rows


# --- the page -------------------------------------------------------------------


class _Pane(UIControl):
    """Renders exactly the rows a render function returns for the window's
    current size, so every pane scrolls itself (no hidden cursor tricks)."""

    def __init__(self, render: Callable[[int, int], List[List[Line]]],
                 focusable: bool = False):
        self._render = render
        self._focusable = focusable

    def is_focusable(self) -> bool:
        return self._focusable

    def create_content(self, width: int, height: int) -> UIContent:
        rows = self._render(width, height) or [[]]
        return UIContent(get_line=lambda index: rows[index], line_count=len(rows),
                         cursor_position=Point(0, 0), show_cursor=False)


class _Row:
    __slots__ = ("entry", "score")

    def __init__(self, entry: Dict[str, Any], score: int = 0):
        self.entry = entry
        self.score = score

    @property
    def id(self) -> str:
        return self.entry["id"]


class ConversationsPage:
    """State, rendering, and keys for one run of the page."""

    def __init__(self, manager: ConversationManager, current_id: Optional[str] = None):
        self.manager = manager
        self.current_id = current_id
        self.entries: List[Dict[str, Any]] = manager.list_all()
        self.rows: List[_Row] = []
        self.selected = 0
        self.sort = "recent"
        self.mode = "list"  # list | search | view | confirm | rename
        self.notice = ""
        self.result: Optional[str] = None
        self.app: Optional[Application] = None
        self._list_top = 0
        self._list_height = 10
        self._view_top = 0
        self._view_height = 10
        self._words: List[str] = []
        self._pattern: Optional[re.Pattern] = None
        self._loaded: "OrderedDict[str, Optional[Conversation]]" = OrderedDict()
        self._view_cache: Tuple[Any, List[Line]] = (None, [])
        self._preview_cache: Tuple[Any, List[Line]] = (None, [])
        # Exports land here (None: the process working directory).
        self.export_dir: Optional[str] = None
        self.search = TextArea(multiline=False, prompt=[("class:prompt", " Search: ")],
                               style="class:search")
        self.search.buffer.on_text_changed += lambda _: self._refilter(keep=False)
        self.rename_input = TextArea(multiline=False, prompt=[("class:prompt", " Title: ")],
                                     style="class:search")
        self._refilter(keep=False)

    # --- data ---------------------------------------------------------------

    def _quiet(self, function, *args):
        """Run a store call without letting a warning paint over the page;
        the warning becomes the footer notice instead."""
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            value = function(*args)
        warning = captured.getvalue().strip()
        if warning:
            self.notice = warning.splitlines()[-1]
        return value

    def _conversation(self, conv_id: str) -> Optional[Conversation]:
        if conv_id in self._loaded:
            self._loaded.move_to_end(conv_id)
            return self._loaded[conv_id]
        conversation = self._quiet(self.manager.load, conv_id)
        self._loaded[conv_id] = conversation
        while len(self._loaded) > 8:
            self._loaded.popitem(last=False)
        return conversation

    def current_row(self) -> Optional[_Row]:
        return self.rows[self.selected] if 0 <= self.selected < len(self.rows) else None

    def _refilter(self, keep: bool = True):
        words = [word for word in self.search.text.casefold().split() if word]
        if words and not self._words and self.sort == "recent":
            self.sort = "relevance"
        elif not words and self.sort == "relevance":
            self.sort = "recent"
        self._words = words
        self._pattern = re.compile(
            "|".join(re.escape(word) for word in sorted(set(words), key=len, reverse=True)),
            re.IGNORECASE) if words else None
        kept = self.current_row().id if keep and self.current_row() else None
        if words:
            hits = self._quiet(self.manager.content_hits, words)
            rows = []
            for entry in self.entries:
                meta = " ".join(str(entry.get(key) or "") for key in
                                ("id", "title", "model", "provider")).casefold()
                score = 0
                for word, found in zip(words, hits):
                    if entry["id"] in found:
                        score += found[entry["id"]]
                    elif word in meta:
                        score += 1
                    else:
                        break
                else:
                    rows.append(_Row(entry, score))
        else:
            rows = [_Row(entry) for entry in self.entries]
        rows.sort(key=self._sort_key, reverse=True)
        self.rows = rows
        self.selected = next((index for index, row in enumerate(rows) if row.id == kept), 0)

    def _sort_key(self, row: _Row):
        entry = row.entry
        updated = str(entry.get("updated_at") or "")
        if self.sort == "relevance":
            return (row.score, updated)
        if self.sort == "cost":
            usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
            tokens = _num(usage.get("input_tokens")) + _num(usage.get("output_tokens"))
            return (_num(usage.get("cost")), tokens, updated)
        if self.sort == "messages":
            return (_num(entry.get("message_count")), updated)
        return (updated,)

    # --- actions --------------------------------------------------------------

    def move(self, delta: int):
        if self.rows:
            self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))

    def move_to(self, index: int):
        if self.rows:
            self.selected = max(0, min(len(self.rows) - 1, index))

    def cycle_sort(self):
        order = (("relevance",) if self._words else ()) + _SORTS
        position = order.index(self.sort) if self.sort in order else -1
        self.sort = order[(position + 1) % len(order)]
        self._refilter()

    def start_search(self):
        self.mode = "search"
        self.app.layout.focus(self.search)

    def end_search(self, clear: bool):
        if clear and self.search.text:
            # Typing jumps to the best match; clearing keeps the pick.
            row = self.current_row()
            self.search.text = ""
            self.selected = next((index for index, other in enumerate(self.rows)
                                  if row is not None and other.id == row.id), 0)
        self.mode = "list"
        self.app.layout.focus(self._list_window)

    def open_view(self):
        if self.current_row() is None:
            return
        self.mode = "view"
        self._view_top = 0
        self.app.layout.focus(self._view_window)

    def close_view(self):
        self.mode = "list"
        self.app.layout.focus(self._list_window)

    def scroll(self, delta: int):
        self._view_top += delta

    def resume(self):
        row = self.current_row()
        if row is not None:
            self.result = row.id
            self.app.exit()

    def ask_delete(self):
        row = self.current_row()
        if row is None:
            return
        if row.id == self.current_id:
            self.notice = "That is the conversation you are in; switch to another before deleting it."
            return
        self.mode = "confirm"

    def delete(self):
        self.mode = "list"
        row = self.current_row()
        if row is None:
            return
        index = self.selected
        title = row.entry.get("title") or row.id
        if self._quiet(self.manager.delete, row.id):
            self.notice = f"Deleted “{title}”."
        else:
            self.notice = f"“{title}” was already gone."
        self._loaded.pop(row.id, None)
        self.entries = self.manager.list_all()
        self._refilter(keep=False)
        self.move_to(index)

    def _focus(self, name: str):
        """Focus a widget by attribute name; a page driven without a
        terminal (tests) has no application to focus in."""
        if self.app is not None:
            self.app.layout.focus(getattr(self, name))

    def _reload_entries(self, select: Optional[str] = None):
        """Re-read the index after a change; keep (or move to) a row."""
        self.entries = self.manager.list_all()
        self._refilter()
        if select is not None:
            self.selected = next((index for index, row in enumerate(self.rows)
                                  if row.id == select), self.selected)

    def start_rename(self):
        row = self.current_row()
        if row is None:
            return
        conversation = self._conversation(row.id)
        title = (conversation.title if conversation else row.entry.get("title")) or ""
        self.rename_input.text = title
        self.rename_input.buffer.cursor_position = len(title)
        self.mode = "rename"
        self._focus("rename_input")

    def finish_rename(self, save: bool):
        """Enter saves the typed title through the manager; Esc keeps the
        old one. An empty title is refused with a notice."""
        self.mode = "list"
        self._focus("_list_window")
        row = self.current_row()
        if not save or row is None:
            return
        title = normalize_title(self.rename_input.text)
        if not title:
            self.notice = "A title needs at least one visible character; nothing renamed."
            return
        conversation = self._conversation(row.id)
        if conversation is None or self._quiet(self.manager.rename, conversation, title) is None:
            self.notice = "Could not load this conversation; nothing renamed."
            return
        self.notice = f"Renamed to “{title}”."
        self._reload_entries(select=row.id)

    def fork(self):
        """Copy the selected conversation into a new one and select it."""
        row = self.current_row()
        if row is None:
            return
        conversation = self._conversation(row.id)
        forked = self._quiet(self.manager.fork, conversation) if conversation else None
        if forked is None:
            self.notice = "Could not load this conversation; nothing forked."
            return
        self._reload_entries(select=forked.id)
        self.notice = (f"Forked into {forked.id}; {row.id} is unchanged. "
                       "Enter resumes the copy.")

    def export(self):
        """Write the selected conversation to Markdown next to the user
        (never over an existing file) and show where it went."""
        row = self.current_row()
        if row is None:
            return
        conversation = self._conversation(row.id)
        if conversation is None:
            self.notice = "Could not load this conversation; nothing exported."
            return
        try:
            path = export_conversation(conversation, cwd=self.export_dir)
        except OSError as exc:
            self.notice = f"Export failed: {exc}"
            return
        self.notice = f"Exported to {path}"

    # --- rendering --------------------------------------------------------------

    def _list_width(self) -> int:
        columns = self.app.output.get_size().columns
        if columns < _SPLIT_MIN_COLUMNS:
            return columns
        return max(56, min(columns - 40, int(columns * 0.55)))

    def _split(self) -> bool:
        return self.app.output.get_size().columns >= _SPLIT_MIN_COLUMNS

    @staticmethod
    def _columns(width: int) -> Tuple[int, int, int, int, int]:
        when, msgs, cost = 9, 5, 9
        model = 16 if width >= 96 else 0
        fixed = 2 + when + 2 + 2 + msgs + 2 + cost + (2 + model if model else 0)
        return when, max(8, width - fixed), msgs, cost, model

    def _render_header(self, width: int, height: int) -> List[List[Line]]:
        total = _total_usage(self.entries)
        messages = sum(int(_num(entry.get("message_count"))) for entry in self.entries)
        parts = [f" Conversations {len(self.entries)}", f"{messages:,} messages"]
        if _has_usage(total):
            parts.append(f"{_compact(_num(total.get('input_tokens')))} in / "
                         f"{_compact(_num(total.get('output_tokens')))} out tokens")
            parts.append(f"total {_cost_label(total)}")
        else:
            parts.append("no cost recorded yet")
        # Narrow terminals give up message and token counts before the cost.
        while len(parts) > 2 and get_cwidth(" · ".join(parts)) > width:
            del parts[1]
        second = []
        if self._words:
            second.append(f"{len(self.rows)} of {len(self.entries)} match "
                          f"“{self.search.text.strip()}”")
            matched = _total_usage(row.entry for row in self.rows)
            if _has_usage(matched):
                second.append(_cost_label(matched))
        second.append(f"sorted by {self.sort}")
        return [[("class:bar", _fit(" · ".join(parts), width))],
                [("class:muted", _fit(" " + " · ".join(second), width))]]

    def _render_columns(self, width: int, height: int) -> List[List[Line]]:
        when, title, msgs, cost, model = self._columns(width)
        text = ("  " + _fit("UPDATED", when) + "  " + _fit("TITLE", title) + "  "
                + _fit_right("MSGS", msgs) + "  " + _fit_right("COST", cost)
                + ("  " + _fit("MODEL", model) if model else ""))
        return [[("class:column", text)]]

    def _render_list(self, width: int, height: int) -> List[List[Line]]:
        self._list_height = height
        if not self.rows:
            message = ("No saved conversations yet." if not self.entries else
                       f"No conversations match “{self.search.text.strip()}”.")
            return [[("class:muted", _fit("  " + message, width))]]
        if self.selected < self._list_top:
            self._list_top = self.selected
        elif self.selected >= self._list_top + height:
            self._list_top = self.selected - height + 1
        self._list_top = max(0, min(self._list_top, len(self.rows) - height))
        now = datetime.now()
        when_w, title_w, msgs_w, cost_w, model_w = self._columns(width)
        lines = []
        for index in range(self._list_top, min(len(self.rows), self._list_top + height)):
            entry = self.rows[index].entry
            selected = index == self.selected
            plain = "class:selected" if selected else ""
            muted = plain or "class:muted"
            here = entry["id"] == self.current_id
            row = [(plain or "class:current", "●" if here else " "), (plain, " "),
                   (muted, _fit(_ago(entry.get("updated_at"), now), when_w)), (plain, "  ")]
            row += _highlight(_fit(entry.get("title") or "Untitled", title_w), plain, self._pattern)
            row += [(plain, "  "),
                    (muted, _fit_right(f"{int(_num(entry.get('message_count'))):,}", msgs_w)),
                    (plain, "  "),
                    (plain or "class:cost", _fit_right(_cost_cell(entry.get("usage")), cost_w))]
            if model_w:
                row += [(plain, "  "),
                        (muted, _fit(entry.get("model") or entry.get("provider") or "", model_w))]
            lines.append(row)
        return lines

    def _meta_lines(self, entry: Dict[str, Any], conversation: Optional[Conversation],
                    width: int) -> List[Line]:
        title = (conversation.title if conversation else entry.get("title")) or "Untitled"
        provider = (conversation.provider if conversation else entry.get("provider")) or ""
        model = (conversation.model if conversation else entry.get("model")) or ""
        usage = conversation.usage if conversation else entry.get("usage")
        lines = _para(title, width, "class:title", indent=1, max_lines=3)
        lines += _para(f"{entry['id']} · {'/'.join(p for p in (provider, model) if p) or 'model unknown'}",
                       width, "class:muted", indent=1)
        if conversation is not None:
            lines += _para(f"Started {_stamp(conversation.created_at)} · "
                           f"updated {_stamp(conversation.updated_at)}",
                           width, "class:muted", indent=1)
        lines += _para(f"{int(_num(entry.get('message_count'))):,} messages", width, "", indent=1)
        if _has_usage(usage):
            lines += _para(f"{int(_num(usage.get('input_tokens'))):,} in / "
                           f"{int(_num(usage.get('output_tokens'))):,} out tokens · "
                           f"{int(_num(usage.get('turns')))} "
                           f"turn{'' if _num(usage.get('turns')) == 1 else 's'}",
                           width, "", indent=1)
            lines += _para(f"Cost {_cost_label(usage)}", width, "class:cost", indent=1)
        else:
            lines += _para("Cost not recorded (saved before per-conversation "
                           "cost tracking, or no turns yet)", width, "class:muted", indent=1)
        if entry["id"] == self.current_id:
            lines += _para("● You are in this conversation", width, "class:current", indent=1)
        return lines

    def _match_lines(self, conversation: Conversation, width: int, room: int) -> List[Line]:
        matches = []
        for message in conversation.messages:
            role = message.get("role")
            if role == "system":
                continue
            text = _extract_searchable_text(message)
            if role == "user":
                text = strip_turn_context(text)
            folded = text.casefold()
            if any(word in folded for word in self._words):
                kinds = {kind for kind, _ in display_blocks(message)}
                tool = role == "tool" or kinds == {"tool"}
                matches.append((_TOOL_LABEL if tool else _ROLE_LABELS.get(role, _TOOL_LABEL),
                                "class:tool" if tool else "", text))
        if not matches:
            return _para("No message text matches; matched on the title, id, "
                         "model, or recalled context.", width, "class:muted", indent=1)
        lines = [("class:column", f" {len(matches)} matching message"
                                  f"{'' if len(matches) == 1 else 's'}")]
        for label, style, text in matches:
            if len(lines) >= room:
                break
            lines.append(label)
            lines += _para(_extract_snippet(text, self._words, 160), width,
                           style, indent=3, max_lines=3)
        return lines

    def _latest_lines(self, conversation: Conversation, width: int, room: int) -> List[Line]:
        blocks: List[List[Line]] = []
        used = 1
        for message in reversed(conversation.messages):
            if used >= room:
                break
            if message.get("role") not in _ROLE_LABELS:
                continue
            said = [(kind, text) for kind, text in display_blocks(message) if kind == "text"]
            if not said:
                continue
            rows = [_ROLE_LABELS[message["role"]]]
            for _, text in said:
                rows += _para(text, width, "", indent=3, max_lines=4)
            blocks.append(rows)
            used += len(rows)
        if not blocks:
            return _para("No messages yet.", width, "class:muted", indent=1)
        lines = [("class:column", " Latest")]
        for rows in reversed(blocks):
            lines += rows
        if len(lines) > room:
            # Room ran out: keep the newest rows, they matter most here.
            lines = lines[:1] + (lines[len(lines) - (room - 1):] if room > 1 else [])
        return lines

    def _render_preview(self, width: int, height: int) -> List[List[Line]]:
        row = self.current_row()
        if row is None:
            return []
        conversation = self._conversation(row.id)
        key = (row.id, tuple(self._words), width, height, self.current_id,
               conversation.updated_at if conversation else None)
        if self._preview_cache[0] != key:
            lines = self._meta_lines(row.entry, conversation, width - 1)
            lines.append(("", ""))
            room = max(0, height - len(lines))
            if conversation is None:
                lines += _para("Could not load this conversation.", width - 1,
                               "class:error", indent=1)
            elif self._words:
                lines += self._match_lines(conversation, width - 1, room)
            else:
                lines += self._latest_lines(conversation, width - 1, room)
            self._preview_cache = (key, lines[:height])
        return [_highlight(text, style, self._pattern) for style, text in self._preview_cache[1]]

    def _view_lines(self, row: _Row, width: int) -> List[Line]:
        conversation = self._conversation(row.id)
        key = (row.id, width, self.current_id,
               conversation.updated_at if conversation else None)
        if self._view_cache[0] != key:
            lines = self._meta_lines(row.entry, conversation, width - 1)
            if conversation is None:
                lines += _para("Could not load this conversation.", width - 1,
                               "class:error", indent=1)
            else:
                for message in conversation.messages:
                    rows = _message_lines(message, width - 1)
                    if rows and rows[0] in _ROLE_LABELS.values():
                        lines.append(("", ""))
                    lines += rows
            self._view_cache = (key, lines)
        return self._view_cache[1]

    def _render_view(self, width: int, height: int) -> List[List[Line]]:
        row = self.current_row()
        if row is None:
            return []
        lines = self._view_lines(row, width)
        self._view_height = height
        self._view_top = max(0, min(self._view_top, len(lines) - height))
        return [_highlight(text, style, self._pattern)
                for style, text in lines[self._view_top:self._view_top + height]]

    def _render_footer(self, width: int, height: int) -> List[List[Line]]:
        if self.mode == "confirm":
            row = self.current_row()
            title = (row.entry.get("title") if row else "") or "this conversation"
            return [[("class:warning", _fit(
                f" Delete “{title}”? Its transcript is removed for good. "
                "y deletes · any other key keeps it", width))]]
        if self.notice:
            return [[("class:notice", _fit(" " + self.notice, width))]]
        hints = {
            "list": ["Enter resume", "/ search", "v read", "r rename", "f fork",
                     "e export", "s sort", "d delete", "q quit", "↑↓ PgUp PgDn move"],
            "search": ["type to filter", "Enter keep filter", "Esc clear", "↑↓ move"],
            "view": ["Enter resume", "Esc back", "↑↓ PgUp PgDn scroll", "g/G top/end"],
        }[self.mode]
        return [[("class:muted", " " + _fit_hints(hints, width - 1))]]

    # --- application --------------------------------------------------------------

    def build(self, input=None, output=None) -> Application:
        self._list_window = Window(_Pane(self._render_list, focusable=True),
                                   wrap_lines=False, always_hide_cursor=True)
        self._view_window = Window(_Pane(self._render_view, focusable=True),
                                   wrap_lines=False, always_hide_cursor=True)
        listing = HSplit([Window(_Pane(self._render_columns), height=1), self._list_window],
                         width=self._list_width)
        preview = VSplit([Window(width=1, char="│", style="class:rule"),
                          Window(_Pane(self._render_preview), wrap_lines=False,
                                 always_hide_cursor=True)])
        body = VSplit([listing, ConditionalContainer(preview, Condition(self._split))])
        root = HSplit([
            Window(_Pane(self._render_header), height=2),
            ConditionalContainer(self.search, Condition(
                lambda: self.mode == "search" or bool(self.search.text))),
            ConditionalContainer(body, Condition(lambda: self.mode != "view")),
            ConditionalContainer(self._view_window, Condition(lambda: self.mode == "view")),
            ConditionalContainer(Window(_Pane(self._render_footer), height=1),
                                 Condition(lambda: self.mode != "rename")),
            # Renaming edits the title in place of the footer.
            ConditionalContainer(self.rename_input, Condition(lambda: self.mode == "rename")),
        ])
        no_color = "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
        self.app = Application(
            layout=Layout(root, focused_element=self._list_window),
            key_bindings=self._key_bindings(), full_screen=True, mouse_support=False,
            enable_page_navigation_bindings=False,
            input=input, output=output,
            color_depth=ColorDepth.DEPTH_1_BIT if no_color else None,
            style=Style.from_dict({
                "bar": "reverse bold", "title": "bold", "column": "bold #888888",
                "muted": "#888888", "rule": "#555555", "selected": "reverse",
                "current": "bold #5fafff", "cost": "#87d787", "match": "bold #ffd75f",
                "user": "bold #ffd75f", "assistant": "bold #5fafff", "tool": "#888888",
                "prompt": "bold", "notice": "#ffd75f", "warning": "bold #ff8787",
                "error": "#ff6666",
            }),
        )
        # Escape answers at once: the page binds no Alt/Meta sequences.
        self.app.ttimeoutlen = 0.05
        self.app.key_processor.before_key_press += self._clear_notice
        return self.app

    def _clear_notice(self, _sender=None):
        if self.mode != "confirm":
            self.notice = ""

    def run(self, input=None, output=None) -> Optional[str]:
        if input is None:
            input = create_input(sys.stdin)
        if output is None:
            output = create_output(stdout=sys.__stdout__ or sys.stdout)
        self.build(input=input, output=output).run()
        return self.result

    def _key_bindings(self) -> KeyBindings:
        keys = KeyBindings()
        listing = Condition(lambda: self.mode == "list")
        searching = Condition(lambda: self.mode == "search")
        moving = Condition(lambda: self.mode in ("list", "search"))
        viewing = Condition(lambda: self.mode == "view")
        confirming = Condition(lambda: self.mode == "confirm")
        renaming = Condition(lambda: self.mode == "rename")

        def page(height: int) -> int:
            return max(1, height - 1)

        @keys.add("c-c", eager=True)
        def _quit(event):
            event.app.exit()

        @keys.add("up", filter=moving)
        @keys.add("c-p", filter=moving)
        @keys.add("k", filter=listing)
        def _up(event):
            self.move(-1)

        @keys.add("down", filter=moving)
        @keys.add("c-n", filter=moving)
        @keys.add("j", filter=listing)
        def _down(event):
            self.move(1)

        @keys.add("pageup", filter=moving)
        def _page_up(event):
            self.move(-page(self._list_height))

        @keys.add("pagedown", filter=moving)
        def _page_down(event):
            self.move(page(self._list_height))

        @keys.add("home", filter=listing)
        @keys.add("g", filter=listing)
        def _top(event):
            self.move_to(0)

        @keys.add("end", filter=listing)
        @keys.add("G", filter=listing)
        def _bottom(event):
            self.move_to(len(self.rows) - 1)

        @keys.add("enter", filter=listing | viewing)
        def _resume(event):
            self.resume()

        @keys.add("enter", filter=searching)
        def _keep_filter(event):
            self.end_search(clear=False)

        @keys.add("escape", filter=searching, eager=True)
        def _clear_filter(event):
            self.end_search(clear=True)

        @keys.add("escape", filter=listing, eager=True)
        def _escape(event):
            if self.search.text:
                self.end_search(clear=True)
            else:
                event.app.exit()

        @keys.add("q", filter=listing)
        def _q(event):
            event.app.exit()

        @keys.add("/", filter=listing)
        def _search(event):
            self.start_search()

        @keys.add("s", filter=listing)
        def _sort(event):
            self.cycle_sort()

        @keys.add("d", filter=listing)
        @keys.add("delete", filter=listing)
        def _delete(event):
            self.ask_delete()

        @keys.add("v", filter=listing)
        @keys.add("right", filter=listing)
        def _view(event):
            self.open_view()

        @keys.add("escape", filter=viewing, eager=True)
        @keys.add("q", filter=viewing)
        @keys.add("left", filter=viewing)
        def _back(event):
            self.close_view()

        @keys.add("up", filter=viewing)
        @keys.add("k", filter=viewing)
        def _scroll_up(event):
            self.scroll(-1)

        @keys.add("down", filter=viewing)
        @keys.add("j", filter=viewing)
        def _scroll_down(event):
            self.scroll(1)

        @keys.add("pageup", filter=viewing)
        @keys.add("b", filter=viewing)
        def _view_page_up(event):
            self.scroll(-page(self._view_height))

        @keys.add("pagedown", filter=viewing)
        @keys.add("space", filter=viewing)
        def _view_page_down(event):
            self.scroll(page(self._view_height))

        @keys.add("home", filter=viewing)
        @keys.add("g", filter=viewing)
        def _view_top(event):
            self._view_top = 0

        @keys.add("end", filter=viewing)
        @keys.add("G", filter=viewing)
        def _view_bottom(event):
            self._view_top = sys.maxsize

        @keys.add("r", filter=listing)
        def _rename(event):
            self.start_rename()

        @keys.add("enter", filter=renaming)
        def _save_title(event):
            self.finish_rename(save=True)

        @keys.add("escape", filter=renaming, eager=True)
        def _keep_title(event):
            self.finish_rename(save=False)

        @keys.add("f", filter=listing)
        def _fork(event):
            self.fork()

        @keys.add("e", filter=listing)
        def _export(event):
            self.export()

        @keys.add("y", filter=confirming)
        def _confirm(event):
            self.delete()

        @keys.add("escape", filter=confirming, eager=True)
        @keys.add("<any>", filter=confirming)
        def _keep(event):
            self.mode = "list"

        return keys
