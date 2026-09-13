"""/notes — editor-backed personal notes (personal-items addendum N1).

Notes are items in the ``notes`` space of the kernel items aggregate:
same store as /todo, same event-sourcing discipline (every edit is an
``item_updated`` event, so the full edit history is free), same
credential write-guard, same local-only privacy rules. What this module
adds is the editing UX — ``/notes new`` and ``/notes open`` hand the
real terminal to the user's editor through the direct secure-terminal
handoff, under the exact /terminal authority gate (interactive local
sessions with a live TTY only; remote and channel sessions are refused
and pointed at quick-add and the personal_items tool, which work
everywhere). The saved buffer's first ``# Title`` line names the note;
an abandoned empty buffer stores nothing.

Kernel imports stay inside functions so the classic shell remains
kernel-free until a notes command actually runs. Note content is stored
text: it is printed verbatim and never parsed as commands, approvals,
or instructions.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
import time as _time
from typing import Any, Dict, List, Optional, Tuple

NOTES_SPACE = "notes"

_NOTE_VERBS = (
    "new", "open", "add", "show", "search", "archive", "reopen", "list",
)

NOTES_USAGE = (
    "\n  \033[1;36m/notes — editor-backed notes"
    " (items in the `notes` space):\033[0m\n"
    "    /notes                      recent notes\n"
    "    /notes new [title]          write a note in your editor\n"
    "    /notes open <#|id|title>    reopen in the editor;"
    " save records an edit\n"
    "    /notes add <title> [#tag ...] [-- body]   quick note, no editor\n"
    "    /notes show <#|id|title>    full note + edit history\n"
    "    /notes search <text>        search titles and bodies\n"
    "    /notes archive|reopen <#|id|title>\n"
    "  \033[2mEditor: `editor` config → $VISUAL → $EDITOR → nano → vi."
    " The buffer's first\n  line '# Title' names the note; an abandoned"
    " empty buffer stores nothing.\n  /note aliases /notes."
    " Titles match by unambiguous substring.\033[0m\n"
)

#: A block-rejection sentinel distinct from every legitimate store result.
_BLOCKED = object()

#: First non-blank buffer line as an ATX heading. Heading text is
#: optional so a bare ``#`` line never leaks into a body or a title.
_HEADING_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")


# ---------------------------------------------------------------------------
# Buffer parsing (pure — the deterministic title rules the tests pin down)
# ---------------------------------------------------------------------------

def parse_note_buffer(text: str, fallback_title: str = "",
                      now: Optional[float] = None,
                      ) -> Optional[Tuple[str, str]]:
    """(title, body) from a saved editor buffer, or None when empty.

    Title precedence: the first non-blank line's ``# Title`` heading,
    then *fallback_title* (the ``/notes new`` argument, or the existing
    title on reopen), then ``Untitled YYYY-MM-DD``. A heading line never
    reaches the body. A buffer with no content — or only a bare ``#``
    heading with nothing else — is an abandoned note.
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return None
    lines = text.split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    heading = _HEADING_RE.match(lines[index])
    if heading:
        title = (heading.group(2) or "").strip()
        body_lines = lines[index + 1:]
    else:
        title = ""
        body_lines = lines[index:]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    body = "\n".join(body_lines).rstrip()
    if not title:
        title = str(fallback_title or "").strip()
    if not title:
        if heading and not body:
            return None  # a lone "#" is an empty buffer, not a note
        stamp = _time.strftime(
            "%Y-%m-%d", _time.localtime(now if now is not None
                                        else _time.time())
        )
        title = f"Untitled {stamp}"
    return title, body


# ---------------------------------------------------------------------------
# Editor handoff (the /terminal gate, unchanged: DirectTerminalRunner
# behind the session's interactive_terminal client)
# ---------------------------------------------------------------------------

def _editor_terminal(tool_map: Optional[Dict[str, Any]]):
    """The session's interactive_terminal client when a handoff is
    possible right now, else None. The client's policy is the authority
    boundary — scheduled, remote, and channel sessions carry
    interactive=False and land here as None."""
    terminal = (tool_map or {}).get("interactive_terminal")
    if terminal is None:
        return None
    check = getattr(terminal, "handoff_available", None)
    if not callable(check) or not check():
        return None
    return terminal


def _print_editor_refusal() -> None:
    print(
        "\n  \033[31mEditor notes need the interactive local session —"
        " the same gate as /terminal.\033[0m\n"
        "  \033[2mFrom here: /notes add <title> -- body captures a note"
        " without the editor,\n  and asking in chat works everywhere —"
        " the personal_items tool writes to the\n  same notes"
        " space.\033[0m\n"
    )


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _open_editor(config: dict, terminal, seed: str,
                 what: str) -> Tuple[Optional[str], str]:
    """Run the resolved editor on a scratch file through the terminal
    handoff. Returns (buffer_text, scratch_path) after a clean save,
    else (None, "") with the reason already printed. The caller owns
    the scratch file: deleted after storing, kept when the write-guard
    rejects — the user's text must not vanish with the block."""
    from .config import resolve_editor

    editor = resolve_editor(config)
    try:
        argv = shlex.split(editor)
    except ValueError:
        argv = [editor]
    if not argv:
        argv = ["vi"]
    fd, path = tempfile.mkstemp(prefix="conch-note-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(seed)
    except OSError as exc:
        _unlink(path)
        print(f"\n  \033[31mCannot write the note scratch file:"
              f" {exc}\033[0m\n")
        return None, ""
    result, _ = terminal.run_argv(
        argv + [path],
        description=f"Open {argv[0]} {what}?",
        timeout=0,
        noun="Editor",
    )
    if not result.approved:
        _unlink(path)
        if result.error:
            print(f"\n  \033[31mRefused: {result.error}.\033[0m\n")
        else:
            print("\n  \033[2mEditor cancelled — nothing saved.\033[0m\n")
        return None, ""
    if result.error:
        _unlink(path)
        print(f"\n  \033[31mCould not launch {editor!r}:"
              f" {result.error}\033[0m\n")
        return None, ""
    if result.interrupted or result.timed_out or result.returncode != 0:
        _unlink(path)
        reason = (
            "was interrupted" if result.interrupted
            else "timed out" if result.timed_out
            else f"exited {result.returncode}"
        )
        print(f"\n  \033[2mEditor {reason} — nothing saved.\033[0m\n")
        return None, ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), path
    except OSError as exc:
        _unlink(path)
        print(f"\n  \033[31mCannot read the note back: {exc}\033[0m\n")
        return None, ""


def _store_guarded(path: str, write):
    """Run a store write, translating a credential rejection into the
    standard whole-entry block message. Returns the write's result, or
    the _BLOCKED sentinel after printing (keeping the scratch file when
    there is one — editor text survives the block)."""
    from .secretguard import CredentialRejected

    try:
        return write()
    except CredentialRejected as exc:
        print(
            f"\n  \033[31mNot saved: matches credential pattern(s)"
            f" ({', '.join(exc.types)}).\033[0m\n"
            "  \033[2mNotes never store secrets — the whole note was"
            " rejected. Save a reference\n  instead (which env var /"
            " keychain item / config file holds it)."
        )
        if path:
            print(f"  Your text is kept at {path} — edit the secret out"
                  " and try again.\033[0m\n")
        else:
            print("\033[0m\n")
        return _BLOCKED


# ---------------------------------------------------------------------------
# Rendering and resolution
# ---------------------------------------------------------------------------

def _note_line(item: Dict[str, Any]) -> str:
    """One plain-text line: status mark, #N, title, tags, date, id."""
    from .kernel.items import format_stamp

    mark = {"open": " ", "done": "x", "archived": "~"}.get(
        item["status"], "?"
    )
    parts = [f"[{mark}] #{item['item_seq']}", str(item["title"])]
    if item.get("tags"):
        parts.append(" ".join(f"#{tag}" for tag in item["tags"]))
    stamp = format_stamp(
        item.get("updated_at") or item.get("created_at") or 0
    )[:10]
    parts.append(f"— {stamp}")
    parts.append(f"({item['item_id']})")
    return " ".join(parts)


def _edit_events(store, item_id: str) -> List[Dict[str, Any]]:
    return [
        event for event in store.item_events(item_id, limit=500)
        if event["kind"] == "item_updated"
    ]


def _edit_summary(edits: List[Dict[str, Any]]) -> str:
    """The show-surface line the event log buys us for free."""
    from .kernel.items import format_stamp

    if not edits:
        return "never edited"
    times = "time" if len(edits) == 1 else "times"
    return (
        f"edited {len(edits)} {times},"
        f" last {format_stamp(edits[-1]['created_at'])}"
    )


def _resolve_note(store, ref: str) -> Optional[Dict[str, Any]]:
    """A note from #N, an id prefix, or a title substring. Returns the
    item, or None after printing what went wrong — an ambiguous title
    lists the candidates, never guesses."""
    ref = str(ref or "").strip()
    if not ref:
        print("\n  \033[2mReference a note by #N, id prefix, or a title"
              " fragment (/notes lists recent).\033[0m\n")
        return None
    item = store.resolve_item(ref)
    if item is not None and item.get("space") == NOTES_SPACE:
        return item
    needle = ref.lower()
    notes = store.list_items(space=NOTES_SPACE, status="all", limit=500)
    matches = [
        note for note in notes if needle in str(note["title"]).lower()
    ]
    if len(matches) > 1:
        exact = [
            note for note in matches
            if str(note["title"]).lower() == needle
        ]
        if len(exact) == 1:
            return exact[0]
        print(f"\n  \033[33m{len(matches)} notes match {ref!r} — be more"
              " specific or use #N:\033[0m")
        for note in matches[:8]:
            print("    " + _note_line(note))
        print()
        return None
    if matches:
        return matches[0]
    print(f"\n  \033[31mNo note matching {ref!r}.\033[0m \033[2m/notes"
          " lists recent notes; /notes search <text> searches titles"
          " and bodies.\033[0m\n")
    return None


def _print_recent(store) -> None:
    from .kernel.model import ItemStatus

    notes = store.list_items(space=NOTES_SPACE, status=ItemStatus.OPEN)
    notes.sort(
        key=lambda item: (-float(item.get("updated_at") or 0.0),
                          item["item_id"])
    )
    archived = len(
        store.list_items(space=NOTES_SPACE, status=ItemStatus.ARCHIVED)
    )
    if not notes:
        extra = (
            f" {archived} archived — /notes search finds them,"
            " /notes reopen restores." if archived else ""
        )
        print(
            "\n  \033[2mNo notes yet. /notes new [title] writes one in"
            " your editor; /notes add <title>\n  -- body is the quick"
            f" form.{extra}\033[0m\n"
        )
        return
    shown = notes[:10]
    header = (
        f"Notes — {len(shown)} of {len(notes)}, recent first"
        if len(notes) > len(shown) else f"Notes ({len(notes)})"
    )
    print(f"\n  \033[1;36m{header}\033[0m")
    for item in shown:
        print("    " + _note_line(item))
    tail = ("new [title] · open <#|title> · add <title> -- body · show"
            " · search <text> · archive")
    if archived:
        tail += f"  ({archived} archived)"
    print(f"  \033[2m{tail}\033[0m\n")


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def _cmd_new(store, rest: str, config: dict,
             tool_map: Optional[Dict[str, Any]], now: float) -> None:
    terminal = _editor_terminal(tool_map)
    if terminal is None:
        _print_editor_refusal()
        return
    title_arg = rest.strip()
    seed = f"# {title_arg}\n\n" if title_arg else ""
    print("\n  \033[2mFirst line '# Title' names the note; save+quit"
          " stores it; an empty buffer\n  stores nothing.\033[0m")
    text, path = _open_editor(config, terminal, seed, "for a new note")
    if text is None:
        return
    parsed = parse_note_buffer(text, fallback_title=title_arg, now=now)
    if parsed is None:
        _unlink(path)
        print("\n  \033[2mEmpty buffer — no note created.\033[0m\n")
        return
    title, body = parsed
    item = _store_guarded(path, lambda: store.add_item(
        title, space=NOTES_SPACE, body=body, source="editor",
        actor="user",
    ))
    if item is _BLOCKED:
        return
    _unlink(path)
    print("\n  \033[1;32m✓ Note saved\033[0m " + _note_line(item) + "\n")


def _cmd_open(store, rest: str, config: dict,
              tool_map: Optional[Dict[str, Any]], now: float) -> None:
    terminal = _editor_terminal(tool_map)
    if terminal is None:
        _print_editor_refusal()
        return
    item = _resolve_note(store, rest)
    if item is None:
        return
    body = str(item.get("body") or "")
    seed = f"# {item['title']}\n\n"
    if body:
        seed += body if body.endswith("\n") else body + "\n"
    print("\n  \033[2mSave+quit records an edit; an empty buffer leaves"
          " the note unchanged.\033[0m")
    what = f"on note #{item['item_seq']} ({str(item['title'])[:40]!r})"
    text, path = _open_editor(config, terminal, seed, what)
    if text is None:
        return
    parsed = parse_note_buffer(
        text, fallback_title=str(item["title"]), now=now
    )
    if parsed is None:
        _unlink(path)
        print("\n  \033[2mEmpty buffer — note unchanged.\033[0m\n")
        return
    title, new_body = parsed
    fields: Dict[str, Any] = {}
    if title != item["title"]:
        fields["title"] = title
    if new_body != body:
        fields["body"] = new_body
    if not fields:
        _unlink(path)
        print("\n  \033[2mNo changes.\033[0m\n")
        return
    outcome = _store_guarded(path, lambda: store.update_item(
        item["item_id"], fields, actor="user", source="editor",
    ))
    if outcome is _BLOCKED:
        return
    _unlink(path)
    updated = store.get_item(item["item_id"])
    edits = _edit_events(store, item["item_id"])
    print("\n  \033[1;32m✓ Edited\033[0m " + _note_line(updated)
          + f"\n    \033[2m{_edit_summary(edits)}\033[0m\n")


def _cmd_add(store, rest: str, now: float) -> None:
    from .commands import _parse_item_add

    # The " -- " body delimiter is only recognizable mid-string; pad so
    # a bodied add with no title ("/notes add -- text") parses as an
    # empty title (→ usage) instead of titling the note "--".
    parsed = _parse_item_add(" " + rest.strip(), now)
    if not parsed["title"]:
        print("\n  \033[2mUsage: /notes add <title> [#tag ...]"
              " [-- body]\033[0m\n")
        return
    item = _store_guarded("", lambda: store.add_item(
        parsed["title"], space=NOTES_SPACE, body=parsed["body"],
        due_at=parsed["due_at"], priority=parsed["priority"],
        tags=parsed["tags"], source="chat", actor="user",
    ))
    if item is _BLOCKED:
        return
    print("\n  \033[1;32m✓ Note saved\033[0m " + _note_line(item) + "\n")


def _cmd_show(store, rest: str, now: float) -> None:
    from .kernel import items as items_mod

    item = _resolve_note(store, rest)
    if item is None:
        return
    print()
    for line in items_mod.item_detail(store, item, now).splitlines():
        print("  " + line)
    edits = _edit_events(store, item["item_id"])
    print(f"  \033[2m{_edit_summary(edits)}\033[0m\n")


def _cmd_search(store, rest: str) -> None:
    needle = rest.strip()
    if not needle:
        print("\n  \033[2mUsage: /notes search <text>\033[0m\n")
        return
    hits = store.search_items(needle, space=NOTES_SPACE)
    if not hits:
        print(f"\n  \033[2mNo notes matching {needle!r}.\033[0m\n")
        return
    print(f"\n  \033[1;36mNotes matching {needle!r}"
          f" ({len(hits)})\033[0m")
    for item in hits:
        print("    " + _note_line(item))
    print()


def _cmd_archive(store, rest: str) -> None:
    from .kernel.model import ItemStatus

    item = _resolve_note(store, rest)
    if item is None:
        return
    if item["status"] == ItemStatus.ARCHIVED:
        print(f"\n  \033[2mNote #{item['item_seq']} is already"
              " archived.\033[0m\n")
        return
    store.archive_item(item["item_id"], actor="user", source="chat")
    updated = store.get_item(item["item_id"])
    print("\n  \033[1;32m✓ Archived\033[0m " + _note_line(updated) + "\n")


def _cmd_reopen(store, rest: str) -> None:
    from .kernel.model import ItemStatus

    item = _resolve_note(store, rest)
    if item is None:
        return
    if item["status"] == ItemStatus.OPEN:
        print(f"\n  \033[2mNote #{item['item_seq']} is already"
              " open.\033[0m\n")
        return
    store.update_item(
        item["item_id"], {"status": ItemStatus.OPEN}, actor="user",
        source="chat",
    )
    updated = store.get_item(item["item_id"])
    print("\n  \033[1;32m✓ Reopened\033[0m " + _note_line(updated) + "\n")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle_notes_command(arg: str, config: dict,
                         tool_map: Optional[Dict[str, Any]] = None,
                         ) -> None:
    """Body of /notes and /note. All output is printed; returns None."""
    from .kernel.model import KernelError
    from .kernel.store import MissionStore
    from .secretguard import CredentialRejected

    arg = (arg or "").strip()
    parts = arg.split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    if sub in ("help", "-h", "--help"):
        print(NOTES_USAGE)
        return None
    if sub and sub not in _NOTE_VERBS:
        print(NOTES_USAGE)
        return None
    try:
        store = MissionStore()
    except Exception as exc:
        print(f"\n  \033[31mNotes unavailable: cannot open the kernel"
              f" store: {exc}\033[0m\n")
        return None
    try:
        now = _time.time()
        if not sub or sub == "list":
            _print_recent(store)
        elif sub == "new":
            _cmd_new(store, rest, config, tool_map, now)
        elif sub == "open":
            _cmd_open(store, rest, config, tool_map, now)
        elif sub == "add":
            _cmd_add(store, rest, now)
        elif sub == "show":
            _cmd_show(store, rest, now)
        elif sub == "search":
            _cmd_search(store, rest)
        elif sub == "archive":
            _cmd_archive(store, rest)
        elif sub == "reopen":
            _cmd_reopen(store, rest)
        return None
    except CredentialRejected as exc:
        print(
            f"\n  \033[31mNot saved: matches credential pattern(s)"
            f" ({', '.join(exc.types)}).\033[0m\n"
            "  \033[2mNotes never store secrets. Save a reference"
            " instead (which env var / keychain\n  item / config file"
            " holds it).\033[0m\n"
        )
        return None
    except KernelError as exc:
        print(f"\n  \033[31mNote command failed: {exc}\033[0m\n")
        return None
    finally:
        store.close()
