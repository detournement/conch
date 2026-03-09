"""Curses-based conversation browser for Conch chat.

Two-pane TUI: scrollable conversation list on the left, message preview on
the right. Returns the selected conversation ID to the chat loop.

Launched via /browse in chat.
"""
import curses
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .conversations import ConversationManager


def browse_conversations(conv_mgr: "ConversationManager",
                         current_id: str = "") -> Optional[str]:
    """Show interactive conversation browser. Returns conv ID, 'new', or None."""
    convos = conv_mgr.list_all()
    if not convos:
        return "new"

    cache: dict = {}

    def _load_preview(conv_id: str) -> list:
        if conv_id in cache:
            return cache[conv_id]
        conv = conv_mgr.load(conv_id)
        if not conv:
            cache[conv_id] = []
            return []
        msgs = []
        for m in conv.messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                msgs.append((role, content.strip()))
        cache[conv_id] = msgs
        return msgs

    def _draw(stdscr, selected: int, scroll_offset: int, preview_scroll: int):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 5 or w < 40:
            stdscr.addstr(0, 0, "Terminal too small")
            return

        left_w = min(max(w // 3, 28), 45)
        right_w = w - left_w - 1

        # Colors
        curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(5, curses.COLOR_YELLOW, curses.COLOR_BLACK)

        cyan = curses.color_pair(1)
        sel_pair = curses.color_pair(2) | curses.A_BOLD
        green = curses.color_pair(3)
        white = curses.color_pair(4)
        yellow = curses.color_pair(5)
        dim = curses.A_DIM

        # Header
        header = " Conversations"
        stdscr.addnstr(0, 0, header + " " * (left_w - len(header)), left_w, cyan | curses.A_BOLD)
        stdscr.addnstr(0, left_w + 1, " Preview", right_w, cyan | curses.A_BOLD)

        # Divider
        for row in range(1, h - 1):
            try:
                stdscr.addch(row, left_w, curses.ACS_VLINE, dim)
            except curses.error:
                pass

        # Left pane — conversation list
        list_h = h - 3
        visible = convos[scroll_offset:scroll_offset + list_h]
        for i, c in enumerate(visible):
            row = i + 1
            if row >= h - 1:
                break
            idx = scroll_offset + i
            is_sel = idx == selected
            is_current = c["id"] == current_id

            title = c.get("title", "untitled")[:left_w - 4]
            msgs = c.get("message_count", 0)
            date = c.get("updated_at", "")[:10]
            meta = f"  {msgs}m {date}"

            attr = sel_pair if is_sel else (white | curses.A_BOLD if is_current else white)
            marker = ">" if is_sel else " "

            line = f"{marker} {title}"
            line = line[:left_w - 1]
            stdscr.addnstr(row, 0, line + " " * (left_w - len(line)), left_w, attr)

            meta_row = row
            if left_w > len(line) + len(meta) + 1:
                try:
                    stdscr.addnstr(meta_row, left_w - len(meta) - 1, meta, len(meta), dim if not is_sel else sel_pair)
                except curses.error:
                    pass

        # Right pane — message preview
        if convos:
            sel_id = convos[selected]["id"]
            msgs = _load_preview(sel_id)
            preview_lines: list = []
            for role, content in msgs:
                tag = "you: " if role == "user" else "assistant: "
                tag_lines = content.split("\n")
                first = True
                for tl in tag_lines:
                    while len(tl) > right_w - 3:
                        chunk = tl[:right_w - 3]
                        preview_lines.append((role, (tag if first else "  ") + chunk))
                        tl = tl[right_w - 3:]
                        first = False
                    preview_lines.append((role, (tag if first else "  ") + tl))
                    first = False
                preview_lines.append(("sep", ""))

            vis_start = preview_scroll
            for i, (role, line) in enumerate(preview_lines[vis_start:]):
                row = i + 1
                if row >= h - 1:
                    break
                attr = yellow | curses.A_BOLD if role == "user" else (cyan if role == "assistant" else dim)
                try:
                    stdscr.addnstr(row, left_w + 2, line[:right_w - 2], right_w - 2, attr)
                except curses.error:
                    pass

        # Footer
        footer = " ↑↓ navigate  Enter select  n new  d delete  q back"
        try:
            stdscr.addnstr(h - 1, 0, footer + " " * (w - len(footer)), w, dim)
        except curses.error:
            pass

        stdscr.refresh()

    def _main(stdscr):
        curses.curs_set(0)
        stdscr.timeout(100)
        selected = 0
        scroll_offset = 0
        preview_scroll = 0

        # Start on current conversation if it exists
        for i, c in enumerate(convos):
            if c["id"] == current_id:
                selected = i
                break

        while True:
            h, _ = stdscr.getmaxyx()
            list_h = max(h - 3, 1)

            if selected < scroll_offset:
                scroll_offset = selected
            if selected >= scroll_offset + list_h:
                scroll_offset = selected - list_h + 1

            _draw(stdscr, selected, scroll_offset, preview_scroll)

            try:
                key = stdscr.getch()
            except curses.error:
                continue

            if key == curses.KEY_RESIZE:
                continue
            elif key in (ord("q"), ord("Q"), 27):  # q or Esc
                return None
            elif key == curses.KEY_UP or key == ord("k"):
                if selected > 0:
                    selected -= 1
                    preview_scroll = 0
            elif key == curses.KEY_DOWN or key == ord("j"):
                if selected < len(convos) - 1:
                    selected += 1
                    preview_scroll = 0
            elif key == ord("n") or key == ord("N"):
                return "new"
            elif key == ord("d") or key == ord("D"):
                if convos and convos[selected]["id"] != current_id:
                    cid = convos[selected]["id"]
                    conv_mgr.delete(cid)
                    if cid in cache:
                        del cache[cid]
                    convos.clear()
                    convos.extend(conv_mgr.list_all())
                    if selected >= len(convos):
                        selected = max(0, len(convos) - 1)
                    if not convos:
                        return "new"
            elif key in (curses.KEY_ENTER, 10, 13):
                if convos:
                    return convos[selected]["id"]
            elif key == curses.KEY_NPAGE or key == ord(" "):
                preview_scroll += list_h
            elif key == curses.KEY_PPAGE:
                preview_scroll = max(0, preview_scroll - list_h)

        return None

    try:
        return curses.wrapper(_main)
    except Exception:
        return None
