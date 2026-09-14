"""One-message multiline input for the interactive chat prompt.

The chat prompt reads through readline's ``input()``, which submits on the
first newline: pasting a block used to fire every line as its own message —
and any interior line starting with "/" as a slash command. This module
assembles one logical message from cooperating layers:

1. Bracketed paste — GNU readline 8.1+ honors the terminal's paste markers
   (``set enable-bracketed-paste on``) and inserts a multiline paste into the
   edit buffer as literal text, so one Enter submits the whole block.
   macOS system Pythons link libedit, where the directive is a silent no-op
   (verified on 3.9–3.14), so support is detected, never assumed.
2. Pending-input drain — on any backend, when a paste's first newline makes
   ``input()`` return, the rest of the paste is still in the tty input queue.
   ``drain_pending_input`` scoops it up raw (tabs intact, no echo mangling)
   and ``read_user_message`` stitches it back into one block, then waits for
   a single Enter on the "... " prompt before sending — the same
   paste-then-confirm shape bracketed paste gives on GNU. This is the
   primary path on libedit.
3. Continuation — an input that opens a ``` fence keeps reading verbatim
   lines until the closing fence; a typed line ending in a single backslash
   continues on the next line.
4. Explicit composers — ``read_paste_block`` (/paste: raw tty lines until a
   lone "." or Ctrl+D) and ``edit_in_editor`` (/edit: the resolved editor
   on a temp file) work on any terminal and any backend.

Ctrl+C anywhere mid-assembly cancels the whole pending block; nothing
partial is ever submitted. Readline history entries must stay single lines
(the history file is newline-delimited), so assembled multiline messages are
collapsed into one entry with newlines shown as ``HISTORY_NEWLINE_MARK``.
"""

from __future__ import annotations

import os
import readline
import select
import shlex
import subprocess
import sys
import tempfile
import termios
from typing import Callable, List, Optional

CONTINUATION_PROMPT = "\033[2m... \033[0m"
PASTE_SENTINEL = "."
HISTORY_NEWLINE_MARK = " ⏎ "

# A paste's remaining bytes are already in flight when input() returns, so a
# ~20ms probe cannot race a human keystroke after Enter but reliably catches
# the same paste; the quiet window tolerates chunked delivery (ssh, large
# blocks) between reads.
PASTE_PROBE_S = 0.02
PASTE_QUIET_S = 0.08


def readline_backend() -> str:
    """"readline" (GNU) or "editline" (libedit), like readline.backend on 3.13+."""
    backend = getattr(readline, "backend", None)
    if backend:
        return backend
    return "editline" if "libedit" in (readline.__doc__ or "") else "readline"


def bracketed_paste_supported() -> bool:
    """True only for GNU readline 8.1+, which implements enable-bracketed-paste."""
    if readline_backend() != "readline":
        return False
    return getattr(readline, "_READLINE_RUNTIME_VERSION", 0) >= 0x0801


def enable_bracketed_paste() -> bool:
    """Turn on bracketed paste where the backend actually supports it.

    On GNU readline 8.1+ the terminal wraps pastes in ESC[200~/ESC[201~ and
    readline inserts the content (newlines included) into the edit buffer as
    one unit. libedit ignores the inputrc directive without error, so calling
    this is always safe; the return value says which behavior is live so
    callers can rely on the drain fallback instead.
    """
    if not bracketed_paste_supported():
        return False
    try:
        readline.parse_and_bind("set enable-bracketed-paste on")
    except Exception:
        return False
    return True


def normalize_newlines(text: str) -> str:
    """Terminals transmit pasted line breaks as CR (or CRLF); use plain LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def fence_open_after(text: str) -> bool:
    """True when the text ends inside an unclosed ``` fence.

    A fence marker is any line whose stripped form starts with three
    backticks (markdown-style); inline runs like "a ``` b" don't count.
    """
    open_ = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            open_ = not open_
    return open_


def is_slash_command(text: str) -> bool:
    """Only a single-line input starting with "/" dispatches as a command.

    Anything containing a newline is pasted or composed content: interior
    lines must never fire as slash commands, so the whole block is chat.
    """
    if "\n" in text:
        return False
    return text.lstrip().startswith("/")


def sanitize_history_entry(text: str) -> str:
    """Collapse a message to one line for readline history.

    Both backends persist history as newline-delimited files, so an embedded
    newline would split one recalled message into several bogus entries on
    the next load. The marker keeps the structure visible when recalled.
    """
    return normalize_newlines(text).replace("\n", HISTORY_NEWLINE_MARK)


def _history_length() -> int:
    try:
        return readline.get_current_history_length()
    except Exception:
        return 0


def record_history_entry(count_before: Optional[int], message: str) -> None:
    """Replace the entries auto-added while assembling one message.

    input() auto-adds each physical non-empty line (deduping consecutive
    repeats), so a multiline assembly leaves several fragments behind.
    Remove everything added since ``count_before`` and store the whole
    message as one sanitized entry. A plain single-line message keeps its
    auto-added entry untouched so readline's consecutive-duplicate dedupe
    behavior is preserved. remove_history_item is 0-based while
    get_history_item is 1-based (verified on libedit and per GNU docs).
    """
    if count_before is None:
        return
    try:
        current = readline.get_current_history_length()
    except Exception:
        return
    added = max(0, current - count_before)
    if added == 1 and message and "\n" not in message:
        return
    for pos in range(current - 1, current - 1 - added, -1):
        try:
            readline.remove_history_item(pos)
        except Exception:
            break
    entry = sanitize_history_entry(message).strip()
    if not entry:
        return
    try:
        last = readline.get_history_item(readline.get_current_history_length())
        if last != entry:
            readline.add_history(entry)
    except Exception:
        pass


def drain_pending_input(
    fd: Optional[int] = None,
    probe_timeout: float = PASTE_PROBE_S,
    quiet_window: float = PASTE_QUIET_S,
) -> str:
    """Scoop up paste bytes the terminal had already sent when Enter fired.

    When a pasted newline makes input() return, the rest of the paste is
    still in the tty input queue — unechoed, and (in canonical mode) a final
    line without a trailing newline would never even become readable. Reading
    it back immediately in raw-ish mode keeps tabs and partial last lines
    intact. ECHO stays off (we re-echo assembled lines ourselves), and ISIG
    is dropped so a pasted ^C byte becomes content instead of killing the
    assembly. Returns "" when nothing was pending, i.e. the user just typed.
    """
    if fd is None:
        if not sys.stdin.isatty():
            return ""
        fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return ""
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    chunks: List[bytes] = []
    try:
        # TCSANOW, not TCSADRAIN: input-side flag flips must not wait for the
        # reader side of the pty to drain pending output (a paused terminal
        # would wedge the probe window open and swallow typed lines).
        termios.tcsetattr(fd, termios.TCSANOW, new)
        timeout = probe_timeout
        while True:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                break
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            timeout = quiet_window
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except termios.error:
            pass
    return b"".join(chunks).decode("utf-8", "replace")


def _echo_continuation(lines: List[str]) -> None:
    """Show drained paste lines: the tty had echo off while readline was
    reading, so they were consumed invisibly and the transcript would
    otherwise not match what is about to be sent."""
    for line in lines:
        sys.stdout.write("\033[2m... \033[0m%s\n" % line)
    sys.stdout.flush()


def echo_message_block(text: str, header: str = "") -> None:
    """Echo a message that never went through the prompt (queued typeahead,
    /edit) so the transcript shows what is being sent."""
    lines = normalize_newlines(text).split("\n")
    sys.stdout.write("%s\033[2m%s\033[0m\n" % (header, lines[0]))
    for line in lines[1:]:
        sys.stdout.write("\033[2m... %s\033[0m\n" % line)
    sys.stdout.flush()


def _print_paste_hint(count: int) -> None:
    sys.stdout.write(
        "\033[2m  (pasted %d line%s — Enter sends, Ctrl+C discards)\033[0m\n"
        % (count, "" if count == 1 else "s")
    )
    sys.stdout.flush()


def read_user_message(
    prompt: str,
    input_fn: Callable[[str], str] = input,
    drain_fn: Optional[Callable[[], str]] = None,
    continuation_prompt: str = CONTINUATION_PROMPT,
    echo_fn: Optional[Callable[[List[str]], None]] = None,
    use_history: bool = True,
) -> Optional[str]:
    """Read one logical user message from the chat prompt.

    Returns the assembled message, or None when the user cancelled a pending
    continuation with Ctrl+C (nothing partial is submitted). EOFError and
    KeyboardInterrupt on the *first* line propagate unchanged so the caller
    keeps its exit semantics. Single-line slash commands and empty lines
    return immediately and never enter continuation.

    Assembly rules:
    - A drained paste enters block mode: captured lines are echoed, further
      typed lines append, and one Enter on the empty "... " line sends the
      whole block (matching bracketed-paste-on-GNU semantics, where a pasted
      trailing newline never auto-submits).
    - A first input containing embedded newlines (GNU bracketed paste; Enter
      was already pressed on the reviewed buffer) submits immediately.
    - An unclosed ``` fence keeps reading verbatim lines until it closes.
    - A typed line ending in "\\" drops the marker and continues.
    - Ctrl+D mid-assembly submits what has been gathered so far.
    """
    if drain_fn is None:
        drain_fn = drain_pending_input
    if echo_fn is None:
        echo_fn = _echo_continuation
    hist_before = _history_length() if use_history else None

    first = normalize_newlines(input_fn(prompt))
    lines = first.split("\n")
    pasted = len(lines) > 1  # GNU bracketed paste: already reviewed + entered
    typed_last = not pasted  # backslash continuation applies to typed lines only
    block_mode = False

    drained = normalize_newlines(drain_fn())
    if drained:
        tail = drained.split("\n")
        if tail and tail[-1] == "":
            tail.pop()
        echo_fn(tail)
        lines.extend(tail)
        pasted = True
        typed_last = False
        block_mode = True
        _print_paste_hint(len(lines))

    if not pasted:
        only = lines[0]
        if not only.strip() or only.lstrip().startswith("/"):
            return only

    cancelled = False
    while True:
        if fence_open_after("\n".join(lines)):
            pass  # inside an unclosed fence: read verbatim, empty lines included
        elif typed_last and not block_mode and lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1]  # explicit continuation: drop the marker
        elif block_mode:
            pass  # pasted block: wait for the empty line that sends it
        else:
            break
        try:
            nxt = normalize_newlines(input_fn(continuation_prompt))
        except EOFError:
            break  # Ctrl+D: send what we have
        except KeyboardInterrupt:
            cancelled = True
            break
        more = normalize_newlines(drain_fn())
        if block_mode and nxt == "" and not more and not fence_open_after("\n".join(lines)):
            break  # the single Enter that sends the assembled block
        chunk = nxt.split("\n")
        typed_last = len(chunk) == 1 and not more
        if more:
            tail = more.split("\n")
            if tail and tail[-1] == "":
                tail.pop()
            echo_fn(tail)
            chunk.extend(tail)
        lines.extend(chunk)
        if more or len(chunk) > 1:
            block_mode = True

    while len(lines) > 1 and not lines[0].strip():
        lines.pop(0)  # pastes often lead with stray blank lines
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    message = "\n".join(lines)

    if use_history:
        # On cancel the draft still becomes one recallable entry, so Ctrl+C
        # followed by ↑ recovers the text instead of losing it.
        record_history_entry(hist_before, message)
    if cancelled:
        return None
    return message


def _read_paste_block_tty(sentinel: str) -> Optional[str]:
    """Raw-tty line collector for /paste.

    Reads the terminal directly instead of through readline so pasted tabs
    are not eaten by tab completion and nothing lands in history. Echoes
    what it consumes. A line that is exactly ``sentinel`` (unstripped) or a
    Ctrl+D ends the block; Ctrl+C (ISIG stays on) cancels and returns None.
    """
    fd = sys.stdin.fileno()
    out_fd = sys.stdout.fileno()
    try:
        old = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return None
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ECHO | termios.ICANON)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    lines: List[str] = []
    cur = bytearray()
    last_was_cr = False
    done = False
    try:
        termios.tcsetattr(fd, termios.TCSANOW, new)
        while not done:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            for byte in data:
                if byte == 10 and last_was_cr:
                    last_was_cr = False
                    continue  # CRLF: the CR already ended this line
                last_was_cr = byte == 13
                if byte in (13, 10):
                    os.write(out_fd, b"\r\n")
                    line = cur.decode("utf-8", "replace")
                    cur = bytearray()
                    if line == sentinel:
                        done = True
                        break
                    lines.append(line)
                elif byte == 4:  # Ctrl+D
                    os.write(out_fd, b"\r\n")
                    if cur:
                        lines.append(cur.decode("utf-8", "replace"))
                        cur = bytearray()
                    done = True
                    break
                elif byte in (127, 8):  # backspace, utf-8 aware
                    if cur:
                        while cur and (cur[-1] & 0xC0) == 0x80:
                            cur.pop()
                        cur.pop()
                        os.write(out_fd, b"\b \b")
                else:
                    cur.append(byte)
                    os.write(out_fd, bytes((byte,)))
    except KeyboardInterrupt:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except termios.error:
            pass
    if cur:
        lines.append(cur.decode("utf-8", "replace"))
    return "\n".join(lines)


def read_paste_block(
    input_fn: Optional[Callable[[str], str]] = None,
    sentinel: str = PASTE_SENTINEL,
    use_history: bool = True,
) -> Optional[str]:
    """Read a literal block for /paste; None when cancelled with Ctrl+C.

    Content lines are never interpreted — slash commands, "exit", fences,
    everything stays literal. The block ends at a line that is exactly the
    sentinel (no stripping, so indented "." lines survive) or at Ctrl+D. On
    a real terminal the raw-tty reader is used; a custom ``input_fn``
    (tests, pipes) gets a plain line loop with the same protocol.
    """
    before = _history_length() if use_history else None
    if input_fn is None and sys.stdin.isatty() and sys.stdout.isatty():
        text: Optional[str] = _read_paste_block_tty(sentinel)
    else:
        fn = input_fn or input
        lines: List[str] = []
        text = None
        try:
            while True:
                try:
                    line = normalize_newlines(fn(""))
                except EOFError:
                    break
                if line == sentinel:
                    break
                lines.append(line)
            text = "\n".join(lines)
        except KeyboardInterrupt:
            text = None
    if use_history:
        # Scrub any per-line auto-adds (input_fn path); store the block once.
        record_history_entry(before, text or "")
    return text


def edit_in_editor(
    config: Optional[dict] = None,
    initial: str = "",
    use_history: bool = True,
) -> Optional[str]:
    """Compose a message in the user's editor (git-commit style) for /edit.

    Opens a temp file, waits for the editor, and returns the saved contents.
    Returns None when the editor can't launch, exits nonzero, or leaves the
    file empty — the caller treats all of those as "nothing to send".

    The editor comes from the shared conch.config.resolve_editor chain
    (config ``editor`` → $VISUAL → $EDITOR → nano → vi) — the same one
    /notes uses, so the ``editor`` key means one thing everywhere.
    """
    from .config import resolve_editor

    editor = resolve_editor(config)
    try:
        argv = shlex.split(editor)
    except ValueError:
        argv = [editor]
    if not argv:
        argv = ["vi"]
    fd, path = tempfile.mkstemp(prefix="conch-message-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(initial)
        try:
            rc = subprocess.call(argv + [path])
        except OSError as exc:
            print("  \033[31mCould not launch editor %r: %s\033[0m" % (editor, exc))
            return None
        if rc != 0:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            content = normalize_newlines(fh.read())
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    content = content.rstrip("\n")
    if not content.strip():
        return None
    if use_history:
        record_history_entry(_history_length(), content)
    return content
